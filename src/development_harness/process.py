from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import TypedDict

from .domain import HarnessError


class ProcessOptions(TypedDict, total=False):
    creationflags: int
    start_new_session: bool

async def terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), 5)
    except (ProcessLookupError, asyncio.TimeoutError):
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def process_options() -> ProcessOptions:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}


async def run(argv: list[str], cwd: Path | None = None, timeout: float = 120,
              check: bool = True, env: dict[str, str] | None = None, max_output: int = 2_000_000) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=2_000_000, **process_options(),
        )
    except FileNotFoundError as exc:
        raise HarnessError("missing_tool", f"Executable not found: {argv[0]}") from exc

    async def read(stream: asyncio.StreamReader) -> bytes:
        data = bytearray()
        while chunk := await stream.read(65536):
            if len(data) < max_output:
                data.extend(chunk[:max_output - len(data)])
        return bytes(data)

    assert proc.stdout is not None and proc.stderr is not None
    readers = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
    try:
        await asyncio.wait_for(proc.wait(), timeout)
        out, err = await asyncio.gather(*readers)
    except BaseException:
        await terminate(proc)
        await asyncio.gather(*readers, return_exceptions=True)
        raise
    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if check and proc.returncode:
        raise HarnessError("command_failed", f"{argv[0]} exited with {proc.returncode}",
                           returncode=proc.returncode, stderr=stderr, stdout=stdout)
    assert proc.returncode is not None
    return proc.returncode, stdout, stderr
