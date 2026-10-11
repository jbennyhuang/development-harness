from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .domain import HarnessError
from .process import run


class Tracker(Protocol):
    async def create(self, title: str, description: str, acceptance: str, priority: int, key: str) -> dict: ...
    async def read(self, id: str) -> dict: ...
    async def list(self) -> list[dict]: ...
    async def ready(self) -> list[dict]: ...
    async def update(self, id: str, changes: dict) -> dict: ...
    async def close(self, id: str, reason: str) -> dict: ...
    async def delete(self, id: str) -> dict: ...
    async def dependency(self, id: str, depends_on: str, remove: bool = False) -> dict: ...


def revision(issue: dict) -> str:
    import hashlib
    # Assignee/status changes made by the scheduler are not specification revisions.
    fields = {k: issue.get(k) for k in ("title", "description", "acceptance_criteria")}
    fields["dependencies"] = sorted(
        (d.get("id") or d.get("depends_on_id"), d.get("dependency_type") or d.get("type", "blocks"))
        for d in issue.get("dependencies", [])
    )
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


class Beads:
    def __init__(self, root: Path, command: list[str]):
        self.root, self.command = root, command

    async def call(self, *args: str) -> object:
        _, out, _ = await run([*self.command, *args, "--json"], self.root)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise HarnessError("invalid_beads_output", "Beads did not return JSON") from exc

    async def create(self, title: str, description: str, acceptance: str, priority: int, key: str) -> dict:
        # External references allow reconciliation after a lost create response.
        ref = "harness:" + key
        existing = await self.list()
        for issue in existing:
            if issue.get("external_ref") == ref:
                return issue
        result = await self.call("create", "--title", title, "--description", description,
                                 "--acceptance", acceptance, "--priority", str(priority),
                                 "--external-ref", ref)
        return result[0] if isinstance(result, list) else result

    async def read(self, id: str) -> dict:
        result = await self.call("show", id)
        if isinstance(result, list):
            if not result:
                raise HarnessError("not_found", f"Issue {id} not found")
            return result[0]
        return result

    async def list(self) -> list[dict]:
        result = await self.call("list", "--all", "--limit", "0")
        issues = result if isinstance(result, list) else result.get("issues", [])
        # bd list omits dependency objects which bd show includes; use one canonical shape.
        # This also prevents approval invalidation merely because a different command was used.
        return [await self.read(issue["id"]) for issue in issues]

    async def ready(self) -> list[dict]:
        result = await self.call("ready", "--limit", "0")
        return result if isinstance(result, list) else result.get("issues", [])

    async def update(self, id: str, changes: dict) -> dict:
        allowed = {"title": "--title", "description": "--description",
                   "acceptance_criteria": "--acceptance", "priority": "--priority",
                   "status": "--status", "assignee": "--assignee"}
        if set(changes) - allowed.keys():
            raise HarnessError("invalid_issue_update", "Unsupported Beads fields")
        args = ["update", id]
        for key, value in changes.items():
            args.extend([allowed[key], str(value)])
        await self.call(*args)
        return await self.read(id)

    async def close(self, id: str, reason: str) -> dict:
        issue = await self.read(id)
        if issue.get("status") != "closed":
            await self.call("close", id, "--reason", reason)
        return await self.read(id)

    async def delete(self, id: str) -> dict:
        await self.call("delete", id, "--force")
        return {"id": id, "deleted": True}

    async def dependency(self, id: str, depends_on: str, remove: bool = False) -> dict:
        # Beads owns dependency validation and rejects cycles.
        await self.call("dep", "remove" if remove else "add", id, depends_on)
        return await self.read(id)

    async def initialize(self) -> dict:
        if not (self.root / ".beads").exists():
            await run([*self.command, "init", "--server", "--non-interactive", "--skip-agents", "--skip-hooks"], self.root)
        await run([*self.command, "dolt", "start"], self.root)
        return {"beads": "server"}
