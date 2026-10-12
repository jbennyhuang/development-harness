from __future__ import annotations

from typing import override

from ..config import Profile
from . import AcpAgentAdapter, LaunchContext


class Codex(AcpAgentAdapter):
    """Codex ACP launch with explicitly configured auth files and environment."""

    @override
    async def setup(self, context: LaunchContext) -> list[str]:
        await context.backend.credentials(context.issue_id, self.profile)
        command = context.backend.agent_command(context.issue_id, self.profile, context.env)
        return command


def default_profile() -> Profile:
    """Image-installed codex-acp with full permission confined to the issue container."""
    return Profile(provider="codex", command=["codex-acp"],
                   env_names=["OPENAI_API_KEY", "CODEX_API_KEY"], mode="agent-full-access")


CodexAdapter = Codex
