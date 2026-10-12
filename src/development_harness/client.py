from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .domain import HarnessError
from .json_types import JSONObject, decode_object, json_object, string
from .service import state_directory


def metadata(root: Path) -> JSONObject | None:
    try:
        return decode_object((root / ".harness" / "service.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def request(info: JSONObject, path: str, body: JSONObject | None=None, timeout: float=30) -> JSONObject:
    url = string(info["url"])
    if not url.startswith("http://127.0.0.1:"):
        raise HarnessError("invalid_service", "Service must be on host loopback")
    with httpx.Client(trust_env=False, timeout=timeout) as client:
        headers = {"Authorization": "Bearer " + string(info["token"])}
        response = client.get(url + path, headers=headers) if body is None else client.post(url + path, json=body, headers=headers)
        envelope = json_object(response.json())
        if not envelope["ok"]:
            error = json_object(envelope["error"])
            raise HarnessError(string(error["code"]), string(error["message"]), **json_object(error.get("details", {})))
        return json_object(envelope["result"])


def status(root: Path) -> JSONObject:
    info = metadata(root)
    if info:
        try:
            return request(info, "/status", timeout=2)
        except (httpx.HTTPError, ValueError, HarnessError):
            pass
    return {"running": False, "project": str(root)}


def start(root: Path) -> JSONObject:
    if status(root).get("pid"):
        info = metadata(root)
        assert info is not None
        return info
    state = state_directory(root)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(state / "service.log", "ab") as log:
        subprocess.Popen([sys.executable, "-m", "development_harness.service", "--project", str(root)],
                         cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=flags, start_new_session=os.name != "nt")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        info = metadata(root)
        if info:
            try:
                request(info, "/status", timeout=2)
                return info
            except (httpx.HTTPError, ValueError, HarnessError):
                pass
        time.sleep(0.2)
    raise HarnessError("service_start_failed", "Service did not become ready; inspect .harness/service.log")


def invoke(root: Path, command: str, args: JSONObject, key: str | None=None) -> JSONObject:
    info = start(root)
    return request(info, "/command", {"command": command, "args": args, "key": key}, timeout=1800)
