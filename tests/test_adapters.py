import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from development_harness.adapters import ADAPTERS, ACPAdapter, LaunchContext, usage_exhausted
from development_harness.config import Config, Profile
from development_harness.domain import HarnessError


async def test_real_stdio_protocol_negotiation_permission_updates_and_restore_fallback():
    updates = []
    adapter = ACPAdapter(Profile(command=[sys.executable], mode="full", model="fixture-model", auth_method="fixture-auth"), updates.append)
    try:
        capabilities = await adapter.connect([sys.executable, str(Path(__file__).parent / "fixtures" / "acp_agent.py")], {})
        assert capabilities["protocolVersion"] == 1
        session, restored = await adapter.session("lost-session")
        assert session == "fixture-session" and not restored
        result = await adapter.prompt(session, "Hello", 10)
        assert result["stopReason"] == "end_turn"
        assert any(x.get("update", {}).get("sessionUpdate") == "agent_message_chunk" for x in updates)
        await adapter.cancel(session)
        await adapter.close_session(session)
        saved, restored = await adapter.session("saved-session")
        assert saved == "saved-session" and restored
        with pytest.raises(Exception, match="Authentication denied"):
            await adapter.session("denied-session")
    finally:
        await adapter.close()
    assert adapter.proc.returncode is not None


def test_provider_usage_classification():
    assert usage_exhausted({"error": "insufficient_quota"})
    assert usage_exhausted("Resource_exhausted: rate limit")
    assert not usage_exhausted("authentication required")


@pytest.mark.parametrize("provider", ["generic", "codex", "agy"])
async def test_provider_start_composes_fenced_backend_and_preserves_profile(provider):
    events = []
    profile = Profile(provider=provider, command=["custom-agent", "--custom"],
                      auth_method="custom-auth", env_names=["SELECTED_ENV"],
                      credential_files={"explicit-source": "/root/auth.json"})

    class Backend:
        async def credentials(self, issue_id, selected):
            events.append(("credentials", issue_id, selected))

        def agent_command(self, issue_id, selected, env):
            events.append(("command", issue_id, selected, env))
            return ["fenced-exec", *selected.command]

    adapter = ADAPTERS[provider](profile, events.append)
    # Spy only at the transport boundary; concrete startup must do the setup.
    adapter.connect = AsyncMock(return_value={"protocolVersion": 1})
    env = {"HARNESS_WORKER_TOKEN": "fixture-token"}
    result = await adapter.start(LaunchContext(Backend(), "issue-one", env, "/issue"))
    assert result == {"protocolVersion": 1}
    assert events == [("credentials", "issue-one", profile), ("command", "issue-one", profile, env)]
    adapter.connect.assert_awaited_once_with(["fenced-exec", "custom-agent", "--custom"], env)
    assert adapter.workspace == "/issue"


def test_provider_owned_defaults_remain_compatible():
    profiles = Config().profiles
    assert profiles["codex"].command == ["codex-acp"]
    assert profiles["codex"].env_names == ["OPENAI_API_KEY", "CODEX_API_KEY"]
    assert profiles["codex"].mode == "agent-full-access"
    assert profiles["agy"].command == ["/opt/agy/agy_acp_server.par", "--uid="]


async def test_not_started_and_unadvertised_client_capabilities_fail_explicitly():
    from acp import RequestError

    adapter = ACPAdapter(Profile(command=["unused"]), lambda data: None)
    with pytest.raises(HarnessError, match="Start the adapter"):
        await adapter.authenticate("auth")
    with pytest.raises(RequestError):
        await adapter.write_text_file("session", "/host/file", "data")
    await adapter.close_session("session")
    await adapter.close()


async def test_native_antigravity_login_uses_public_authentication_and_selected_cache(tmp_path, monkeypatch):
    from development_harness.adapters import antigravity

    tools = tmp_path / ".harness/tool-cache/agy-acp/1.3.0"
    tools.mkdir(parents=True)
    (tools / "agy_acp_server.par").touch()
    cache = tmp_path / ".harness/credentials/gemini"
    events = []

    class NativeAdapter:
        def __init__(self, profile, emit):
            assert profile.auth_method is None and profile.mode is None and profile.model is None

        async def connect(self, command, env):
            assert self.workspace == str(tmp_path)
            events.append(("connect", command, env))

        async def authenticate(self, method_id, timeout):
            events.append(("authenticate", method_id, timeout))

        async def session(self, existing):
            assert existing is None
            folder = cache / "antigravity-acp"
            folder.mkdir()
            (folder / "acp_token.json").write_text("fixture")
            (folder / "settings.json").write_text("fixture")
            return "native-session", False

        async def close(self):
            events.append("close")

    monkeypatch.setattr(antigravity.platform, "system", lambda: "Linux")
    monkeypatch.setattr(antigravity, "AntigravityAdapter", NativeAdapter)
    result = await antigravity.login(tmp_path, "agy")
    assert result["authenticated"]
    assert events == [
        ("connect", [str(tools / "agy_acp_server.par"), "--uid="], {"GEMINI_HOME": str(cache)}),
        ("authenticate", "oauth-personal", 600), "close",
    ]
    profile = Config.load(tmp_path).profiles["agy"]
    assert profile.auth_method == "oauth-personal"
    assert set(profile.credential_files.values()) == {
        "/root/.gemini/antigravity-acp/acp_token.json", "/root/.gemini/antigravity-acp/settings.json",
    }


async def test_stdio_callbacks_are_isolated_across_concurrent_adapters():
    first_updates, second_updates = [], []
    first = ACPAdapter(Profile(command=["unused"]), first_updates.append)
    second = ACPAdapter(Profile(command=["unused"]), second_updates.append)
    command = [sys.executable, str(Path(__file__).parent / "fixtures" / "acp_agent.py")]
    try:
        await asyncio.gather(first.connect(command, {}), second.connect(command, {}))
        await asyncio.gather(first.session(None), second.session(None))
        await asyncio.gather(first.prompt("fixture-session", "first", 10),
                             second.prompt("fixture-session", "second", 10))
        for updates, text in ((first_updates, "first"), (second_updates, "second")):
            assert [item["params"] for item in updates if item.get("method") == "fixture/status"] == [{"text": text}]
            assert any(item.get("update", {}).get("sessionUpdate") == "agent_message_chunk" for item in updates)
            assert {"unsupported_extension": "fixture/unsupported", "parameter_keys": ["private"]} in updates
            assert "do not echo" not in str(updates)
        await first.close()
        assert first.proc.returncode is not None
        # Closing one transport must leave the other callback receiver usable.
        await second.prompt("fixture-session", "still connected", 10)
        assert {"method": "fixture/status", "params": {"text": "still connected"}} in second_updates
        assert {"method": "fixture/status", "params": {"text": "still connected"}} not in first_updates
    finally:
        await asyncio.gather(first.close(), second.close())
    assert second.proc.returncode is not None


@pytest.mark.parametrize("method", [
    "write_text_file", "read_text_file", "create_terminal", "terminal_output",
    "release_terminal", "wait_for_terminal_exit", "kill_terminal",
    "create_elicitation", "complete_elicitation",
])
async def test_unadvertised_callbacks_return_request_errors(method):
    from acp import RequestError

    adapter = ACPAdapter(Profile(command=["unused"]), lambda data: None)
    with pytest.raises(RequestError):
        await getattr(adapter, method)(session_id="session")


async def test_permission_without_allow_option_fails_explicitly():
    from acp.schema import PermissionOption, ToolCallUpdate

    adapter = ACPAdapter(Profile(command=["unused"]), lambda data: None)
    with pytest.raises(HarnessError, match="Agent offered no allow option"):
        await adapter.request_permission("session", ToolCallUpdate(tool_call_id="call"), [
            PermissionOption(option_id="deny", name="Reject", kind="reject_once"),
        ])
