from __future__ import annotations

from ..config import Profile
from ._acp import ACPAdapter
from ._context import LaunchContext


class CodexAdapter(ACPAdapter):
    """Codex ACP launch with explicitly configured auth files and environment."""

    async def start(self, context: LaunchContext) -> dict:
        self.workspace = context.workspace
        await context.backend.credentials(context.issue_id, self.profile)
        command = context.backend.agent_command(context.issue_id, self.profile, context.env)
        return await self.connect(command, context.env)


def default_profile() -> Profile:
    """Image-installed codex-acp with full permission confined to the issue container."""
    return Profile(provider="codex", command=["codex-acp"],
                   env_names=["OPENAI_API_KEY", "CODEX_API_KEY"], mode="agent-full-access")
