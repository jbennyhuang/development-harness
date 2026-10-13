from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import sys
import zipfile
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Never

import httpx
from acp import Agent, connect_to_agent
from acp.client.connection import ClientSideConnection
from acp.schema import (
    AllowedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallUpdate,
)
from pydantic import BaseModel

from ..config import Config
from ..domain import HarnessError
from ..json_types import JSONObject, json_object, string
from ..process import process_options, run, shield_cleanup, terminate
from ..provider_types import Emit, LaunchContext
from ..quota import usage_exhausted
from ..sessions import close_session, initialize

if TYPE_CHECKING:
    from ..config import Profile

class Antigravity:
    """Independent SDK Client callback receiver and owned stdio transport."""

    def __init__(self, profile: Profile, emit: Emit) -> None:
        self.profile, self.emit = profile, emit
        self.proc: asyncio.subprocess.Process | None = None
        self._connection: ClientSideConnection | None = None
        self.stderr_task: asyncio.Task[None] | None = None

    async def request_permission(self, session_id: str, tool_call: ToolCallUpdate, options: list[PermissionOption], **kwargs: object) -> RequestPermissionResponse:
        # Full tool permission is confined to the issue container.
        option = next((o for o in options if o.kind in ("allow_once", "allow_always")), None)
        if option is None:
            raise HarnessError("permission_options", "Agent offered no allow option")
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option.option_id)
        )

    async def session_update(self, session_id: str, update: BaseModel, **kwargs: object) -> None:
        data = json_object(update.model_dump(by_alias=True, exclude_none=True))
        self.emit({"session_id": session_id, "update": data})

    async def ext_method(self, method: str, params: Mapping[str, object]) -> JSONObject:
        from acp import RequestError
        params = json_object(params)
        self.emit({"unsupported_extension": method, "parameter_keys": [key for key in sorted(params)]})
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: Mapping[str, object]) -> None:
        self.emit({"method": method, "params": json_object(params)})

    async def _unsupported(self, *args: object, **kwargs: object) -> Never:
        from acp import RequestError
        raise RequestError.method_not_found("Client capability not advertised")

    write_text_file = _unsupported
    read_text_file = _unsupported
    create_terminal = _unsupported
    terminal_output = _unsupported
    release_terminal = _unsupported
    wait_for_terminal_exit = _unsupported
    kill_terminal = _unsupported
    create_elicitation = _unsupported
    complete_elicitation = _unsupported

    def on_connect(self, conn: Agent) -> None:
        # The SDK invokes this during construction.
        pass

    async def connect(self, command: list[str], env: dict[str, str]) -> Agent:
        self.proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ, **env}, **process_options(),
        )

        assert self.proc.stdin is not None and self.proc.stdout is not None
        assert self.proc.stderr is not None
        stream = self.proc.stderr

        async def stderr() -> None:
            total = 0
            while chunk := await stream.read(4096):
                # Do not store unbounded stderr or credentials.
                text = chunk.decode("utf-8", "replace")
                total += len(text)
                if usage_exhausted(text):
                    self.emit({"provider_usage_exhausted": True})
                if total < 100_000:
                    self.emit({"diagnostic": text})

        self.stderr_task = asyncio.create_task(stderr())
        self._connection = connect_to_agent(self, self.proc.stdin, self.proc.stdout)
        return self._connection

    async def close(self) -> None:
        if self._connection:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._connection.close(), 5)
        if self.proc:
            await terminate(self.proc)
        if self.stderr_task:
            try:
                await asyncio.wait_for(asyncio.shield(self.stderr_task), 5)
            except (Exception, asyncio.CancelledError):
                self.stderr_task.cancel()
                await asyncio.gather(self.stderr_task, return_exceptions=True)


@asynccontextmanager
async def transport(profile: Profile, emit: Emit, command: list[str], env: dict[str, str]) -> AsyncGenerator[Agent]:
    client = Antigravity(profile, emit)
    try:
        yield await client.connect(command, env)
    finally:
        await shield_cleanup(client.close())


@asynccontextmanager
async def provider(profile: Profile, emit: Emit, context: LaunchContext) -> AsyncGenerator[Agent]:
    await context.backend.credentials(context.issue_id, profile)
    command = context.backend.agent_command(context.issue_id, profile, context.env)
    async with transport(profile, emit, command, context.env) as agent:
        yield agent

async def login(root: Path, profile_name: str) -> JSONObject:
    config = Config.load(root)
    if profile_name not in config.profiles or config.profiles[profile_name].provider != "agy":
        raise HarnessError("login_configuration", "This OAuth bootstrap supports Antigravity profiles; configure Codex auth files or API-key env_names separately")
    os_name = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    machine = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    release_os = "macos" if os_name == "darwin" else os_name
    if not os_name:
        raise HarnessError("unsupported_platform", "No official native ACP helper for this platform")
    tools = root / ".harness" / "tool-cache" / "agy-acp" / "1.3.0"
    tools.mkdir(parents=True, exist_ok=True)
    executable = tools / ("agy_acp_server.exe" if os_name == "windows" else "agy_acp_server.par")
    if not executable.exists():
        archive = tools / "agy.zip"
        url = f"https://dl.google.com/agy-extensions/releases/{release_os}/agy-acp-server-1.3.0-{os_name}-{machine}.zip"
        async with httpx.AsyncClient(timeout=120) as http:
            response = await http.get(url)
            response.raise_for_status()
            archive.write_bytes(response.content)
        with zipfile.ZipFile(archive) as files:
            for member in files.infolist():
                target = (tools / member.filename).resolve()
                if not target.is_relative_to(tools.resolve()):
                    raise HarnessError("unsafe_archive", "Archive member escaped the owned tool directory")
            files.extractall(tools)
        if os.name != "nt":
            executable.chmod(0o755)
            for helper in tools.glob("localharness_external*"):
                helper.chmod(0o755)
    cache = root / ".harness" / "credentials" / "gemini"
    cache.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        cache.chmod(0o700)
    else:
        account = os.environ.get("USERDOMAIN", ".") + "\\" + os.environ["USERNAME"]
        await run(["icacls", str(cache), "/inheritance:r", "/grant:r", account + ":(OI)(CI)(F)"])

    def emit(data: JSONObject) -> None:
        diagnostic = string(data.get("diagnostic", ""))
        if "Open the following link" in diagnostic:
            print(diagnostic, file=sys.stderr, end="", flush=True)
        elif data.get("unsupported_extension"):
            print("Authentication helper requested unsupported extension: " + string(data["unsupported_extension"]), file=sys.stderr)

    profile = config.profiles[profile_name]
    command = [str(executable), *(["--uid="] if os_name == "linux" else [])]
    async with transport(profile, emit, command, {"GEMINI_HOME": str(cache)}) as agent:
        capabilities = await initialize(agent)
        await asyncio.wait_for(agent.authenticate(method_id="oauth-personal"), 600)
        response = await asyncio.wait_for(agent.new_session(cwd=str(root), mcp_servers=[]), 60)
        with contextlib.suppress(Exception):
            await close_session(agent, capabilities, response.session_id)
        folder = cache / "antigravity-acp"
        required = [folder / "acp_token.json", folder / "settings.json"]
        if not all(file.is_file() for file in required):
            raise HarnessError("credential_location_changed", "Authentication succeeded but expected ACP cache files are absent")
        profile.credential_files = {str(file): "/root/.gemini/antigravity-acp/" + file.name for file in required}
        profile.auth_method = "oauth-personal"
        config.save(root)
        if os.name != "nt":
            for file in required:
                file.chmod(0o600)
        return {"profile": profile_name, "authenticated": True, "credential_directory": str(cache),
                "next": "harness adapters probe --profile " + profile_name + " --session --json"}


def default_profile() -> Profile:
    """Pinned container helper; Linux uses an empty UID for the isolated root user."""
    from ..config import Profile
    return Profile(provider="agy", command=["/opt/agy/agy_acp_server.par", "--uid="])
