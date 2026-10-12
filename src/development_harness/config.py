from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__

STATE_DIR = ".harness"


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = "generic"
    command: list[str] = Field(min_length=1)
    env_names: list[str] = Field(default_factory=list)
    mode: str | None = None
    model: str | None = None
    auth_method: str | None = None
    credential_files: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def registered_provider(self) -> Profile:
        from .adapters import resolve_adapter_factory
        resolve_adapter_factory(self.provider)
        return self


def default_profiles() -> dict[str, Profile]:
    # Import after Profile is defined so providers can own their launch defaults.
    from .adapters.antigravity import default_profile as agy_profile
    from .adapters.codex import default_profile as codex_profile
    return {"codex": codex_profile(), "agy": agy_profile()}


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_branch: str = "main"
    remote: str | None = "origin"
    image: str = f"development-harness-worker:{__version__}"
    max_agents: int = Field(default=4, ge=2, le=128)
    developers: int = Field(default=2, ge=1)
    reviewers: int = Field(default=2, ge=1)
    max_review_queue: int = Field(default=4, ge=1)
    max_revision_cycles: int = Field(default=5, ge=1)
    max_failures: int = Field(default=3, ge=1)
    command_timeout: int = Field(default=900, ge=1)
    agent_timeout: int = Field(default=1800, ge=1)
    poll_interval: int = Field(default=10, ge=1)
    reconcile_interval: int = Field(default=5, ge=1)
    max_log_bytes: int = Field(default=2_000_000, ge=1024)
    container_cpus: float = Field(default=2, gt=0)
    container_memory: str = "4g"
    bd_command: list[str] = Field(default_factory=lambda: ["bd"])
    tool_paths: list[str] = Field(default_factory=list)
    validation_commands: list[list[str]] = Field(default_factory=list)
    profiles: dict[str, Profile] = Field(
        default_factory=default_profiles
    )

    @model_validator(mode="after")
    def valid_pools(self) -> Config:
        if self.developers + self.reviewers > self.max_agents:
            raise ValueError("Developer and reviewer targets exceed max_agents")
        if any(not command or not all(isinstance(x, str) and x for x in command)
               for command in self.validation_commands):
            raise ValueError("Validation commands must be non-empty argv arrays")
        if not self.target_branch or self.target_branch.startswith("-"):
            raise ValueError("Invalid target branch")
        return self

    @classmethod
    def load(cls, root: Path) -> Config:
        path = root / STATE_DIR / "config.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        if "HARNESS_MAX_AGENTS" in os.environ:
            cap = int(os.environ["HARNESS_MAX_AGENTS"])
            data["max_agents"] = cap
            # Preserve at least one reviewer when a smaller override is supplied.
            data["reviewers"] = min(data.get("reviewers", 2), max(1, cap // 2))
            data["developers"] = min(data.get("developers", 2), cap - data["reviewers"])
        return cls.model_validate(data)

    def save(self, root: Path) -> None:
        path = root / STATE_DIR / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(self.model_dump_json(indent=2) + "\n")
        temp.replace(path)


def project_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Project directory does not exist: {root}")
    return root
