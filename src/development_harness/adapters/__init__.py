from __future__ import annotations

import asyncio
import contextlib
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never, override

from acp import Agent, connect_to_agent
from acp.client.connection import ClientSideConnection
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallUpdate,
)
from pydantic import BaseModel

from .. import __version__
from ..backend import Backend
from ..json_types import JSONObject, JSONValue, json_object

if TYPE_CHECKING:
    from ..config import Profile
from ..domain import HarnessError
from ..process import process_options, terminate

Update = JSONObject
Emit = Callable[[Update], None]

@dataclass(frozen=True)
class LaunchContext:
    """Prepared issue container; backend retains ownership and shutdown policy."""
    backend: Backend
    issue_id: str
    env: dict[str, str]
    workspace: str = "/workspace"


USAGE_MARKERS = (
    "usage limit", "usage_limit", "quota exceeded", "quota_exceeded",
    "insufficient_quota", "credits exhausted", "credit balance", "resource_exhausted",
    "rate limit", "rate_limit", "rate_limit_exceeded", "usage_limit_reached",
)


def usage_exhausted(value: object) -> bool:
    text = json.dumps(value, default=str).lower()
    return any(marker in text for marker in USAGE_MARKERS)


class AcpAgentAdapter(ABC):
    """ACP callback receiver and provider-neutral lifecycle manager using the SDK."""

    def __init__(self, profile: Profile, emit: Callable[[JSONObject], None]) -> None:
        self.profile, self.emit = profile, emit
        self.proc: asyncio.subprocess.Process | None = None
        self._connection: ClientSideConnection | None = None
        self.capabilities: JSONObject = {}
        self.stderr_task: asyncio.Task[None] | None = None
        self.workspace = "/workspace"

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
        # The SDK invokes this during construction; connect owns initialization.
        pass

    async def connect(self, command: list[str], env: dict[str, str]) -> JSONObject:
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
        response = await asyncio.wait_for(
            self._connection.initialize(
                protocol_version=1, client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="development-harness", version=__version__),
            ), 60,
        )
        self.capabilities = json_object(response.model_dump(by_alias=True, exclude_none=True))
        if response.protocol_version != 1:
            raise HarnessError("unsupported_acp_version", "Adapter requires stable ACP v1")
        return self.capabilities

    def _require_connection(self) -> Agent:
        if self._connection is None:
            raise HarnessError("adapter_not_started", "Start the adapter before using a session")
        return self._connection

    async def authenticate(self, method_id: str, timeout: int = 60) -> None:
        await asyncio.wait_for(self._require_connection().authenticate(method_id=method_id), timeout)

    async def start(self, context: LaunchContext) -> Update:
        self.workspace = context.workspace
        command = await self.setup(context)
        return await self.connect(command, context.env)

    @abstractmethod
    async def setup(self, context: LaunchContext) -> list[str]:
        """Prepare provider credentials and return an isolated ACP launch argv."""
        raise NotImplementedError

    async def session(self, existing: str | None, history: str = "") -> tuple[str, bool]:
        connection = self._require_connection()
        restored = False
        capabilities = json_object(self.capabilities.get("agentCapabilities", {}))
        if self.profile.auth_method:
            await self.authenticate(self.profile.auth_method)
        if existing and capabilities.get("loadSession"):
            try:
                await connection.load_session(cwd=self.workspace, session_id=existing, mcp_servers=[])
                session_id, restored = existing, True
            except Exception as exc:
                if not any(marker in str(exc).lower() for marker in ("not found", "not_found", "unknown session")):
                    raise
                self.emit({"session_restore_unavailable": True})
                response = await connection.new_session(cwd=self.workspace, mcp_servers=[])
                session_id = response.session_id
        else:
            response = await connection.new_session(cwd=self.workspace, mcp_servers=[])
            session_id = response.session_id
        if self.profile.mode:
            await connection.set_session_mode(
                session_id=session_id, mode_id=self.profile.mode
            )
        if self.profile.model:
            await connection.set_config_option(session_id=session_id, config_id="model", value=self.profile.model)
        # If native restoration is unsupported, the next prompt receives the recorded history.
        return session_id, restored

    async def prompt(self, session_id: str, text: str, timeout: int) -> JSONObject:
        connection = self._require_connection()
        try:
            response = await asyncio.wait_for(
                connection.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text=text)]),
                timeout,
            )
            return json_object(response.model_dump(by_alias=True, exclude_none=True))
        except Exception as exc:
            if usage_exhausted(str(exc)):
                raise HarnessError("provider_usage_exhausted", "Provider usage limit reached") from exc
            raise

    async def cancel(self, session_id: str) -> None:
        if self._connection:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._connection.cancel(session_id=session_id), 5)

    async def close_session(self, session_id: str) -> None:
        caps = json_object(json_object(self.capabilities.get("agentCapabilities", {})).get("sessionCapabilities", {}))
        if self._connection and "close" in caps:
            await asyncio.wait_for(self._connection.close_session(session_id=session_id), 5)

    async def close(self) -> None:
        if self._connection:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._connection.close(), 5)
        if self.proc:
            await terminate(self.proc)
        if self.stderr_task:
            await asyncio.gather(self.stderr_task, return_exceptions=True)


class GenericACPAdapter(AcpAgentAdapter):
    """Configured ACP command with backend-owned credential fencing."""
    @override
    async def setup(self, context: LaunchContext) -> list[str]:
        await context.backend.credentials(context.issue_id, self.profile)
        return context.backend.agent_command(context.issue_id, self.profile, context.env)

ACPAdapter = GenericACPAdapter
# Secondary compatibility name for existing public imports.
CodingHarnessAdapter = AcpAgentAdapter
AdapterFactory = Callable[["Profile", Emit], AcpAgentAdapter]

# Providers import the fully defined base; defaults create Profiles only on demand.
from .antigravity import Antigravity, AntigravityAdapter  # noqa: E402 — providers require the defined base
from .codex import Codex, CodexAdapter  # noqa: E402 — providers require the defined base

ADAPTERS: dict[str, AdapterFactory] = {
    "generic": GenericACPAdapter, "codex": Codex, "agy": Antigravity,
}

def resolve_adapter_factory(provider: str) -> AdapterFactory:
    factory = ADAPTERS.get(provider)
    if factory is None:
        raise HarnessError("unknown_provider", "Coding provider is not registered", provider=provider)
    return factory

__all__ = ["AcpAgentAdapter", "CodingHarnessAdapter", "LaunchContext", "AdapterFactory", "ADAPTERS", "Emit", "Update",
           "JSONObject", "JSONValue", "resolve_adapter_factory", "GenericACPAdapter", "ACPAdapter", "Codex", "CodexAdapter",
           "Antigravity", "AntigravityAdapter", "usage_exhausted"]
