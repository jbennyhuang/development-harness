from adapter_contract import ThirdPartyAdapter, structural_contract
from conftest import advance

from development_harness.adapters import ACPAdapter


async def test_engine_runs_structural_third_party_adapter_and_retains_context(engine):
    instances = []

    def factory(profile, emit):
        adapter = ThirdPartyAdapter(engine, profile, emit)
        instances.append(adapter)
        return structural_contract(adapter)

    engine.adapter_factory = factory
    issue = await engine.tracker.create(key="third-party")
    engine.ensure_pool()
    engine.store.set_execution_state(True)
    await engine.sync_issues()
    await advance(engine, lambda: engine.store.require("execution", issue["id"])["paused"])
    adapter = instances[0]
    assert not isinstance(adapter, ACPAdapter)
    assert adapter.closed and adapter.prompts and adapter.restores == [None]
    assert adapter.context.issue_id == issue["id"]
    assert issue["id"] in engine.backend.stopped
    execution = engine.store.require("execution", issue["id"])
    agent = engine.store.require("agent", execution["developer"])
    assert agent["session_id"] == "third-party-session"
    assert agent["capabilities"] == {"provider": "third-party"}
    assert agent["credential_hash"] is None
    assert engine.store.get("transcript", agent["id"] + ":" + issue["id"])
