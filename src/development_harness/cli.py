from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Never, Protocol, override

import httpx
from pydantic import ValidationError

from . import __version__, client
from .catalog import COMMANDS, schema
from .config import Config, project_root
from .domain import HarnessError
from .git import Git
from .instructions import ORCHESTRATOR
from .json_types import JSONObject, boolean, decode_object, json_object, optional_string, string
from .tracker import Beads


class Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        raise HarnessError("invalid_arguments", message)


def common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit a JSON envelope")
    parser.add_argument("--project", default=argparse.SUPPRESS, help="Select a project directory")
    parser.add_argument("--input", default=argparse.SUPPRESS, help="JSON arguments file, or - for stdin")
    parser.add_argument("--key", default=argparse.SUPPRESS, help="Idempotency key for a retried mutation")


class Subcommands(Protocol):
    def add_parser(self, name: str, *, help: str, description: str) -> argparse.ArgumentParser: ...


def parser() -> Parser:
    root = Parser(prog="harness", description="Manage durable, isolated ACP development and review workflows")
    root.add_argument("--version", action="version", version=__version__)
    common(root)
    subs = root.add_subparsers(dest="group", required=True, parser_class=Parser)
    groups: dict[str, Subcommands] = {}
    for name, (description, arguments) in COMMANDS.items():
        words = name.split()
        if len(words) == 1:
            leaf = subs.add_parser(name, help=description, description=description)
        else:
            if words[0] not in groups:
                group = subs.add_parser(words[0])
                common(group)
                groups[words[0]] = group.add_subparsers(dest="action", required=True, parser_class=Parser)
            leaf = groups[words[0]].add_parser(words[1], help=description, description=description)
        common(leaf)
        leaf.set_defaults(command=name)
        for key, definition in arguments.items():
            if definition["type"] == "bool":
                leaf.add_argument("--" + key.replace("_", "-"), action="store_true", default=argparse.SUPPRESS, help=definition["description"])
            elif definition["type"] == "object":
                leaf.add_argument("--" + key.replace("_", "-"), type=decode_object, default=argparse.SUPPRESS, help=definition["description"])
            else:
                leaf.add_argument("--" + key.replace("_", "-"), type=int if definition["type"] == "int" else str, default=argparse.SUPPRESS, help=definition["description"])
    return root


def selected_root(value: str | None=None) -> Path:
    if value:
        return project_root(value)
    cwd = Path.cwd().resolve()
    for path in (cwd, *cwd.parents):
        if (path / ".harness" / "config.json").exists() or (path / ".git").exists():
            return path
    return cwd


async def initialize(root: Path, args: JSONObject) -> JSONObject:
    config = Config.load(root)
    if config.tool_paths:
        os.environ["PATH"] = os.pathsep.join(config.tool_paths) + os.pathsep + os.environ.get("PATH", "")
    if args.get("target_branch"):
        config.target_branch = string(args["target_branch"])
    elif not (root / ".harness" / "config.json").exists():
        code, out, _ = await Git(root).call("branch", "--show-current", check=False)
        if not code and out.strip():
            config.target_branch = out.strip()
    git = Git(root, config.target_branch, config.remote)
    await git.initialize(boolean(args.get("initialize_git", False)))
    config.save(root)
    result: JSONObject = {"project": str(root), "target_branch": config.target_branch}
    if not args.get("skip_beads", False):
        result.update(await Beads(root, config.bd_command).initialize())
    return result


def worker(command: str, args: JSONObject) -> JSONObject:
    url, token = os.getenv("HARNESS_WORKER_URL"), os.getenv("HARNESS_WORKER_TOKEN")
    if not url or not token:
        raise HarnessError("missing_assignment", "Worker credentials are provided only inside an active issue container")
    with httpx.Client(trust_env=False, timeout=30) as transport:
        headers = {"Authorization": "Bearer " + token}
        if command == "worker assignment":
            response = transport.get(url + "/assignment", headers=headers)
        else:
            response = transport.post(url + "/submit", json=json_object(args["result"]), headers=headers)
        envelope = json_object(response.json())
        if not envelope["ok"]:
            error = json_object(envelope["error"])
            raise HarnessError(string(error["code"]), string(error["message"]), **json_object(error.get("details", {})))
        return json_object(envelope["result"])


def execute(command: str, args: JSONObject, root: Path, key: str | None) -> JSONObject:
    if command.startswith("worker "):
        return worker(command, args)
    if command == "schema":
        return schema()
    if command == "status":
        from .store import project_status
        return project_status(root)
    if command == "instructions":
        return {"instructions": ORCHESTRATOR, "poll_interval": Config.load(root).poll_interval}
    if command == "init":
        if client.status(root).get("pid"):
            raise HarnessError("service_running", "Stop the service before initializing project configuration")
        return asyncio.run(initialize(root, args))
    if command == "service start":
        client.start(root)
        return client.status(root)
    if command == "service status":
        return client.status(root)
    if command == "service stop":
        info = client.metadata(root)
        return client.request(info, "/stop", {}) if info and client.status(root).get("pid") else {"running": False}
    if command == "image build":
        from .tooling import build_image
        return asyncio.run(build_image(root))
    if command == "tools setup":
        if client.status(root).get("pid"):
            raise HarnessError("service_running", "Stop the service before changing tool paths")
        from .tooling import setup_tools
        return asyncio.run(setup_tools(root))
    if command == "tui":
        config = Config.load(root)
        env = {**os.environ, "PATH": os.pathsep.join(config.tool_paths) + os.pathsep + os.environ.get("PATH", "")}
        code = subprocess.call(["perles"], cwd=root, env=env)
        if code:
            raise HarnessError("tui_failed", "Perles exited unsuccessfully", returncode=code)
        return {"closed": True}
    if command == "adapters probe":
        from .tooling import probe
        return asyncio.run(probe(root, string(args["profile"]), boolean(args.get("session", False))))
    if command == "adapters login":
        if client.status(root).get("pid"):
            raise HarnessError("service_running", "Stop the service before changing authentication configuration")
        from .tooling import login
        return asyncio.run(login(root, string(args["profile"])))
    return client.invoke(root, command, args, key)


def main() -> None:
    json_mode = "--json" in sys.argv
    try:
        values = json_object(vars(parser().parse_args()))
        command = string(values.pop("command"))
        values.pop("group", None)
        values.pop("action", None)
        json_mode = boolean(values.pop("json", False))
        root = selected_root(optional_string(values.pop("project", None)))
        key, input_path = optional_string(values.pop("key", None)), optional_string(values.pop("input", None))
        if input_path:
            raw = sys.stdin.read() if input_path == "-" else Path(input_path).read_text(encoding="utf-8-sig")
            data = decode_object(raw)
            if command == "worker submit" and "kind" in data:
                data = {"result": data}
            values = {**data, **values}
        definitions = COMMANDS[command][1]
        if set(values) - definitions.keys():
            raise HarnessError("invalid_arguments", "Unknown input fields")
        for name, definition in definitions.items():
            if definition["required"] and values.get(name) is None:
                raise HarnessError("invalid_arguments", f"Missing --{name.replace('_', '-')}; --input may supply it")
            if name not in values and definition["default"] is not None:
                values[name] = definition["default"]
        result = execute(command, values, root, key)
        if json_mode:
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
        elif command == "instructions":
            print(result["instructions"])
        else:
            print(json.dumps(result, indent=2, ensure_ascii=True))
    except KeyboardInterrupt:
        error: JSONObject = {"code": "interrupted", "message": "CLI interrupted; the service retains execution state", "details": {}}
        print(json.dumps({"ok": False, "error": error}), file=sys.stdout if json_mode else sys.stderr)
        raise SystemExit(130)
    except (HarnessError, ValidationError, ValueError, OSError, httpx.HTTPError) as exc:
        error = exc.as_dict() if isinstance(exc, HarnessError) else {"code": "invalid_arguments" if isinstance(exc, (ValueError, ValidationError)) else "operation_failed", "message": str(exc), "details": {}}
        print(json.dumps({"ok": False, "error": error}), file=sys.stdout if json_mode else sys.stderr)
        raise SystemExit(2 if string(error["code"]) == "invalid_arguments" else 1)
