"""Plain construction types for SDK Agent provider factories."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncContextManager

from acp import Agent

from .json_types import JSONObject

if TYPE_CHECKING:
    from .backend import Backend
    from .config import Profile

Update = JSONObject
Emit = Callable[[Update], None]

@dataclass(frozen=True)
class LaunchContext:
    """Prepared issue container; backend retains ownership and shutdown policy."""
    backend: Backend
    issue_id: str
    env: dict[str, str]
    workspace: str = "/workspace"


AdapterFactory = Callable[["Profile", Emit, LaunchContext], AsyncContextManager[Agent]]
