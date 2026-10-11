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
