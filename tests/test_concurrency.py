import asyncio

from conftest import FixtureAdapter, advance

from development_harness.domain import Stage


async def test_development_and_review_are_concurrent_across_issues(engine):
    engine.config.max_agents = 4
    engine.config.developers = engine.config.reviewers = 2
    hold = asyncio.Event()
    class Controlled(FixtureAdapter):
        async def prompt(self, session_id, text, timeout):
            assignment = self.engine.worker_assignment(self.env["HARNESS_WORKER_TOKEN"])
            e = assignment["execution"]
            if e["id"] == "issue-b" or e["stage"] == Stage.REVIEWING:
                await hold.wait()
            return await super().prompt(session_id, text, timeout)
    engine.adapter_factory = lambda profile, emit: Controlled(engine, profile, emit)
    await engine.tracker.create(key="a")
    await engine.tracker.create(key="b")
    engine.ensure_pool()
    engine.store.set_execution_state(True)
    await engine.sync_issues()
    await advance(engine, lambda: engine.store.require("execution", "issue-a")["stage"] == Stage.REVIEWING)
    assert engine.store.require("execution", "issue-b")["stage"] == Stage.DEVELOPING
    assert len([a for a in engine.agents() if a["state"] == "assigned_working"]) == 2
    assert len({a["issue_id"] for a in engine.agents() if a["state"] == "assigned_working"}) == 2


async def test_external_requirement_edit_pauses_work_and_invalidates_review(engine):
    await engine.tracker.create(key="a")
    engine.ensure_pool()
    engine.store.set_execution_state(True)
    await engine.sync_issues()
    await advance(engine, lambda: engine.store.require("execution", "issue-a")["stage"] == Stage.REVIEW_QUEUED)
    old = engine.store.require("execution", "issue-a")
    await engine.tracker.update("issue-a", {"acceptance_criteria": "Different behavior"})
    await engine.sync_issues()
    current = engine.store.require("execution", "issue-a")
    assert current["stage"] == Stage.DEVELOPING and current["paused"]
    assert current["specification"] != old["specification"]
    assert current["developer"] == old["developer"] and current.get("approval") is None
