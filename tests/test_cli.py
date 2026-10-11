import json
import sys

from development_harness.client import metadata, request
from development_harness.config import Config
from development_harness.process import run


async def cli(root, *args, check=True):
    code, out, err = await run([sys.executable, "-m", "development_harness", "--project", str(root), *args, "--json"], check=check)
    return code, json.loads(out), err


async def test_cli_json_invalid_input_and_command_schemas(tmp_path):
    code, result, _ = await cli(tmp_path, "issue", "create", check=False)
    assert code == 2 and result["error"]["code"] == "invalid_arguments"
    _, result, _ = await cli(tmp_path, "schema")
    assert "execution start" in result["result"]["commands"]
    assert result["result"]["exit_codes"]["0"] == "success"


async def test_git_init_requires_explicit_consent(tmp_path):
    code, result, _ = await cli(tmp_path, "init", "--skip-beads", check=False)
    assert code == 1 and result["error"]["code"] == "git_initialization_required"
    assert not (tmp_path / ".git").exists()
    _, result, _ = await cli(tmp_path, "init", "--skip-beads", "--initialize-git")
    assert result["ok"] and (tmp_path / ".git").exists()


async def test_service_survives_client_exit_durable_agent_and_cursor(tmp_path):
    config = Config(max_agents=2, developers=1, reviewers=1)
    config.save(tmp_path)
    try:
        _, started, _ = await cli(tmp_path, "service", "start")
        first_pid = started["result"]["pid"]
        _, created, _ = await cli(tmp_path, "agent", "create", "--role", "developer", "--profile", "codex", "--id", "dev", "--key", "create-dev")
        _, repeated, _ = await cli(tmp_path, "agent", "create", "--role", "developer", "--profile", "codex", "--id", "dev", "--key", "create-dev")
        assert created == repeated
        _, status, _ = await cli(tmp_path, "service", "status")
        assert status["result"]["pid"] == first_pid
        _, polled, _ = await cli(tmp_path, "updates", "poll", "--consumer", "chat")
        cursor = polled["result"]["next_cursor"]
        await cli(tmp_path, "updates", "ack", "--consumer", "chat", "--cursor", str(cursor))
        _, stopped, _ = await cli(tmp_path, "service", "stop")
        assert stopped["result"]["stopping"]
        import asyncio

        from development_harness.client import status
        for _ in range(50):
            if not status(tmp_path).get("pid"):
                break
            await asyncio.sleep(0.1)
        _, restarted, _ = await cli(tmp_path, "service", "start")
        assert restarted["result"]["pid"] != first_pid
        _, shown, _ = await cli(tmp_path, "agent", "show", "--id", "dev")
        assert shown["result"]["id"] == "dev" and "credential_hash" not in shown["result"]
        _, poll_after, _ = await cli(tmp_path, "updates", "poll", "--consumer", "chat")
        assert all(x["id"] > cursor for x in poll_after["result"]["updates"])
        info = metadata(tmp_path)
        import pytest

        from development_harness.domain import HarnessError
        with pytest.raises(HarnessError) as error:
            request({**info, "token": "wrong"}, "/status")
        assert error.value.code == "unauthorized"
    finally:
        await cli(tmp_path, "service", "stop", check=False)


async def test_status_empty_and_catalog(tmp_path):
    from development_harness.domain import Stage

    code, envelope, _ = await cli(tmp_path, "status")
    assert code == 0 and envelope["ok"]
    result = envelope["result"]
    assert result == {
        "project": str(tmp_path), "execution": {"enabled": False, "reason": "not_started"},
        "issues": {"total": 0, "by_stage": {stage.value: 0 for stage in Stage}, "paused": 0, "terminal": 0},
        "agents": {"total": 0, "by_role": {"developer": 0, "reviewer": 0}, "by_state": {}, "locked": 0},
        "pending_guidance": 0,
    }
    assert not (tmp_path / ".harness").exists()
    _, envelope, _ = await cli(tmp_path, "schema")
    assert "status" in envelope["result"]["commands"]
    code, out, _ = await run([sys.executable, "-m", "development_harness", "--help"])
    assert code == 0 and "status" in out


async def test_status_paused_snapshot_has_no_side_effects(tmp_path, monkeypatch):
    from development_harness import cli as module
    from development_harness.store import Store

    store = Store(tmp_path / ".harness" / "journal.sqlite")
    try:
        store.set_execution_state(False, "usage_exhausted", provider="codex")
        for id, stage, paused in (("a", "developing", True), ("b", "reviewing", False),
                                  ("c", "done", False), ("d", "cancelled", True)):
            store.put("execution", id, {"id": id, "stage": stage, "paused": paused})
        for id, role, state, issue in (("dev", "developer", "assigned_waiting", "a"),
                                      ("review", "reviewer", "idle", None),
                                      ("retired", "developer", "stopped", None)):
            store.put("agent", id, {"id": id, "role": role, "state": state, "issue_id": issue,
                                    "credential_hash": "private"})
        store.request("a", {"message": "help"})
        answered = store.request(None, {"message": "answered"})
        store.answer(answered, "proceed")
        store.ack("chat", 1)
        before = list(store.db.iterdump())

        def forbidden(*args, **kwargs):
            raise AssertionError("status must not invoke the service or initialize Store")

        monkeypatch.setattr(module.client, "invoke", forbidden)
        monkeypatch.setattr(Store, "__init__", forbidden)
        result = module.execute("status", {}, tmp_path, "ignored-key")
        code, envelope, _ = await cli(tmp_path, "status")
        assert code == 0 and envelope == {"ok": True, "result": result}
        assert result["execution"] == {"enabled": False, "reason": "usage_exhausted", "provider": "codex"}
        assert result["issues"]["total"] == 4
        assert result["issues"]["paused"] == 2 and result["issues"]["terminal"] == 2
        assert sum(result["issues"]["by_stage"].values()) == 4
        assert result["issues"]["by_stage"]["developing"] == 1
        assert result["agents"] == {"total": 3, "by_role": {"developer": 2, "reviewer": 1},
                                    "by_state": {"assigned_waiting": 1, "idle": 1, "stopped": 1}, "locked": 1}
        assert result["pending_guidance"] == 1
        assert "private" not in json.dumps(result)
        assert list(store.db.iterdump()) == before
        assert not (tmp_path / ".harness" / "service.json").exists()
    finally:
        store.close()
