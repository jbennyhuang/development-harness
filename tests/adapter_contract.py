"""A third-party adapter: no ACP dependency or base class."""
from development_harness.adapters import CodingHarnessAdapter, Emit, LaunchContext, Update
from development_harness.config import Profile
from development_harness.engine import Engine


class ThirdPartyAdapter:
    def __init__(self, engine: Engine, profile: Profile, emit: Emit):
        self.engine, self.emit = engine, emit
        self.capabilities: Update = {"provider": "third-party"}
        self.context: LaunchContext | None = None
        self.closed = False
        self.prompts: list[str] = []
        self.restores: list[str | None] = []

    async def start(self, context: LaunchContext) -> Update:
        self.context = context
        return self.capabilities

    async def authenticate(self, method_id: str, timeout: int = 60) -> None:
        pass

    async def session(self, existing: str | None, history: str = "") -> tuple[str, bool]:
        self.restores.append(existing)
        return existing or "third-party-session", bool(existing)

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

    async def cancel(self, session_id: str) -> None:
        pass

    async def close_session(self, session_id: str) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def structural_contract(adapter: ThirdPartyAdapter) -> CodingHarnessAdapter:
    return adapter
