from __future__ import annotations

import hashlib
from pathlib import Path

from .domain import HarnessError
from .process import run


class Git:
    def __init__(self, root: Path, target: str = "main", remote: str | None = "origin"):
        self.root, self.target, self.remote = root, target, remote
        self.state = root / ".harness"
        self.workspaces = self.state / "workspaces"
        self.artifacts = self.state / "artifacts"

    @staticmethod
    def slug(id: str) -> str:
        return hashlib.sha256(id.encode()).hexdigest()[:16]

    def workspace(self, id: str) -> Path:
        path = (self.workspaces / self.slug(id)).resolve()
        if not path.is_relative_to(self.workspaces.resolve()):
            raise HarnessError("unsafe_path", "Workspace escaped the project")
        return path

    def branch(self, id: str) -> str:
        return "harness/" + self.slug(id)

    async def call(self, *args: str, cwd: Path | None = None, check: bool = True) -> tuple[int, str, str]:
        return await run(["git", "-c", "core.hooksPath=", "-C", str(cwd or self.root), *args],
                         check=check)

    async def oid(self, ref: str, cwd: Path | None = None) -> str:
        _, out, _ = await self.call("rev-parse", "--verify", ref + "^{commit}", cwd=cwd)
        return out.strip()

    async def initialized(self) -> bool:
        code, out, _ = await self.call("rev-parse", "--show-toplevel", check=False)
        return code == 0 and Path(out.strip()).resolve() == self.root.resolve()

    async def initialize(self, consent: bool) -> None:
        if not await self.initialized():
            if not consent:
                raise HarnessError("git_initialization_required", "Ask the developer before Git initialization")
            await self.call("init", "-b", self.target)
        ignore = self.root / ".gitignore"
        current = ignore.read_text() if ignore.exists() else ""
        if ".harness/" not in current.splitlines():
            ignore.write_text(current.rstrip() + "\n.harness/\n")

    async def latest(self) -> dict:
        """Fetch remote state without changing any developer checkout or refs."""
        local = await self.oid("refs/heads/" + self.target)
        remote_oid = None
        if self.remote:
            code, _, _ = await self.call("remote", "get-url", self.remote, check=False)
            if code == 0:
                await self.call("fetch", "--no-tags", self.remote,
                                f"refs/heads/{self.target}:refs/remotes/{self.remote}/{self.target}")
                remote_oid = await self.oid(f"refs/remotes/{self.remote}/{self.target}")
        return {"local": local, "remote": remote_oid, "target": self.target}

    async def prepare(self, id: str) -> dict:
        self.workspaces.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        path, branch = self.workspace(id), self.branch(id)
        if not path.exists():
            code, _, _ = await self.call("show-ref", "--verify", "refs/heads/" + branch, check=False)
            args = ["worktree", "add"]
            if code:
                args += ["-b", branch, str(path), "refs/heads/" + self.target]
            else:
                args += [str(path), branch]
            await self.call(*args)
        return {"path": str(path), "branch": branch, "commit": await self.oid("HEAD", path)}

    async def bundle(self, id: str, name: str = "seed", extra_refs: list[str] | None = None) -> Path:
        folder = self.artifacts / self.slug(id)
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (name + ".bundle")
        # Git refuses to overwrite an existing bundle. Only this owned artifact is replaced.
        if destination.exists():
            destination.unlink()
        refs = [self.branch(id), *(extra_refs or [])]
        await self.call("bundle", "create", str(destination), *refs)
        return destination

    async def import_candidate(self, id: str, bundle: Path) -> str:
        ref = "refs/harness/candidates/" + self.slug(id)
        await self.call("bundle", "verify", str(bundle))
        await self.call("fetch", "--no-tags", str(bundle), "+HEAD:" + ref)
        oid = await self.oid(ref)
        # Reset only the harness-owned worktree, never the developer's checkout.
        path = self.workspace(id)
        if not path.is_relative_to(self.workspaces.resolve()):
            raise HarnessError("unsafe_path", "Refusing to update an unowned worktree")
        await self.call("reset", "--hard", oid, cwd=path)
        return oid

    async def ancestor(self, before: str, after: str) -> bool:
        code, _, _ = await self.call("merge-base", "--is-ancestor", before, after, check=False)
        return code == 0

    async def validate_merge_candidate(self, candidate: str, base: str, target: dict) -> None:
        actual = await self.oid(candidate)
        parent = await self.oid(candidate + "^")
        if actual != candidate or parent != base:
            raise HarnessError("invalid_candidate", "Squash candidate must have the reported base as its parent")
        _, parents, _ = await self.call("rev-list", "--parents", "-n", "1", candidate)
        if len(parents.split()) != 2:
            raise HarnessError("invalid_candidate", "The final issue commit must have exactly one parent")
        for oid in (target["local"], target.get("remote")):
            if oid and not await self.ancestor(oid, base):
                raise HarnessError("invalid_base", "Candidate base omits local or remote target history")
        local, remote = target["local"], target.get("remote")
        if not remote or await self.ancestor(remote, local):
            expected_base = local
        elif await self.ancestor(local, remote):
            expected_base = remote
        else:
            expected_base = None
            _, parents, _ = await self.call("rev-list", "--parents", "-n", "1", base)
            if set(parents.split()[1:]) != {local, remote} or len(parents.split()) != 3:
                raise HarnessError("invalid_base", "Divergent targets require a base merging exactly local and remote tips")
        if expected_base and base != expected_base:
            raise HarnessError("invalid_base", "Squash base must be the latest target tip, without extra issue commits")

    async def target_checkout(self) -> Path | None:
        _, out, _ = await self.call("worktree", "list", "--porcelain", "-z")
        # -z fields and records are both NUL separated.
        path = None
        for line in out.split("\0"):
            if line.startswith("worktree "):
                path = Path(line[9:])
            elif line == "branch refs/heads/" + self.target:
                return path
        return None

    async def integrate(self, candidate: str, expected: str) -> None:
        current = await self.oid("refs/heads/" + self.target)
        if current != expected:
            raise HarnessError("target_moved", "Target branch moved; redo preparation")
        if not await self.ancestor(expected, candidate):
            raise HarnessError("invalid_candidate", "Candidate would discard target history")
        checkout = await self.target_checkout()
        if checkout:
            _, dirty, _ = await self.call("status", "--porcelain", "--untracked-files=normal", cwd=checkout)
            if dirty:
                raise HarnessError("dirty_target", "Commit or move your working changes before integration; "
                                   "the harness will not stash, reset, or overwrite them")
            # Git protects the checkout and performs its own locked ref update.
            await self.call("merge", "--ff-only", "--no-edit", candidate, cwd=checkout)
        else:
            await self.call("update-ref", "-m", "harness validated integration",
                            "refs/heads/" + self.target, candidate, expected)
        if await self.oid("refs/heads/" + self.target) != candidate:
            raise HarnessError("integration_uncertain", "Target changed during integration; reconciliation required")

    async def cleanup(self, id: str) -> None:
        path, branch = self.workspace(id), self.branch(id)
        if path.exists():
            await self.call("worktree", "remove", "--force", str(path))
        code, _, _ = await self.call("show-ref", "--verify", "refs/heads/" + branch, check=False)
        if code == 0:
            await self.call("branch", "-D", branch)
        await self.call("update-ref", "-d", "refs/harness/candidates/" + self.slug(id))

    async def snapshot(self, id: str) -> dict:
        path = self.workspace(id)
        _, status, _ = await self.call("status", "--porcelain", cwd=path)
        return {"commit": await self.oid("HEAD", path), "dirty": bool(status)}
