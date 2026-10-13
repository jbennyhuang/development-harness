"""Strict structural SDK contracts, independent of project provider classes."""
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import AsyncContextManager, Never

from acp import Agent, Client, RequestError
from acp.schema import (
    AgentCapabilities,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
)

from development_harness.adapters.antigravity import Antigravity
from development_harness.adapters.codex import Codex
from development_harness.config import Profile
from development_harness.engine import Engine
from development_harness.json_types import JSONObject
from development_harness.provider_types import AdapterFactory, Emit, LaunchContext


class ThirdPartyAgent:
    def __init__(self, engine: Engine, emit: Emit, context: LaunchContext) -> None:
        self.engine, self.emit, self.context = engine, emit, context
        self.closed = False
        self.prompts: list[str] = []
        self.restores: list[str | None] = []

    async def initialize(self, *args: object, **kwargs: object) -> InitializeResponse:
        return InitializeResponse(protocol_version=1, agent_capabilities=AgentCapabilities(load_session=True))

    async def new_session(self, *args: object, **kwargs: object) -> NewSessionResponse:
        self.restores.append(None)
        return NewSessionResponse(session_id="third-party-session")

    async def load_session(self, cwd: str, session_id: str, *args: object, **kwargs: object) -> LoadSessionResponse:
        self.restores.append(session_id)
        return LoadSessionResponse()

    async def authenticate(self, *args: object, **kwargs: object) -> None:
        pass

    set_session_mode = authenticate
    set_config_option = authenticate
    close_session = authenticate
    cancel = authenticate

    async def prompt(self, session_id: str, prompt: object, **kwargs: object) -> PromptResponse:
        self.prompts.append(str(prompt))
        self.emit({"session_id": session_id, "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Explain how usage limits work"},
        }})
        self.engine.submit(self.context.env["HARNESS_WORKER_TOKEN"], {
            "kind": "guidance", "summary": "Choose the expected output format",
        })
        return PromptResponse(stop_reason="end_turn")

    async def unsupported(self, *args: object, **kwargs: object) -> Never:
        raise RequestError.method_not_found("Unsupported")

    list_sessions = unsupported
    fork_session = unsupported
    resume_session = unsupported

    async def ext_method(self, method: str, params: Mapping[str, object]) -> JSONObject:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: Mapping[str, object]) -> None:
        pass

    def on_connect(self, conn: Client) -> None:
        pass


@asynccontextmanager
async def third_party_provider(engine: Engine, instances: list[ThirdPartyAgent], profile: Profile,
                               emit: Emit, context: LaunchContext) -> AsyncGenerator[Agent]:
    agent = ThirdPartyAgent(engine, emit, context)
    instances.append(agent)
    try:
        yield agent
    finally:
        agent.closed = True


def factory_contract(factory: AdapterFactory) -> AdapterFactory:
    return factory


def sdk_client_contract(codex: Codex, antigravity: Antigravity) -> tuple[Client, Client]:
    return codex, antigravity


def third_party_factory(engine: Engine, instances: list[ThirdPartyAgent]) -> AdapterFactory:
    def factory(profile: Profile, emit: Emit, context: LaunchContext) -> AsyncContextManager[Agent]:
        return third_party_provider(engine, instances, profile, emit, context)
    return factory
