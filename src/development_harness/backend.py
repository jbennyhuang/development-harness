from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Protocol

from .config import Config, Profile
from .domain import HarnessError
from .git import Git
from .process import run


class Backend(Protocol):
    async def prepare(self, id: str, bundle: Path, branch: str) -> None: ...
    async def export(self, id: str) -> Path: ...
    async def validate(self, id: str, bundle: Path) -> list[dict]: ...
    async def refresh(self, id: str, bundle: Path) -> None: ...
    async def quiesce(self, id: str) -> None: ...
    async def cleanup(self, id: str) -> None: ...
    def agent_command(self, id: str, profile: Profile, env: dict[str, str]) -> list[str]: ...


class Docker:
    """Containers contain isolated Git repositories; no host directory is mounted."""

    def __init__(self, root: Path, config: Config):
        self.root, self.config = root, config
        self.project = hashlib.sha256(str(root).encode()).hexdigest()[:12]

    def name(self, id: str) -> str:
        return "harness-" + self.project + "-" + Git.slug(id)

    async def inspect(self, name: str) -> dict | None:
        code, out, _ = await run(["docker", "inspect", name], check=False)
        if code:
            return None
        return json.loads(out)[0]

    async def create(self, name: str) -> None:
        await run(["docker", "run", "--detach", "--name", name,
                   "--label", "harness.project=" + self.project,
                   "--cpus", str(self.config.container_cpus), "--memory", self.config.container_memory,
                   "--pids-limit", "512", "--security-opt", "no-new-privileges",
                   "--cap-drop", "NET_RAW", "--workdir", "/workspace",
                   self.config.image, "sleep", "infinity"], timeout=self.config.command_timeout)

    async def exec(self, name: str, args: list[str], check: bool = True) -> tuple[int, str, str]:
        try:
            return await run(["docker", "exec", "--workdir", "/workspace", name, *args],
                             timeout=self.config.command_timeout, check=check,
                             max_output=self.config.max_log_bytes)
        except BaseException:
            # Terminating docker exec alone does not guarantee the inner command stopped.
            await run(["docker", "stop", "--time", "2", name], check=False)
            raise

    async def prepare(self, id: str, bundle: Path, branch: str) -> None:
        name = self.name(id)
        container = await self.inspect(name)
        if container:
            if container.get("Config", {}).get("Labels", {}).get("harness.project") != self.project:
                raise HarnessError("unowned_container", "Container is owned by another project")
            if not container["State"]["Running"]:
                await run(["docker", "start", name])
            code, _, _ = await self.exec(name, ["git", "rev-parse", "--verify", "HEAD"], check=False)
            if not code:
                return
            # Clone may have been interrupted; remove only this verified project-owned container.
            await self.remove(name)
        await self.create(name)
        await run(["docker", "cp", str(bundle), name + ":/tmp/seed.bundle"])
        await self.exec(name, ["git", "clone", "/tmp/seed.bundle", "/workspace"])
        await self.exec(name, ["git", "checkout", branch])
        await self.exec(name, ["git", "config", "user.name", "Development Harness"])
        await self.exec(name, ["git", "config", "user.email", "harness@localhost"])
        await self.exec(name, ["git", "config", "core.hooksPath", "/dev/null"])

    async def credentials(self, id: str, profile: Profile) -> None:
        for source, destination in profile.credential_files.items():
            path = Path(source).expanduser().resolve()
            target = PurePosixPath(destination)
            if not path.is_file() or not target.is_absolute() or ".." in target.parts:
                raise HarnessError("invalid_credential_file", "Configured credential source/destination is invalid")
            if not str(target).startswith("/root/"):
                raise HarnessError("invalid_credential_file", "Credential copies must stay under the container user's home")
            await self.exec(self.name(id), ["mkdir", "-p", str(target.parent)])
            await run(["docker", "cp", str(path), self.name(id) + ":" + str(target)])
            await self.exec(self.name(id), ["chmod", "600", str(target)])

    async def export(self, id: str) -> Path:
        name = self.name(id)
        code, out, _ = await self.exec(name, ["git", "status", "--porcelain"], check=False)
        if code or out.strip():
            raise HarnessError("uncommitted_candidate", "Worker must commit its changes before submitting")
        await self.exec(name, ["git", "bundle", "create", "/tmp/candidate.bundle", "HEAD"])
        folder = self.root / ".harness" / "artifacts" / Git.slug(id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "candidate.bundle"
        await run(["docker", "cp", name + ":/tmp/candidate.bundle", str(path)])
        return path

    async def refresh(self, id: str, bundle: Path) -> None:
        name = self.name(id)
        await run(["docker", "cp", str(bundle), name + ":/tmp/target.bundle"])
        # Only transfer refs; the reviewer performs merge-base reconciliation, rebase and squash.
        await self.exec(name, ["git", "fetch", "/tmp/target.bundle",
                              "+refs/heads/*:refs/remotes/harness/*",
                              "+refs/remotes/*:refs/remotes/upstream/*"])

    async def validate(self, id: str, bundle: Path) -> list[dict]:
        name = self.name(id) + "-validation"
        old = await self.inspect(name)
        if old:
            await self.remove(name)
        await self.create(name)
        results = []
        try:
            await run(["docker", "cp", str(bundle), name + ":/tmp/candidate.bundle"])
            await self.exec(name, ["git", "clone", "--branch", Git(self.root).branch(id),
                                   "/tmp/candidate.bundle", "/workspace"])
            for argv in self.config.validation_commands:
                code, out, err = await self.exec(name, argv, check=False)
                results.append({"command": argv, "returncode": code, "stdout": out, "stderr": err})
                if code:
                    break
            return results
        finally:
            await self.remove(name)

    def agent_command(self, id: str, profile: Profile, env: dict[str, str]) -> list[str]:
        # Provider startup must not evaluate an issue's untrusted mise configuration.
        # Use the pinned image tools directly; this also repairs resumed containers from
        # the initial image which placed mise shims on PATH.
        argv = ["docker", "exec", "-i", "--workdir", "/workspace", "--env",
                "PATH=/opt/harness/bin:/root/.local/share/mise/installs/node/24.21.0/bin:/usr/local/bin:/usr/bin:/bin"]
        for key in (*profile.env_names, *env.keys()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise HarnessError("invalid_environment", "Environment names must be identifiers")
            if key in env or key in os.environ:
                # Values are passed through the subprocess environment, not command arguments.
                argv += ["--env", key]
        return [*argv, self.name(id), *profile.command]

    async def quiesce(self, id: str) -> None:
        name = self.name(id)
        if await self.inspect(name):
            await run(["docker", "stop", "--time", "3", name], check=False)
        validation = name + "-validation"
        if await self.inspect(validation):
            await self.remove(validation)

    async def remove(self, name: str) -> None:
        container = await self.inspect(name)
        if container and container.get("Config", {}).get("Labels", {}).get("harness.project") != self.project:
            raise HarnessError("unowned_container", "Refusing to delete a container owned by another project")
        if container:
            await run(["docker", "rm", "--force", name])

    async def cleanup(self, id: str) -> None:
        await self.remove(self.name(id))
        await self.remove(self.name(id) + "-validation")
