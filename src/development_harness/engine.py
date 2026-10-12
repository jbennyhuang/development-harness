from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import AsyncContextManager

from acp import Agent
from acp.schema import InitializeResponse, TextContentBlock

from .adapters import (
    AdapterFactory,
    LaunchContext,
    close_session,
    initialize,
    resolve_adapter_factory,
    session,
    usage_exhausted,
)
from .backend import Backend, Docker
from .config import Config
from .domain import HarnessError, Result, Stage
from .git import Git
from .instructions import worker_prompt
from .json_types import (
    JSONObject,
    JSONValue,
    array,
    integer,
    json_object,
    json_value,
    number,
    optional_object,
    optional_string,
    string,
)
from .store import Store
from .tracker import Beads, Tracker, revision

TERMINAL = {Stage.DONE, Stage.CANCELLED}
POST_MERGE = {Stage.MERGED, Stage.RESOLVING, Stage.CLEANUP}


class Engine:
    def __init__(self, root: Path, config: Config, store: Store, tracker: Tracker | None = None,
                 backend: Backend | None = None, git: Git | None = None, adapter_factory: AdapterFactory | None = None) -> None:
        self.root, self.config, self.store = root, config, store
        self.tracker = tracker or Beads(root, config.bd_command)
        self.backend = backend or Docker(root, config)
        self.git = git or Git(root, config.target_branch, config.remote)
        self.adapter_factory = adapter_factory
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.adapters: dict[str, Agent] = {}
        self.integration_issue: str | None = None
        self.worker_url = ""
        self.stopping = False
        self.last_sync = 0.0
        self.command_lock = asyncio.Lock()
        self.sync_lock = asyncio.Lock()
        self.control_tasks: set[asyncio.Task[JSONObject]] = set()

    def executions(self) -> list[JSONObject]:
        return self.store.list("execution")

    def agents(self) -> list[JSONObject]:
        return self.store.list("agent")

    def save_execution(self, e: JSONObject) -> None:
        e["updated_at"] = time.time()
        self.store.put("execution", string(e["id"]), e)

    def available(self, role: str) -> JSONObject | None:
        return next((a for a in self.agents()
                     if string(a["role"]) == role and string(a["state"]) == "idle" and not optional_string(a.get("issue_id"))), None)

    def create_agent(self, role: str, profile: str, id: str | None = None) -> JSONObject:
        if role not in ("developer", "reviewer") or profile not in self.config.profiles:
            raise HarnessError("invalid_agent", "Unknown role or profile")
        resolve_adapter_factory(self.config.profiles[profile].provider)
        active = [a for a in self.agents() if string(a["state"]) != "stopped"]
        if len(active) >= self.config.max_agents:
            raise HarnessError("agent_limit", "Configured managed-agent cap reached")
        target = self.config.developers if role == "developer" else self.config.reviewers
        if sum(string(a["role"]) == role for a in active) >= target:
            raise HarnessError("role_limit", "Configured role capacity reached")
        id = id or role + "-" + uuid.uuid4().hex[:10]
        if self.store.get("agent", id):
            raise HarnessError("already_exists", "Agent identity already exists")
        a: JSONObject = {"id": id, "role": role, "profile": profile, "state": "idle",
             "issue_id": None, "session_id": None, "messages": [], "created_at": time.time()}
        self.store.put("agent", id, a)
        self.store.emit("agent.created", {"role": role, "profile": profile}, agent_id=id)
        return a

    def validate_providers(self) -> None:
        for profile in self.config.profiles.values():
            resolve_adapter_factory(profile.provider)

    def ensure_pool(self) -> None:
        self.validate_providers()
        for role, count in (("developer", self.config.developers), ("reviewer", self.config.reviewers)):
            profile = "codex" if role == "developer" else "agy"
            if profile not in self.config.profiles:
                profile = next(iter(self.config.profiles))
            current = sum(string(a["role"]) == role and string(a["state"]) != "stopped" for a in self.agents())
            for _ in range(count - current):
                self.create_agent(role, profile)

    def bind(self, a: JSONObject, e: JSONObject) -> None:
        profile = self.config.profiles.get(string(a["profile"]))
        if profile is None:
            raise HarnessError("invalid_agent", "Unknown agent profile")
        resolve_adapter_factory(profile.provider)
        if optional_string(a.get("issue_id")) and string(a["issue_id"]) != string(e["id"]):
            raise HarnessError("agent_locked", "Agent is already locked to another issue")
        with self.store.transaction():
            a.update(issue_id=string(e["id"]), state="assigned_waiting")
            e[string(a["role"])] = string(a["id"])
            self.store.put("agent", string(a["id"]), a)
            self.save_execution(e)
            self.store.emit("agent.assigned", {"role": string(a["role"])}, string(e["id"]), string(a["id"]))

    def pause_issue(self, id: str, reason: str, message: str, **details: JSONValue) -> JSONObject:
        e = self.store.require("execution", id)
        e.update(paused=True, pause_reason=reason)
        self.save_execution(e)
        self.store.request(id, {"reason": reason, "message": message, **details})
        self.store.emit("issue.paused", {"reason": reason}, id)
        if self.integration_issue == id:
            self.integration_issue = None
        return e

    async def interrupt(self, id: str) -> None:
        task = self.tasks.get(id)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for a in self.agents():
            if optional_string(a.get("issue_id")) == id and string(a["id"]) in self.adapters:
                adapter = self.adapters[string(a["id"])]
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(adapter.cancel(session_id=optional_string(a.get("session_id")) or ""), 5)
        await self.backend.quiesce(id)

    async def pause_project(self, reason: str, message: str) -> JSONObject:
        self.store.set_execution_state(False, reason)
        if reason == "provider_usage_exhausted":
            self.store.request(None, {"reason": reason, "message": message})
        current = asyncio.current_task()
        ids = [id for id, task in self.tasks.items() if task is not current]
        for id in ids:
            self.tasks[id].cancel()
        await asyncio.gather(*(self.tasks[id] for id in ids), return_exceptions=True)
        # Stop containers as well as client processes; background commands must not keep running.
        for e in self.executions():
            if string(e["stage"]) not in TERMINAL and string(e["stage"]) not in POST_MERGE:
                await self.backend.quiesce(string(e["id"]))
        self.integration_issue = None
        return self.store.execution_state()

    async def recover(self) -> None:
        """No live worker from an earlier service instance may retain write access."""
        for e in self.executions():
            if string(e["stage"]) in TERMINAL:
                continue
            await self.backend.quiesce(string(e["id"]))
            e["generation"] = integer(e.get("generation", 0)) + 1
            self.save_execution(e)
        for a in self.agents():
            if optional_string(a.get("issue_id")):
                a.update(state="assigned_waiting", credential_hash=None, result=None)
                self.store.put("agent", string(a["id"]), a)
        for op in self.store.pending_operations():
            if op["kind"] == "integrate":
                e = self.store.require("execution", string(op["issue_id"]))
                candidate = string(json_object(op["data"])["candidate"])
                if await self.git.ancestor(candidate, await self.git.oid("refs/heads/" + self.config.target_branch)):
                    if string(e["stage"]) == Stage.INTEGRATING:
                        self.store.transition(string(e["id"]), Stage.MERGED, merged_commit=candidate)
                    self.store.complete_operation(string(op["id"]))
        self.store.emit("project.recovered", {"native_sessions": "capability dependent"})

    async def sync_issues(self) -> None:
        async with self.sync_lock:
            await self._sync_issues()

    async def _sync_issues(self) -> None:
        issues = await self.tracker.list()
        ready_ids = {string(i["id"]) for i in await self.tracker.ready()}
        for issue in issues:
            id = string(issue["id"])
            old = self.store.get("issue", id)
            self.store.put("issue", id, issue)
            e = self.store.get("execution", id)
            if e and string(e["stage"]) not in TERMINAL and string(e["stage"]) not in POST_MERGE:
                if issue.get("status") == "closed":
                    self.pause_issue(id, "externally_closed", "Issue closed outside the harness; inspect or cancel it")
                elif old and revision(old) != revision(issue):
                    self.pause_issue(id, "specification_changed", "Requirements changed; invalidating active work")
                    await self.interrupt(id)
                    e = self.store.require("execution", id)
                    e["specification"] = revision(issue)
                    e["approval"] = None
                    e["feedback"] = "Issue specification changed. Implement and review the current acceptance criteria."
                    self.save_execution(e)
                    if optional_string(e.get("developer")) and string(e["stage"]) not in (Stage.PREPARING, Stage.DEVELOPING):
                        self.store.transition(id, Stage.DEVELOPING, approval=None)
                    self.store.emit("issue.specification_changed", {"revision": string(e["specification"])}, id)
            elif not e and id in ready_ids and issue.get("issue_type", "task") != "epic":
                if not string(issue.get("acceptance_criteria", "")).strip():
                    continue
                self.save_execution({"id": id, "stage": Stage.READY, "paused": False,
                                     "generation": 0, "revision_cycles": 0, "failures": 0,
                                     "specification": revision(issue), "created_at": time.time(),
                                     "priority": integer(issue.get("priority", 2)), "branch": self.git.branch(id)})
        self.last_sync = time.monotonic()

    async def tick(self) -> None:
        self.validate_providers()
        for id, task in list(self.tasks.items()):
            if task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    error = task.exception()
                    if error:
                        self.store.emit("execution.unexpected_error", {"message": str(error)}, id)
                self.tasks.pop(id)
        enabled = self.store.execution_state()["enabled"]
        if enabled and time.monotonic() - self.last_sync >= self.config.reconcile_interval:
            await self.sync_issues()
        queued = sum(string(e["stage"]) == Stage.REVIEW_QUEUED for e in self.executions())
        ordered = sorted(self.executions(), key=lambda e: (integer(e.get("priority", 2)), number(e["created_at"]), string(e["id"])))
        for e in ordered:
            id, stage = string(e["id"]), Stage(string(e["stage"]))
            if id in self.tasks or e.get("paused") or stage in TERMINAL:
                continue
            if not enabled and stage not in POST_MERGE:
                continue
            if stage == Stage.READY:
                if queued >= self.config.max_review_queue:
                    continue
                a = self.available("developer")
                if not a:
                    continue
                self.bind(a, e)
                self.store.transition(id, Stage.PREPARING)
            elif stage == Stage.REVIEW_QUEUED:
                if not optional_string(e.get("reviewer")):
                    a = self.available("reviewer")
                    if not a:
                        continue
                    self.bind(a, e)
                self.store.transition(id, Stage.REVIEWING)
            elif stage == Stage.INTEGRATION_QUEUED:
                if self.integration_issue is not None:
                    continue
                self.integration_issue = id
                self.store.transition(id, Stage.PREPARING_MERGE)
            elif stage in (Stage.PREPARING_MERGE, Stage.MERGE_VALIDATING, Stage.INTEGRATING):
                if self.integration_issue not in (None, id):
                    continue
                self.integration_issue = id
            self.tasks[id] = asyncio.create_task(self.step(id))

    async def step(self, id: str) -> None:
        try:
            e = self.store.require("execution", id)
            stage = Stage(string(e["stage"]))
            if stage == Stage.PREPARING:
                work = await self.git.prepare(id)
                bundle = await self.git.bundle(id)
                await self.backend.prepare(id, bundle, string(work["branch"]))
                await self.tracker.update(id, {"status": "in_progress", "assignee": string(e["developer"])})
                self.store.transition(id, Stage.DEVELOPING, **work)
            elif stage in (Stage.DEVELOPING, Stage.REVIEWING, Stage.PREPARING_MERGE):
                if stage == Stage.PREPARING_MERGE:
                    target = await self.git.latest()
                    refs = ["refs/heads/" + self.config.target_branch]
                    if target.get("remote"):
                        refs += [f"refs/remotes/{self.config.remote}/{self.config.target_branch}"]
                    bundle = await self.git.bundle(id, "target", refs)
                    await self.backend.refresh(id, bundle)
                    e["target"] = target
                    self.save_execution(e)
                await self.agent_turn(id, stage)
            elif stage in (Stage.VALIDATING, Stage.MERGE_VALIDATING):
                bundle = await self.git.bundle(id, "validation")
                checks = await self.backend.validate(id, bundle)
                e = self.store.require("execution", id)
                self.store.put("artifact", id + ":validation", {"id": id + ":validation", "checks": json_value(checks)})
                self.store.emit("validation.completed", {"checks": json_value(checks)}, id)
                if not checks or any(integer(c["returncode"]) for c in checks):
                    self.revise(id, "Validation failed: " + json.dumps(checks))
                elif revision(await self.tracker.read(id)) != string(e["specification"]):
                    self.revise(id, "Acceptance criteria changed; develop and review again")
                else:
                    self.store.transition(id, Stage.REVIEW_QUEUED if stage == Stage.VALIDATING else Stage.INTEGRATING)
            elif stage == Stage.INTEGRATING:
                latest = await self.git.latest()
                if latest != json_object(e["target"]):
                    self.store.transition(id, Stage.PREPARING_MERGE, approval=None)
                    return
                if e.get("approval") != {"commit": string(e["candidate"]), "specification": string(e["specification"])}:
                    raise HarnessError("invalid_approval", "Final candidate has no matching approval")
                await self.git.validate_merge_candidate(string(e["candidate"]), string(e["merge_base"]), json_object(e["target"]))
                op = self.store.operation(id, "integrate", {"candidate": string(e["candidate"]), "target": latest})
                if not self.store.execution_state()["enabled"]:
                    return
                await self.git.integrate(string(e["candidate"]), string(latest["local"]))
                self.store.transition(id, Stage.MERGED, merged_commit=string(e["candidate"]))
                self.store.complete_operation(op)
                self.integration_issue = None
            elif stage == Stage.MERGED:
                with self.store.transaction():
                    for role in ("developer", "reviewer"):
                        if e.get(role):
                            a = self.store.require("agent", string(e[role]))
                            if optional_string(a.get("issue_id")) not in (None, id):
                                raise HarnessError("assignment_conflict", "Context reset encountered another issue lock")
                            a.update(issue_id=None, session_id=None, credential_hash=None, result=None,
                                     state="stopped" if a.get("retiring") else "idle", messages=[])
                            self.store.put("agent", string(a["id"]), a)
                            self.store.emit("agent.context_cleared", {"merged_issue": id}, id, string(a["id"]))
                    e["stage"] = Stage.RESOLVING
                    self.save_execution(e)
                    self.store.emit("execution.transition", {"from": Stage.MERGED, "to": Stage.RESOLVING}, id)
            elif stage == Stage.RESOLVING:
                await self.tracker.close(id, "Validated integration " + string(e["merged_commit"]))
                self.store.transition(id, Stage.CLEANUP)
            elif stage == Stage.CLEANUP:
                await self.backend.cleanup(id)
                await self.git.cleanup(id)
                self.store.transition(id, Stage.DONE)
                self.store.emit("issue.resolved", {"commit": string(e["merged_commit"])}, id)
        except asyncio.CancelledError:
            raise
        except HarnessError as exc:
            if exc.code == "provider_usage_exhausted":
                await self.pause_project(exc.code, str(exc))
            elif exc.code == "target_moved":
                self.store.transition(id, Stage.PREPARING_MERGE, approval=None)
            elif exc.code in ("dirty_target", "quiesce_failed"):
                self.pause_issue(id, exc.code, str(exc))
            else:
                self.failure(id, exc)
        except Exception as exc:
            if usage_exhausted(str(exc)):
                await self.pause_project("provider_usage_exhausted", "A provider usage limit was reported")
            else:
                self.failure(id, exc)

    def revise(self, id: str, feedback: str) -> None:
        e = self.store.require("execution", id)
        cycles = integer(e.get("revision_cycles", 0)) + 1
        self.store.transition(id, Stage.DEVELOPING, feedback=feedback, approval=None, revision_cycles=cycles)
        if self.integration_issue == id:
            self.integration_issue = None
        if cycles >= self.config.max_revision_cycles:
            self.pause_issue(id, "revision_limit", "Revision limit reached; inspect findings and decide how to proceed")

    def failure(self, id: str, error: Exception) -> None:
        e = self.store.require("execution", id)
        e["failures"] = integer(e.get("failures", 0)) + 1
        self.save_execution(e)
        self.store.emit("execution.failed", {"message": str(error), "failures": integer(e["failures"]),
                         "error": error.as_dict() if isinstance(error, HarnessError) else
                         {"code": "unexpected_error", "message": str(error)}}, id)
        if integer(e["failures"]) >= self.config.max_failures:
            self.pause_issue(id, "failure_limit", str(error))
        if self.integration_issue == id:
            self.integration_issue = None

    async def agent_turn(self, id: str, stage: Stage) -> None:
        e = self.store.require("execution", id)
        role = "developer" if stage == Stage.DEVELOPING else "reviewer"
        a = self.store.require("agent", string(e[role]))
        profile = self.config.profiles[string(a["profile"])]
        registered_factory = resolve_adapter_factory(profile.provider)
        # A new scoped credential per turn rejects late submissions from obsolete processes.
        token = secrets.token_urlsafe(32)
        e["generation"] = integer(e["generation"]) + 1
        generation = integer(e["generation"])
        a.update(state="assigned_working", attempt=generation,
                 credential_hash=hashlib.sha256(token.encode()).hexdigest(), result=None)
        self.save_execution(e)
        self.store.put("agent", string(a["id"]), a)
        transcript_id = string(a["id"]) + ":" + id
        history = self.store.get("transcript", transcript_id) or {"entries": [], "bytes": 0}
        quota = False

        def emit(data: JSONObject) -> None:
            nonlocal quota
            # Only explicit provider error/notice envelopes can trigger a usage pause.
            update = json_object(data.get("update", {}))
            if data.get("provider_usage_exhausted") or (
                update.get("sessionUpdate") in ("notice", "tool_call_update")
                and update.get("status") == "failed" and usage_exhausted(update)
            ):
                if not quota:
                    quota = True
                    self.store.set_execution_state(False, "provider_usage_exhausted")
                    control = asyncio.create_task(self.pause_project("provider_usage_exhausted", "Provider usage limit reached"))
                    self.control_tasks.add(control)
                    control.add_done_callback(self.control_tasks.discard)
            size = len(json.dumps(data))
            if integer(history["bytes"]) + size <= self.config.max_log_bytes:
                array(history["entries"]).append(data)
                history["bytes"] = integer(history["bytes"]) + size
                self.store.put("transcript", transcript_id, history)
            if update.get("sessionUpdate") == "agent_message_chunk":
                self.store.emit("agent.response", {"session": optional_string(data.get("session_id")), "content": update.get("content")}, id, string(a["id"]))

        factory = self.adapter_factory or registered_factory
        manager: AsyncContextManager[Agent] | None = None
        adapter: Agent | None = None
        capabilities: InitializeResponse | None = None
        session_id: str | None = None
        prompt_active = False
        exit_error: BaseException | None = None
        env = {"HARNESS_WORKER_TOKEN": token, "HARNESS_WORKER_URL": self.worker_url}
        try:
            await self.backend.prepare(id, await self.git.bundle(id), string(e["branch"]))
            manager = factory(profile, emit, LaunchContext(self.backend, id, env))
            adapter = await manager.__aenter__()
            self.adapters[string(a["id"])] = adapter
            capabilities = await initialize(adapter)
            current = self.store.require("agent", string(a["id"]))
            current["capabilities"] = json_object(capabilities.model_dump(by_alias=True, exclude_none=True))
            self.store.put("agent", string(a["id"]), current)
            session_id, restored = await session(adapter, profile, capabilities, "/workspace", optional_string(a.get("session_id")), emit)
            current = self.store.require("agent", string(a["id"]))
            current["session_id"] = session_id
            self.store.put("agent", string(a["id"]), current)
            issue = await self.tracker.read(id)
            if revision(issue) != string(e["specification"]):
                e["specification"] = revision(issue)
                self.save_execution(e)
            text = worker_prompt(e, issue, role, stage, optional_object(e.get("target")),
                                 "" if restored else json.dumps(array(history["entries"])),
                                 array(a.get("messages", [])))
            current = self.store.require("agent", string(a["id"]))
            current["messages"] = array(current["messages"])[len(array(a.get("messages", []))):]
            self.store.put("agent", string(a["id"]), current)
            emit({"role": "user", "text": text})
            try:
                prompt_active = True
                await asyncio.wait_for(adapter.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text=text)]), self.config.agent_timeout)
            except Exception as exc:
                if usage_exhausted(str(exc)):
                    raise HarnessError("provider_usage_exhausted", "Provider usage limit reached") from exc
                raise
            prompt_active = False
            if quota:
                raise HarnessError("provider_usage_exhausted", "Provider usage limit reached")
            current = self.store.require("agent", string(a["id"]))
            result = current.get("result")
            if not result:
                raise HarnessError("missing_submission", "Agent ended its turn without submitting a result")
        except BaseException as exc:
            exit_error = exc
            raise
        finally:
            current = self.store.require("agent", string(a["id"]))
            if optional_string(current.get("issue_id")) == id:
                current.update(state="assigned_waiting", credential_hash=None)
                self.store.put("agent", string(a["id"]), current)

            async def teardown() -> None:
                try:
                    try:
                        if adapter is not None and capabilities is not None and session_id is not None:
                            if prompt_active:
                                with contextlib.suppress(Exception):
                                    await asyncio.wait_for(adapter.cancel(session_id=session_id), 5)
                            with contextlib.suppress(Exception):
                                await close_session(adapter, capabilities, session_id)
                    finally:
                        if manager is not None and adapter is not None:
                            await manager.__aexit__(
                                type(exit_error) if exit_error is not None else None,
                                exit_error, exit_error.__traceback__ if exit_error is not None else None,
                            )
                finally:
                    self.adapters.pop(string(a["id"]), None)
                    await self.backend.quiesce(id)

            # Revoke synchronously above, then finish cleanup even if cancellation repeats.
            # No quota control task is awaited here: it may itself be waiting for this turn.
            cleanup = asyncio.create_task(teardown())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError
        await self.apply_result(id, string(a["id"]), generation, Result.model_validate(result))

    def worker_identity(self, token: str) -> tuple[JSONObject, JSONObject]:
        digest = hashlib.sha256(token.encode()).hexdigest()
        a = next((a for a in self.agents() if a.get("credential_hash") == digest), None)
        if not a or not optional_string(a.get("issue_id")):
            raise HarnessError("unauthorized", "Worker credential is expired or invalid")
        e = self.store.require("execution", string(a["issue_id"]))
        active_stages = {"developer": {Stage.DEVELOPING},
                         "reviewer": {Stage.REVIEWING, Stage.PREPARING_MERGE}}
        if (a.get("attempt") != integer(e["generation"]) or string(a["state"]) != "assigned_working"
                or e.get(string(a["role"])) != string(a["id"])
                or string(e["stage"]) not in active_stages.get(optional_string(a.get("role")) or "", set())):
            raise HarnessError("stale_assignment", "Worker assignment is no longer active")
        if not self.store.execution_state()["enabled"] or e.get("paused"):
            raise HarnessError("execution_paused", "Execution is paused; preserve changes")
        return a, e

    def worker_assignment(self, token: str) -> JSONObject:
        a, e = self.worker_identity(token)
        return {"agent": string(a["id"]), "role": string(a["role"]), "execution": e,
                "issue": self.store.require("issue", string(e["id"]))}

    def submit(self, token: str, result: JSONObject) -> JSONObject:
        a, e = self.worker_identity(token)
        parsed = Result.model_validate(result)
        expected = "development" if string(e["stage"]) == Stage.DEVELOPING else (
            "merge" if string(e["stage"]) == Stage.PREPARING_MERGE else "review")
        if parsed.kind not in (expected, "guidance"):
            raise HarnessError("wrong_role", "Result type does not match the active assignment")
        normalized = json_object(parsed.model_dump())
        if a.get("result") and json_object(a["result"]) != normalized:
            raise HarnessError("already_submitted", "Assignment already has a different result")
        a["result"] = normalized
        self.store.put("agent", string(a["id"]), a)
        return {"accepted": True, "generation": integer(e["generation"])}

    async def apply_result(self, id: str, agent_id: str, generation: int, result: Result) -> None:
        e = self.store.require("execution", id)
        if generation != integer(e["generation"]):
            raise HarnessError("stale_assignment", "Result is from an obsolete assignment")
        if not self.store.execution_state()["enabled"] or e.get("paused"):
            raise HarnessError("execution_paused", "Execution is paused; preserve changes")
        agent = self.store.require("agent", agent_id)
        expected_role = "developer" if string(e["stage"]) == Stage.DEVELOPING else "reviewer"
        if (optional_string(agent.get("issue_id")) != id or optional_string(agent.get("role")) != expected_role
                or e.get(expected_role) != agent_id
                or string(e["stage"]) not in (Stage.DEVELOPING, Stage.REVIEWING, Stage.PREPARING_MERGE)):
            raise HarnessError("stale_assignment", "Result assignment is no longer active")
        self.store.emit("agent.submitted", json_object(result.model_dump()), id, agent_id)
        if result.kind == "guidance" or result.verdict == "blocked":
            self.pause_issue(id, "guidance", result.summary)
            return
        if result.verdict == "changes_requested" or result.conflict:
            self.revise(id, result.summary)
            return
        if not result.commit:
            raise HarnessError("missing_commit", "Submission requires an exact commit")
        bundle = await self.backend.export(id)
        candidate = await self.git.import_candidate(id, bundle)
        if candidate != result.commit:
            raise HarnessError("candidate_mismatch", "Reported commit differs from actual container HEAD")
        issue = await self.tracker.read(id)
        if revision(issue) != string(e["specification"]):
            self.revise(id, "Issue specification changed during the assignment")
            return
        current = self.store.require("execution", id)
        if integer(current["generation"]) != generation or string(current["stage"]) != string(e["stage"]):
            raise HarnessError("stale_assignment", "Assignment changed during export")
        if not self.store.execution_state()["enabled"] or current.get("paused"):
            raise HarnessError("execution_paused", "Execution paused during export; preserve changes")
        if result.kind == "development":
            self.store.transition(id, Stage.VALIDATING, candidate=candidate, feedback=result.summary, approval=None)
        elif result.kind == "review":
            if result.verdict != "approved" or candidate != optional_string(e.get("candidate")):
                raise HarnessError("review_modified_candidate", "Reviewer may not modify the implementation")
            self.store.transition(id, Stage.INTEGRATION_QUEUED, feedback=result.summary)
        elif result.kind == "merge":
            if result.verdict != "approved" or not result.base:
                raise HarnessError("invalid_merge_report", "Merge preparation needs exact base and approval")
            await self.git.validate_merge_candidate(candidate, result.base, json_object(e["target"]))
            self.store.transition(id, Stage.MERGE_VALIDATING, candidate=candidate, merge_base=result.base,
                                  approval={"commit": candidate, "specification": string(e["specification"])})

    async def close(self) -> None:
        self.stopping = True
        await asyncio.gather(*self.control_tasks, return_exceptions=True)
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for e in self.executions():
            if string(e["stage"]) not in TERMINAL and string(e["stage"]) not in POST_MERGE:
                await self.backend.quiesce(string(e["id"]))
