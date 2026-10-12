from __future__ import annotations

from dataclasses import dataclass

from ..backend import Backend


@dataclass(frozen=True)
class LaunchContext:
    """Prepared issue container; backend retains ownership and shutdown policy."""

    backend: Backend
    issue_id: str
    env: dict[str, str]
    workspace: str = "/workspace"

