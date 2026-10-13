"""Bounded SDK session negotiation and restoration for orchestration."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from acp import Agent, RequestError
from acp.schema import ClientCapabilities, Implementation, InitializeResponse

from . import __version__
from .domain import HarnessError
from .provider_types import Emit

if TYPE_CHECKING:
    from .config import Profile

async def initialize(agent: Agent) -> InitializeResponse:
    response = await asyncio.wait_for(agent.initialize(
        protocol_version=1, client_capabilities=ClientCapabilities(),
        client_info=Implementation(name="development-harness", version=__version__),
    ), 60)
    if response.protocol_version != 1:
        raise HarnessError("unsupported_acp_version", "Provider requires stable ACP v1")
    return response


async def session(agent: Agent, profile: Profile, capabilities: InitializeResponse,
                  workspace: str, existing: str | None, emit: Emit) -> tuple[str, bool]:
    if profile.auth_method:
        await asyncio.wait_for(agent.authenticate(method_id=profile.auth_method), 60)
    restored = False
    if existing and capabilities.agent_capabilities and capabilities.agent_capabilities.load_session:
        try:
            await asyncio.wait_for(agent.load_session(cwd=workspace, session_id=existing, mcp_servers=[]), 60)
            session_id, restored = existing, True
        except RequestError as exc:
            message = str(exc).lower()
            missing = exc.code == -32002 or (exc.code in (-32602, -32603) and
                any(marker in message for marker in ("session not found", "session_not_found", "unknown session")))
            if not missing or any(marker in message for marker in ("permission", "auth", "denied", "forbidden")):
                raise
            emit({"session_restore_unavailable": True})
            response = await asyncio.wait_for(agent.new_session(cwd=workspace, mcp_servers=[]), 60)
            session_id = response.session_id
    else:
        response = await asyncio.wait_for(agent.new_session(cwd=workspace, mcp_servers=[]), 60)
        session_id = response.session_id
    if profile.mode:
        await asyncio.wait_for(agent.set_session_mode(session_id=session_id, mode_id=profile.mode), 60)
    if profile.model:
        await asyncio.wait_for(agent.set_config_option(session_id=session_id, config_id="model", value=profile.model), 60)
    return session_id, restored


async def close_session(agent: Agent, capabilities: InitializeResponse, session_id: str) -> None:
    caps = capabilities.agent_capabilities
    if caps and caps.session_capabilities and caps.session_capabilities.close is not None:
        await asyncio.wait_for(agent.close_session(session_id=session_id), 5)
