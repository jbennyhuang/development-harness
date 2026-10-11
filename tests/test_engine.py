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
