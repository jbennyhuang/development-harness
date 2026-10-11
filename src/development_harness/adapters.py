from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from typing import Any

from acp import Client, connect_to_agent
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    Implementation,
    RequestPermissionResponse,
    TextContentBlock,
)

from .config import Profile
from .domain import HarnessError
from .process import process_options, terminate

USAGE_MARKERS = (
    "usage limit", "usage_limit", "quota exceeded", "quota_exceeded",
    "insufficient_quota", "credits exhausted", "credit balance", "resource_exhausted",
    "rate limit", "rate_limit", "rate_limit_exceeded", "usage_limit_reached",
)


def usage_exhausted(value: Any) -> bool:
    text = json.dumps(value, default=str).lower()
    return any(marker in text for marker in USAGE_MARKERS)


class ACPClient(Client):
    def __init__(self, emit: Callable[[dict], None]):
        self.emit = emit

    async def request_permission(self, session_id: str, tool_call, options, **kwargs):
        # Full tool permission is confined to the issue container.
        option = next((o for o in options if o.kind in ("allow_once", "allow_always")), None)
        if option is None:
            raise HarnessError("permission_options", "Agent offered no allow option")
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option.option_id)
        )

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        data = update.model_dump(by_alias=True, exclude_none=True)
        self.emit({"session_id": session_id, "update": data})

    async def ext_method(self, method: str, params: dict) -> dict:
        from acp import RequestError
        self.emit({"unsupported_extension": method, "parameter_keys": sorted(params)})
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict) -> None:
        self.emit({"method": method, "params": params})


class ACPAdapter:
    """Provider-neutral ACP v1 conversation manager using the official SDK."""

    def __init__(self, profile: Profile, emit: Callable[[dict], None]):
        self.profile, self.emit = profile, emit
        self.proc = None
        self.connection = None
        self.capabilities: dict = {}
        self.stderr_task = None
        self.workspace = "/workspace"

    async def connect(self, command: list[str], env: dict[str, str]) -> dict:
        self.proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ, **env}, **process_options(),
        )

        async def stderr() -> None:
            total = 0
            while chunk := await self.proc.stderr.read(4096):
                # Do not store unbounded stderr or credentials.
                text = chunk.decode("utf-8", "replace")
                total += len(text)
                if usage_exhausted(text):
                    self.emit({"provider_usage_exhausted": True})
                if total < 100_000:
                    self.emit({"diagnostic": text})

        self.stderr_task = asyncio.create_task(stderr())
        self.connection = connect_to_agent(ACPClient(self.emit), self.proc.stdin, self.proc.stdout)
        response = await asyncio.wait_for(
            self.connection.initialize(
                protocol_version=1, client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="development-harness", version="0.1.0"),
            ), 60,
        )
        self.capabilities = response.model_dump(by_alias=True, exclude_none=True)
        if response.protocol_version != 1:
            raise HarnessError("unsupported_acp_version", "Adapter requires stable ACP v1")
        return self.capabilities

    async def session(self, existing: str | None, history: str = "") -> tuple[str, bool]:
        restored = False
        capabilities = self.capabilities.get("agentCapabilities", {})
        if self.profile.auth_method:
            await asyncio.wait_for(self.connection.authenticate(method_id=self.profile.auth_method), 60)
        if existing and capabilities.get("loadSession"):
            try:
                await self.connection.load_session(cwd=self.workspace, session_id=existing, mcp_servers=[])
                session_id, restored = existing, True
            except Exception as exc:
                if not any(marker in str(exc).lower() for marker in ("not found", "not_found", "unknown session")):
                    raise
                self.emit({"session_restore_unavailable": True})
                response = await self.connection.new_session(cwd=self.workspace, mcp_servers=[])
                session_id = response.session_id
        else:
            response = await self.connection.new_session(cwd=self.workspace, mcp_servers=[])
            session_id = response.session_id
        if self.profile.mode:
            await self.connection.set_session_mode(
                session_id=session_id, mode_id=self.profile.mode
            )
        if self.profile.model:
            await self.connection.set_config_option(session_id=session_id, config_id="model", value=self.profile.model)
        # If native restoration is unsupported, the next prompt receives the recorded history.
        return session_id, restored

    async def prompt(self, session_id: str, text: str, timeout: int) -> dict:
        try:
            response = await asyncio.wait_for(
                self.connection.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text=text)]),
                timeout,
            )
            return response.model_dump(by_alias=True, exclude_none=True)
        except Exception as exc:
            if usage_exhausted(str(exc)):
                raise HarnessError("provider_usage_exhausted", "Provider usage limit reached") from exc
            raise

    async def cancel(self, session_id: str) -> None:
        if self.connection:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.connection.cancel(session_id=session_id), 5)

    async def close_session(self, session_id: str) -> None:
        caps = self.capabilities.get("agentCapabilities", {}).get("sessionCapabilities", {})
        if self.connection and "close" in caps:
            await self.connection.close_session(session_id=session_id)

    async def close(self) -> None:
        if self.connection:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.connection.close(), 5)
        if self.proc:
            await terminate(self.proc)
        if self.stderr_task:
            await asyncio.gather(self.stderr_task, return_exceptions=True)


class CodexAdapter(ACPAdapter):
    """Codex uses codex-acp; provider policy is configured through ACP modes."""


class AntigravityAdapter(ACPAdapter):
    """Google Antigravity uses the separately published agy ACP server."""


ADAPTERS = {"generic": ACPAdapter, "codex": CodexAdapter, "agy": AntigravityAdapter}
