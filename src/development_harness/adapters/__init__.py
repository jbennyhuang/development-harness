"""Public structural coding-agent contract and provider registry."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..config import Profile
from ._acp import ACPAdapter, usage_exhausted
from ._context import LaunchContext
from .antigravity import AntigravityAdapter
from .codex import CodexAdapter

Update = dict[str, Any]
Emit = Callable[[Update], None]


class CodingHarnessAdapter(Protocol):
    @property
    def capabilities(self) -> Update: ...
    async def start(self, context: LaunchContext) -> Update: ...
    async def authenticate(self, method_id: str, timeout: int = 60) -> None: ...
    async def session(self, existing: str | None, history: str = "") -> tuple[str, bool]: ...
    async def prompt(self, session_id: str, text: str, timeout: int) -> Update: ...
    async def cancel(self, session_id: str) -> None: ...
    async def close_session(self, session_id: str) -> None: ...
    async def close(self) -> None: ...


AdapterFactory = Callable[[Profile, Emit], CodingHarnessAdapter]


ADAPTERS: dict[str, AdapterFactory] = {
    "generic": ACPAdapter, "codex": CodexAdapter, "agy": AntigravityAdapter,
}

__all__ = ["CodingHarnessAdapter", "LaunchContext", "AdapterFactory", "ADAPTERS", "Emit", "Update",
           "ACPAdapter", "CodexAdapter", "AntigravityAdapter", "usage_exhausted"]
