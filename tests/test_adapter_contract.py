import pytest
from adapter_contract import third_party_factory
from conftest import advance

from development_harness.adapters import ADAPTERS
from development_harness.config import Config, Profile
from development_harness.domain import HarnessError


async def test_engine_runs_independent_third_party_agent_factory_and_retains_context(engine, monkeypatch):
    instances = []

    factory = third_party_factory(engine, instances)

    monkeypatch.setitem(ADAPTERS, "third-party", factory)
    engine.config.profiles["codex"] = Profile(provider="third-party", command=["custom-worker"])
    engine.config.save(engine.root)
    engine.config = Config.load(engine.root)
    engine.adapter_factory = None
    issue = await engine.tracker.create(key="third-party")
    engine.ensure_pool()
    engine.store.set_execution_state(True)
    await engine.sync_issues()
    await advance(engine, lambda: engine.store.require("execution", issue["id"])["paused"])
    adapter = instances[0]
    assert adapter.closed and adapter.prompts and adapter.restores == [None]
    assert engine.store.execution_state()["enabled"]
    assert adapter.context.issue_id == issue["id"]
    assert issue["id"] in engine.backend.stopped
    execution = engine.store.require("execution", issue["id"])
    agent = engine.store.require("agent", execution["developer"])
    assert agent["session_id"] == "third-party-session"
    assert agent["capabilities"]["protocolVersion"] == 1
    assert agent["credential_hash"] is None
    assert engine.store.get("transcript", agent["id"] + ":" + issue["id"])


async def test_unknown_provider_configuration_rejected_before_pool_or_workers(engine):
    before = engine.agents()
    with pytest.raises(HarnessError) as error:
        Config.model_validate({"profiles": {"codex": {"provider": "codxe", "command": ["unused"]}}})
    assert error.value.as_dict() == {
        "code": "unknown_provider", "message": "Coding provider is not registered",
        "details": {"provider": "codxe"},
    }
    assert engine.agents() == before == []
    assert not engine.tasks and not engine.adapters
    assert not engine.store.poll("unknown-provider")["updates"]


async def test_mutated_unregistered_provider_rejected_before_binding_or_dispatch(engine):
    engine.ensure_pool()
    issue = await engine.tracker.create(key="typo")
    await engine.sync_issues()
    engine.store.set_execution_state(True)
    before_agents = engine.agents()
    before_execution = engine.store.require("execution", issue["id"])
    engine.config.profiles["codex"].provider = "codxe"
    for operation in (engine.ensure_pool, lambda: engine.bind(before_agents[0], before_execution)):
        with pytest.raises(HarnessError) as error:
            operation()
        assert error.value.code == "unknown_provider"
    with pytest.raises(HarnessError) as error:
        await engine.tick()
    assert error.value.code == "unknown_provider"
    assert engine.agents() == before_agents
    assert engine.store.require("execution", issue["id"]) == before_execution
    assert not engine.tasks and not engine.adapters
    assert not engine.backend.stopped
    assert not any(update["type"] == "agent.assigned" for update in engine.store.poll("typo")["updates"])



def test_pyright_rejects_nonconforming_provider_and_agent(tmp_path):
    import json
    import subprocess

    fixture = tmp_path / "bad_provider.py"
    fixture.write_text("""
from acp import Agent
from development_harness.adapters import AdapterFactory
class WrongAgent:
    async def prompt(self, session_id: int) -> str:
        return "invalid"
agent: Agent = WrongAgent()
factory: AdapterFactory = lambda profile, emit, context: WrongAgent()
""")
    result = subprocess.run(["pyright", "--outputjson", str(fixture)], capture_output=True, text=True)
    diagnostics = json.loads(result.stdout)["generalDiagnostics"]
    assert result.returncode == 1
    assert sum(item.get("rule") == "reportAssignmentType" for item in diagnostics) >= 2
