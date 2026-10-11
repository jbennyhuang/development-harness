import asyncio
import hashlib

import pytest
from conftest import advance

from development_harness.domain import HarnessError, Result, Stage


async def start(engine, key="a"):
    issue = await engine.tracker.create(key=key)
    engine.ensure_pool()
    engine.store.set_execution_state(True)
    await engine.sync_issues()
    return issue["id"]


async def test_full_lifecycle_merges_closes_clears_context_and_retains_evidence(engine):
    id = await start(engine)
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == "done")
    e = engine.store.require("execution", id)
    assert await engine.git.oid("main") == e["merged_commit"]
    assert (await engine.tracker.read(id))["status"] == "closed"
    assert engine.backend.validations == 2
    assert all(a["state"] == "idle" and a["issue_id"] is None and a["session_id"] is None for a in engine.agents())
    assert not engine.git.workspace(id).exists()
    assert id in engine.backend.deleted
    assert engine.store.get("transcript", e["developer"] + ":" + id)
    assert engine.store.get("artifact", id + ":validation")


async def test_waiting_developer_stays_locked_until_merge(engine):
    id = await start(engine)
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == "review_queued")
    agent = engine.store.require("agent", engine.store.require("execution", id)["developer"])
    assert agent["issue_id"] == id
    assert engine.available("developer") is None
    await engine.tracker.create(key="b")
    await engine.sync_issues()
    await engine.tick()
    assert engine.store.require("execution", "issue-b")["stage"] == "ready"


async def test_change_request_preserves_both_issue_locks(engine):
    id = await start(engine)
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == "review_queued")
    e = engine.store.require("execution", id)
    engine.bind(engine.available("reviewer"), e)
    engine.store.transition(id, Stage.REVIEWING)
    e = engine.store.require("execution", id)
    await engine.apply_result(id, e["reviewer"], e["generation"], Result(kind="review", verdict="changes_requested", summary="Fix behavior"))
    current = engine.store.require("execution", id)
    assert current["stage"] == "developing"
    assert current["developer"] == e["developer"] and current["reviewer"] == e["reviewer"]
    assert engine.available("reviewer") is None


async def test_stalled_issue_retains_context_and_locks(engine):
    id = await start(engine)
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == "developing")
    engine.pause_issue(id, "guidance", "Which output format?")
    e = engine.store.require("execution", id)
    assert e["paused"] and engine.available("developer") is None
    assert engine.store.pending_requests(id)
    assert engine.store.execution_state()["enabled"]


async def test_global_usage_pause_interrupts_other_work_and_survives_journal(engine):
    id = await start(engine)
    other_cancelled = asyncio.Event()
    async def active():
        try:
            await asyncio.sleep(100)
        finally:
            other_cancelled.set()
    engine.tasks[id] = asyncio.create_task(active())
    await asyncio.sleep(0)
    await engine.pause_project("provider_usage_exhausted", "quota exhausted")
    assert other_cancelled.is_set()
    assert engine.store.execution_state() == {"enabled": False, "reason": "provider_usage_exhausted"}
    assert engine.store.pending_requests()[0]["issue_id"] is None
    assert id in engine.backend.stopped


async def test_scoped_worker_credentials_reject_wrong_role_stale_and_paused(engine):
    id = await start(engine)
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == "developing")
    e = engine.store.require("execution", id)
    a = engine.store.require("agent", e["developer"])
    a.update(state="assigned_working", attempt=e["generation"], credential_hash=hashlib.sha256(b"token").hexdigest())
    engine.store.put("agent", a["id"], a)
    with pytest.raises(HarnessError) as error:
        engine.submit("token", {"kind": "review", "verdict": "approved", "summary": "wrong role"})
    assert error.value.code == "wrong_role"
    e["generation"] += 1
    engine.save_execution(e)
    with pytest.raises(HarnessError) as error:
        engine.worker_assignment("token")
    assert error.value.code == "stale_assignment"


async def test_recovery_recognizes_completed_merge_without_merging_twice(engine):
    id = await start(engine)
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == "integrating")
    e = engine.store.require("execution", id)
    operation = engine.store.operation(id, "integrate", {"candidate": e["candidate"], "target": e["target"]})
    await engine.git.integrate(e["candidate"], e["target"]["local"])
    await engine.recover()
    assert engine.store.require("execution", id)["stage"] == "merged"
    assert not any(x["id"] == operation for x in engine.store.pending_operations())
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == "done")


async def test_completed_merge_finishes_resolution_even_when_dispatch_is_paused(engine):
    id = await start(engine)
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == Stage.MERGED)
    engine.store.set_execution_state(False, "provider_usage_exhausted")
    await advance(engine, lambda: engine.store.require("execution", id)["stage"] == Stage.DONE)
    assert (await engine.tracker.read(id))["status"] == "closed"
    assert not engine.store.execution_state()["enabled"]


@pytest.mark.parametrize("role,stage,valid", [
    ("developer", Stage.DEVELOPING, True),
    ("reviewer", Stage.REVIEWING, True),
    ("reviewer", Stage.PREPARING_MERGE, True),
    *[("developer", stage, False) for stage in Stage if stage != Stage.DEVELOPING],
    *[("reviewer", stage, False) for stage in Stage
      if stage not in (Stage.REVIEWING, Stage.PREPARING_MERGE)],
])
async def test_credentials_fenced_to_active_role_and_stage(engine, role, stage, valid):
    id = await start(engine)
    e = engine.store.require("execution", id)
    a = engine.available(role)
    engine.bind(a, e)
    e = engine.store.require("execution", id)
    e.update(stage=stage, generation=7)
    engine.save_execution(e)
    a.update(state="assigned_working", attempt=7, credential_hash=hashlib.sha256(b"scoped").hexdigest())
    engine.store.put("agent", a["id"], a)
    kind = {Stage.DEVELOPING: "development", Stage.PREPARING_MERGE: "merge"}.get(stage, "review")
    report = {"kind": kind, "summary": "evidence"}
    if valid:
        assert engine.worker_assignment("scoped")["role"] == role
        assert engine.submit("scoped", report)["accepted"]
    else:
        for call in (lambda: engine.worker_assignment("scoped"), lambda: engine.submit("scoped", report)):
            with pytest.raises(HarnessError, match="no longer active"):
                call()


@pytest.mark.parametrize("mutation", ["binding", "role", "generation", "state", "paused", "project"])
async def test_credentials_reject_broken_assignment(engine, mutation):
    id = await start(engine)
    e = engine.store.require("execution", id)
    a = engine.available("developer")
    engine.bind(a, e)
    e = engine.store.require("execution", id)
    e.update(stage=Stage.DEVELOPING, generation=3)
    a.update(state="assigned_working", attempt=3, credential_hash=hashlib.sha256(b"scoped").hexdigest())
    if mutation == "binding":
        e["developer"] = "another-developer"
    elif mutation == "role":
        a["role"] = "reviewer"
    elif mutation == "generation":
        a["attempt"] = 2
    elif mutation == "state":
        a["state"] = "assigned_waiting"
    elif mutation == "paused":
        e["paused"] = True
    else:
        engine.store.set_execution_state(False, "user")
    engine.save_execution(e)
    engine.store.put("agent", a["id"], a)
    with pytest.raises(HarnessError):
        engine.worker_assignment("scoped")
    with pytest.raises(HarnessError):
        engine.submit("scoped", {"kind": "guidance", "summary": "late"})


@pytest.mark.parametrize("outcome", ["submitted", "missing", "provider", "cancel", "quota"])
async def test_turn_revokes_closes_and_quiesces_before_apply_or_retry(engine, outcome):
    from conftest import FixtureAdapter

    id = await start(engine)
    await engine.tick()
    await asyncio.gather(*engine.tasks.values())
    engine.tasks.clear()
    events = []
    entered = asyncio.Event()
    token = None

    class Controlled(FixtureAdapter):
        async def prompt(self, session_id, text, timeout):
            nonlocal token
            token = self.env["HARNESS_WORKER_TOKEN"]
            (engine.backend.path(id) / "uncommitted.txt").write_text("preserved")
            entered.set()
            if outcome == "cancel":
                await asyncio.Event().wait()
            if outcome == "provider":
                raise RuntimeError("provider connection failed")
            if outcome == "quota":
                self.emit({"provider_usage_exhausted": True})
                await asyncio.Event().wait()
            if outcome == "submitted":
                await super().prompt(session_id, text, timeout)

        async def close(self):
            with pytest.raises(HarnessError):
                engine.worker_assignment(token)
            events.append("close")

    original_stop = engine.backend.quiesce
    async def stop(issue):
        events.append("stop")
        await original_stop(issue)
    async def apply(*args):
        assert events == ["close", "stop"]
        events.append("apply")
    engine.backend.quiesce = stop
    engine.apply_result = apply
    engine.adapter_factory = lambda profile, emit: Controlled(engine, profile, emit)
    task = asyncio.create_task(engine.agent_turn(id, Stage.DEVELOPING))
    engine.tasks[id] = task
    await asyncio.wait_for(entered.wait(), 5)
    if outcome == "cancel":
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    await asyncio.wait_for(asyncio.gather(*engine.control_tasks, return_exceptions=True), 5)
    assert events[:2] == ["close", "stop"]
    assert events.count("close") == 1
    assert ("apply" in events) == (outcome == "submitted")
    a = engine.store.require("agent", engine.store.require("execution", id)["developer"])
    assert a["credential_hash"] is None and a["issue_id"] == id
    assert a["session_id"] and engine.store.get("transcript", a["id"] + ":" + id)
    assert (engine.backend.path(id) / "uncommitted.txt").read_text() == "preserved"
    assert bool(a.get("result")) == (outcome == "submitted")


async def test_repeated_cancellation_waits_for_quiescence_before_retry(engine):
    from conftest import FixtureAdapter

    id = await start(engine)
    await engine.tick()
    await asyncio.gather(*engine.tasks.values())
    engine.tasks.clear()
    prompted, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closes = 0

    class Controlled(FixtureAdapter):
        async def prompt(self, *args):
            prompted.set()
            await asyncio.Event().wait()

        async def close(self):
            nonlocal closes
            closes += 1

    async def stop(issue):
        stopping.set()
        await release.wait()
        engine.backend.stopped.append(issue)

    engine.backend.quiesce = stop
    engine.adapter_factory = lambda profile, emit: Controlled(engine, profile, emit)
    task = asyncio.create_task(engine.agent_turn(id, Stage.DEVELOPING))
    await asyncio.wait_for(prompted.wait(), 5)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    a = engine.store.require("agent", engine.store.require("execution", id)["developer"])
    assert a["credential_hash"] is None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert closes == 1 and id in engine.backend.stopped
