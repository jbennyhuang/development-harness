from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HarnessError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.details = code, details

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class Stage(StrEnum):
    READY = "ready"
    PREPARING = "preparing"
    DEVELOPING = "developing"
    VALIDATING = "validating"
    REVIEW_QUEUED = "review_queued"
    REVIEWING = "reviewing"
    INTEGRATION_QUEUED = "integration_queued"
    PREPARING_MERGE = "preparing_merge"
    MERGE_VALIDATING = "merge_validating"
    INTEGRATING = "integrating"
    MERGED = "merged"
    RESOLVING = "resolving"
    CLEANUP = "cleanup"
    DONE = "done"
    CANCELLED = "cancelled"


ALLOWED: dict[Stage, set[Stage]] = {
    Stage.READY: {Stage.PREPARING},
    Stage.PREPARING: {Stage.DEVELOPING},
    Stage.DEVELOPING: {Stage.VALIDATING},
    Stage.VALIDATING: {Stage.DEVELOPING, Stage.REVIEW_QUEUED},
    Stage.REVIEW_QUEUED: {Stage.REVIEWING, Stage.DEVELOPING},
    Stage.REVIEWING: {Stage.DEVELOPING, Stage.INTEGRATION_QUEUED},
    Stage.INTEGRATION_QUEUED: {Stage.PREPARING_MERGE, Stage.DEVELOPING},
    Stage.PREPARING_MERGE: {Stage.DEVELOPING, Stage.MERGE_VALIDATING},
    Stage.MERGE_VALIDATING: {Stage.DEVELOPING, Stage.INTEGRATING},
    Stage.INTEGRATING: {Stage.PREPARING_MERGE, Stage.DEVELOPING, Stage.MERGED},
    Stage.MERGED: {Stage.RESOLVING},
    Stage.RESOLVING: {Stage.CLEANUP},
    Stage.CLEANUP: {Stage.DONE},
    Stage.DONE: set(),
    Stage.CANCELLED: set(),
}


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["development", "review", "merge", "guidance"]
    verdict: Literal["approved", "changes_requested", "blocked"] | None = None
    commit: str | None = None
    base: str | None = None
    summary: str = Field(min_length=1, max_length=100_000)
    conflict: bool = False


def check_transition(before: str, after: str) -> None:
    if Stage(after) not in ALLOWED[Stage(before)]:
        raise HarnessError("invalid_transition", f"Cannot transition {before} to {after}")
