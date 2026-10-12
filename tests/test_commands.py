import pytest
from conftest import advance

from development_harness.commands import Commands
from development_harness.domain import HarnessError, Stage


async def test_issue_create_idempotency_and_scoped_agent_messages(engine):
    commands = Commands(engine)
    args = {"title": "Feature", "description": "Do it", "acceptance": "It works", "priority": 2}
    issue = await commands.invoke("issue create", args, "create-a")
    assert await commands.invoke("issue create", args, "create-a") == issue
    assert len(await engine.tracker.list()) == 1
    agent = engine.create_agent("developer", "codex")
    with pytest.raises(HarnessError) as error:
        await commands.invoke("agent send", {"id": agent["id"], "message": "Implement"})
    assert error.value.code == "unassigned_agent"


async def test_answer_resumes_issue_without_resuming_global_usage_pause(engine):
    issue = await engine.tracker.create(key="guidance")
    engine.ensure_pool()
    engine.store.set_execution_state(True)
    await engine.sync_issues()
    await advance(engine, lambda: engine.store.require("execution", issue["id"])["stage"] == Stage.DEVELOPING)
    engine.pause_issue(issue["id"], "guidance", "Need a decision")
    request = engine.store.pending_requests(issue["id"])[0]
    await engine.pause_project("provider_usage_exhausted", "Usage exhausted")
    await Commands(engine).invoke("request respond", {"id": request["id"], "response": "Use JSON"})
    assert not engine.store.require("execution", issue["id"])["paused"]
    assert not engine.store.execution_state()["enabled"]


async def test_cancel_retains_uncommitted_work_and_releases_locks(engine):
    issue = await engine.tracker.create(key="cancel")
    engine.ensure_pool()
    engine.store.set_execution_state(True)
    await engine.sync_issues()
    await advance(engine, lambda: engine.store.require("execution", issue["id"])["stage"] == Stage.DEVELOPING)
    path = engine.backend.path(issue["id"]) / "partial.txt"
    path.write_text("uncommitted work")
    result = await Commands(engine).invoke("execution cancel", {"id": issue["id"]})
    assert result["stage"] == "cancelled" and result["archive_retained"]
    assert path.read_text() == "uncommitted work"
    assert all(a.get("issue_id") is None for a in engine.agents())
    assert (await engine.tracker.read(issue["id"]))["status"] != "closed"


async def test_interrupted_create_is_reconciled_without_duplicate_issue(engine):
    commands = Commands(engine)
    args = {"title": "A", "description": "B", "acceptance": "C", "priority": 2}
    engine.store.begin_command("interrupted", "issue create", args)
    first = await commands.dispatch("issue create", args, "interrupted")
    result = await commands.invoke("command reconcile", {"idempotency_key": "interrupted"})
    assert result == first and len(await engine.tracker.list()) == 1
    assert (await commands.invoke("command inspect", {"idempotency_key": "interrupted"}))["state"] == "done"


async def test_config_update_rejects_provider_typo_without_saving_or_assigning(engine):
    engine.config.save(engine.root)
    path = engine.root / ".harness/config.json"
    saved = path.read_bytes()
    original = engine.config
    with pytest.raises(HarnessError) as error:
        await Commands(engine).invoke("config update", {"changes": {"profiles": {
            "codex": {"provider": "codxe", "command": ["unused"]},
        }}})
    assert error.value.code == "unknown_provider"
    assert engine.config is original and path.read_bytes() == saved
    assert engine.agents() == [] and engine.executions() == []
    assert not engine.tasks and not engine.adapters
