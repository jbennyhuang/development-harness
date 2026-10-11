from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import sys
from pathlib import Path

from aiohttp import web
from pydantic import ValidationError

from .commands import Commands
from .config import Config, project_root
from .domain import HarnessError
from .engine import Engine
from .store import Store

LOG = logging.getLogger(__name__)


def state_directory(root: Path) -> Path:
    state = root / ".harness"
    state.mkdir(exist_ok=True)
    if not state.resolve().is_relative_to(root.resolve()):
        raise HarnessError("unsafe_path", ".harness must stay inside the project")
    return state


class ProjectLock:
    """Kernel-owned lock: released on process death on both Windows and POSIX."""

    def __init__(self, path):
        self.file = open(path, "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise HarnessError("service_running", "Another service owns this project") from exc

    def close(self):
        self.file.close()


def publish(path: Path, data: dict):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    if os.name != "nt":
        temp.chmod(0o600)
    else:
        # Restrict the bearer credential to the current Windows account.
        import subprocess
        account = os.environ.get("USERDOMAIN", ".") + "\\" + os.environ["USERNAME"]
        subprocess.run(["icacls", str(temp), "/inheritance:r", "/grant:r", account + ":(F)"],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    temp.replace(path)


async def serve(root: Path):
    state = state_directory(root)
    lock = ProjectLock(state / "service.lock")
    store = Store(state / "journal.sqlite")
    config = Config.load(root)
    if config.tool_paths:
        os.environ["PATH"] = os.pathsep.join(config.tool_paths) + os.pathsep + os.environ.get("PATH", "")
    engine = Engine(root, config, store)
    commands = Commands(engine)
    stop = asyncio.Event()
    token = secrets.token_urlsafe(48)
    generation = secrets.token_hex(16)

    @web.middleware
    async def errors(request, handler):
        try:
            result = await handler(request)
            return web.json_response({"ok": True, "result": result})
        except HarnessError as exc:
            return web.json_response({"ok": False, "error": exc.as_dict()},
                                     status=401 if exc.code == "unauthorized" else 400)
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            return web.json_response({"ok": False, "error": {"code": "invalid_arguments", "message": str(exc), "details": {}}}, status=400)
        except Exception:
            LOG.exception("Control request failed")
            return web.json_response({"ok": False, "error": {"code": "internal_error", "message": "See service log", "details": {}}}, status=500)

    def authorize(request):
        received = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(received, token):
            raise HarnessError("unauthorized", "Host service authentication failed")

    async def status(request):
        authorize(request)
        return {"pid": os.getpid(), "project": str(root), "generation": generation,
                "execution": store.execution_state()}

    async def command(request):
        authorize(request)
        data = await request.json()
        return await commands.invoke(data["command"], data.get("args", {}), data.get("key"))

    async def shutdown(request):
        authorize(request)
        stop.set()
        return {"stopping": True}

    async def worker(request):
        credential = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if request.path == "/assignment":
            return engine.worker_assignment(credential)
        return engine.submit(credential, await request.json())

    admin = web.Application(middlewares=[errors], client_max_size=1_000_000)
    admin.add_routes([web.get("/status", status), web.post("/command", command), web.post("/stop", shutdown)])
    workers = web.Application(middlewares=[errors], client_max_size=1_000_000)
    workers.add_routes([web.get("/assignment", worker), web.post("/submit", worker)])
    admin_runner, worker_runner = web.AppRunner(admin, access_log=None), web.AppRunner(workers, access_log=None)
    scheduler = None
    try:
        await engine.recover()
        await admin_runner.setup()
        await worker_runner.setup()
        admin_site = web.TCPSite(admin_runner, "127.0.0.1", 0)
        worker_site = web.TCPSite(worker_runner, "0.0.0.0", 0)
        await admin_site.start()
        await worker_site.start()
        admin_port = admin_site._server.sockets[0].getsockname()[1]
        worker_port = worker_site._server.sockets[0].getsockname()[1]
        engine.worker_url = f"http://host.docker.internal:{worker_port}"
        publish(state / "service.json", {"url": f"http://127.0.0.1:{admin_port}", "token": token,
                                         "pid": os.getpid(), "generation": generation})

        async def schedule():
            while not stop.is_set():
                try:
                    await engine.tick()
                except Exception as exc:
                    LOG.exception("Scheduler failure")
                    store.emit("scheduler.failed", {"message": str(exc)})
                    await engine.pause_project("scheduler_failure", "Inspect service log and resume")
                try:
                    await asyncio.wait_for(stop.wait(), 0.5)
                except TimeoutError:
                    pass

        scheduler = asyncio.create_task(schedule())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    finally:
        if scheduler:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
        await engine.close()
        await admin_runner.cleanup()
        await worker_runner.cleanup()
        path = state / "service.json"
        if path.exists() and json.loads(path.read_text()).get("generation") == generation:
            path.unlink()
        store.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(serve(project_root(args.project)))


if __name__ == "__main__":
    main()
