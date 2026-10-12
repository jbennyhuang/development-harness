"""Malformed external values cannot become typed journal or command data."""
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from development_harness import client
from development_harness.config import Config
from development_harness.domain import HarnessError
from development_harness.engine import Engine
from development_harness.json_types import (
    JSONObject,
    JSONValue,
    decode,
    decode_object,
    integer,
    json_object,
    optional_string,
    string,
)
from development_harness.store import Store
from development_harness.tracker import Beads


@pytest.mark.parametrize("value", [None, [], 4, "text", True])
def test_object_decoder_rejects_non_objects(value: JSONValue) -> None:
    import json

    with pytest.raises(ValueError, match="object"):
        decode_object(json.dumps(value))


@pytest.mark.parametrize("raw", ['{"nested": [NaN]}', '[Infinity]', '{"a": -Infinity}'])
def test_decoder_rejects_non_json_numbers(raw: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        decode(raw)


def test_nested_boundary_validation_and_primitive_narrowing() -> None:
    assert decode_object('{"nested": [null, true, {"count": 2}], "extra": "kept"}') == {
        "nested": [None, True, {"count": 2}], "extra": "kept",
    }
    with pytest.raises(ValueError, match="keys"):
        json_object({1: "not a JSON key"})
    with pytest.raises(ValueError, match="JSON value"):
        json_object({"nested": [Path("not-json")]})
    with pytest.raises(ValueError, match="integer"):
        integer(True)


def test_journal_decode_rejects_malformed_object_without_mutation(tmp_path: Path) -> None:
    store = Store(tmp_path / "journal")
    try:
        store.db.execute("INSERT INTO objects VALUES('execution','broken','[1,2]')")
        before = list(store.db.iterdump())
        with pytest.raises(ValueError, match="object"):
            store.get("execution", "broken")
        assert list(store.db.iterdump()) == before
    finally:
        store.close()


async def test_malformed_worker_result_does_not_overwrite_submission(engine: Engine) -> None:
    import hashlib

    engine.ensure_pool()
    issue = await engine.tracker.create("Feature", "Description", "Acceptance", 2, "malformed")
    await engine.sync_issues()
    execution = engine.store.require("execution", string(issue["id"]))
    agent = engine.agents()[0]
    engine.bind(agent, execution)
    engine.store.transition(string(issue["id"]), "preparing")
    engine.store.transition(string(issue["id"]), "developing")
    agent.update(state="assigned_working", attempt=execution["generation"],
                 credential_hash=hashlib.sha256(b"test-scope").hexdigest())
    engine.store.put("agent", string(agent["id"]), agent)
    engine.store.set_execution_state(True)
    before = engine.store.require("agent", string(agent["id"]))
    for result in ({"kind": "development", "summary": []},
                   {"kind": "guidance", "summary": "question", "unexpected": "field"}):
        with pytest.raises(ValidationError):
            engine.submit("test-scope", json_object(result))
        assert engine.store.require("agent", string(agent["id"])) == before
    accepted: JSONObject = {"kind": "guidance", "summary": "valid"}
    assert engine.submit("test-scope", accepted)["accepted"]
    assert engine.submit("test-scope", accepted)["accepted"]
    with pytest.raises(HarnessError, match="different result"):
        engine.submit("test-scope", {"kind": "guidance", "summary": "different"})
    saved = engine.store.require("agent", string(agent["id"]))
    assert json_object(saved["result"])["summary"] == "valid"


async def test_http_rejects_malformed_command_before_agent_mutation(tmp_path: Path) -> None:
    import asyncio

    Config(max_agents=2, developers=1, reviewers=1).save(tmp_path)
    info = await asyncio.to_thread(client.start, tmp_path)
    try:
        async with httpx.AsyncClient(trust_env=False) as transport:
            for body in ([], {"command": []}, {"command": "agent create", "args": []},
                         {"command": "agent create", "args": {"role": 1, "profile": "codex"}}):
                response = await transport.post(string(info["url"]) + "/command", json=body,
                                                headers={"Authorization": "Bearer " + string(info["token"])})
                envelope = json_object(response.json())
                assert response.status_code == 400 and envelope["ok"] is False
                assert json_object(envelope["error"])["code"] == "invalid_arguments"
        result = await asyncio.to_thread(client.invoke, tmp_path, "agent list", {})
        assert result == {"agents": []}
    finally:
        await asyncio.to_thread(client.request, info, "/stop", {})
        for _ in range(100):
            current = client.metadata(tmp_path)
            if current is None or optional_string(current.get("generation")) != optional_string(info.get("generation")):
                break
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("raw", ['[1]', '{"issues": []} garbage', 'true'])
async def test_beads_boundary_rejects_malformed_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    from development_harness import tracker

    async def fake_run(argv: list[str], cwd: Path) -> tuple[int, str, str]:
        return 0, raw, ""

    monkeypatch.setattr(tracker, "run", fake_run)
    with pytest.raises(HarnessError) as error:
        await Beads(tmp_path, ["bd"]).call("list")
    assert error.value.code == "invalid_beads_output"
