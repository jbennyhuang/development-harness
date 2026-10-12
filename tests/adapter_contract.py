"""Strict-checked third-party ABC extension, including its own constructor/members."""
from typing import override

from acp import Client

from development_harness.adapters import AcpAgentAdapter, Emit, LaunchContext, Update
from development_harness.config import Profile
from development_harness.engine import Engine


class ThirdPartyAdapter(AcpAgentAdapter):
    def __init__(self, engine: Engine, profile: Profile, emit: Emit) -> None:
        super().__init__(profile, emit)
        self.engine, self.emit = engine, emit
        self.capabilities: Update = {"provider": "third-party"}
        self.context: LaunchContext | None = None
        self.closed = False
        self.prompts: list[str] = []
        self.restores: list[str | None] = []

    @override
    async def setup(self, context: LaunchContext) -> list[str]:
        return ["third-party-worker"]

    @override
    async def start(self, context: LaunchContext) -> Update:
        self.context = context
        return self.capabilities

    @override
    async def authenticate(self, method_id: str, timeout: int = 60) -> None:
        pass

    @override
    async def session(self, existing: str | None, history: str = "") -> tuple[str, bool]:
        self.restores.append(existing)
        return existing or "third-party-session", bool(existing)

    @override
    async def prompt(self, session_id: str, text: str, timeout: int) -> Update:
        assert self.context is not None
        self.prompts.append(text)
        self.emit({"session_id": session_id, "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Need input"},
        }})
        self.engine.submit(self.context.env["HARNESS_WORKER_TOKEN"], {
            "kind": "guidance", "summary": "Choose the expected output format",
        })
        return {"stopReason": "end_turn"}

    @override
    async def cancel(self, session_id: str) -> None:
        pass

    @override
    async def close_session(self, session_id: str) -> None:
        pass

    @override
    async def close(self) -> None:
        self.closed = True


def subclass_contract(adapter: ThirdPartyAdapter) -> AcpAgentAdapter:
    return adapter


def retains_subclass_members(adapter: ThirdPartyAdapter) -> list[str]:
    engine: Engine = adapter.engine
    assert engine is adapter.engine
    return adapter.prompts


def sdk_client_contract(adapter: AcpAgentAdapter) -> Client:
    return adapter
