from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Never

from acp import Agent, connect_to_agent
from acp.client.connection import ClientSideConnection
from acp.schema import (
    AllowedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallUpdate,
)
from pydantic import BaseModel

from ..domain import HarnessError
from ..json_types import JSONObject, json_object
from ..process import process_options, shield_cleanup, terminate
from ..provider_types import Emit, LaunchContext
from ..quota import usage_exhausted

if TYPE_CHECKING:
    from ..config import Profile

class Codex:
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
    client = Codex(profile, emit)
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

def default_profile() -> Profile:
    """Image-installed codex-acp with full permission confined to the issue container."""
    from ..config import Profile
    return Profile(provider="codex", command=["codex-acp"],
                   env_names=["OPENAI_API_KEY", "CODEX_API_KEY"], mode="agent-full-access")
