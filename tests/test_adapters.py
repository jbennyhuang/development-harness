import asyncio
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp import RequestError
from acp.schema import PermissionOption, TextContentBlock, ToolCallUpdate

from development_harness.adapters.antigravity import Antigravity
from development_harness.adapters.codex import Codex
from development_harness.config import Config, Profile
from development_harness.domain import HarnessError
from development_harness.provider_types import LaunchContext
from development_harness.providers import ADAPTERS
from development_harness.quota import usage_exhausted
from development_harness.sessions import initialize, session

COMMAND = [sys.executable, str(Path(__file__).parent / "fixtures" / "acp_agent.py")]


class FixtureBackend:
    def __init__(self):
        self.events = []

    async def credentials(self, issue_id, profile):
        self.events.append(("credentials", issue_id, profile))

    def agent_command(self, issue_id, profile, env):
        self.events.append(("command", issue_id, profile, env))
        return COMMAND


@pytest.mark.parametrize("provider", ["codex", "agy"])
async def test_independent_managed_stdio_negotiation_permissions_restore_and_configuration(provider):
    updates, backend = [], FixtureBackend()
    profile = Profile(provider=provider, command=COMMAND, mode="full", model="fixture-model", auth_method="fixture-auth")
    context = LaunchContext(backend, "issue", {}, "/issue")
    async with ADAPTERS[provider](profile, updates.append, context) as agent:
        caps = await initialize(agent)
        assert caps.protocol_version == 1
        session_id, restored = await session(agent, profile, caps, context.workspace, "lost-session", updates.append)
        assert session_id == "fixture-session" and not restored
        result = await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="Hello")])
        assert result.stop_reason == "end_turn"
        assert any(x.get("update", {}).get("sessionUpdate") == "agent_message_chunk" for x in updates)
        await agent.cancel(session_id=session_id)
        await agent.close_session(session_id=session_id)
        saved, restored = await session(agent, profile, caps, context.workspace, "saved-session", updates.append)
        assert saved == "saved-session" and restored
        with pytest.raises(Exception, match="Authentication denied"):
            await session(agent, profile, caps, context.workspace, "denied-session", updates.append)
    assert backend.events == [("credentials", "issue", profile), ("command", "issue", profile, {})]


def test_provider_usage_classification():
    assert usage_exhausted({"error": "insufficient_quota"})
    assert usage_exhausted("Resource_exhausted: rate limit")
    assert not usage_exhausted("authentication required")


def test_provider_owned_defaults_remain_compatible():
    profiles = Config().profiles
    assert profiles["codex"].command == ["codex-acp"]
    assert profiles["codex"].env_names == ["OPENAI_API_KEY", "CODEX_API_KEY"]
    assert profiles["codex"].mode == "agent-full-access"
    assert profiles["agy"].command == ["/opt/agy/agy_acp_server.par", "--uid="]


async def test_stdio_emitters_are_isolated_and_one_exit_keeps_other_connection_alive():
    first_updates, second_updates = [], []
    profile = Profile(provider="codex", command=COMMAND)
    async with AsyncExitStack() as stack:
        first_manager = ADAPTERS["codex"](profile, first_updates.append, LaunchContext(FixtureBackend(), "first", {}))
        first = await first_manager.__aenter__()
        second_profile = Profile(provider="agy", command=COMMAND)
        second = await stack.enter_async_context(ADAPTERS["agy"](second_profile, second_updates.append, LaunchContext(FixtureBackend(), "second", {})))
        try:
            await asyncio.gather(initialize(first), initialize(second))
            await asyncio.gather(first.new_session(cwd="/workspace", mcp_servers=[]), second.new_session(cwd="/workspace", mcp_servers=[]))
            await asyncio.gather(first.prompt(session_id="fixture-session", prompt=[TextContentBlock(type="text", text="first")]),
                                 second.prompt(session_id="fixture-session", prompt=[TextContentBlock(type="text", text="second")]))
            for updates, text in ((first_updates, "first"), (second_updates, "second")):
                assert [item["params"] for item in updates if item.get("method") == "fixture/status"] == [{"text": text}]
                assert {"unsupported_extension": "fixture/unsupported", "parameter_keys": ["private"]} in updates
                assert "do not echo" not in str(updates)
        finally:
            await first_manager.__aexit__(None, None, None)
        await second.prompt(session_id="fixture-session", prompt=[TextContentBlock(type="text", text="still connected")])
        assert {"method": "fixture/status", "params": {"text": "still connected"}} in second_updates
        assert {"method": "fixture/status", "params": {"text": "still connected"}} not in first_updates


@pytest.mark.parametrize("client_type", [Codex, Antigravity])
@pytest.mark.parametrize("method", ["write_text_file", "read_text_file", "create_terminal", "terminal_output",
    "release_terminal", "wait_for_terminal_exit", "kill_terminal", "create_elicitation", "complete_elicitation"])
async def test_unadvertised_callbacks_rejected(client_type, method):
    client = client_type(Profile(provider="codex" if client_type is Codex else "agy", command=["unused"]), lambda data: None)
    with pytest.raises(RequestError):
        await getattr(client, method)(session_id="session")


@pytest.mark.parametrize("client_type", [Codex, Antigravity])
async def test_permission_without_allow_option_fails_explicitly(client_type):
    client = client_type(Profile(provider="codex" if client_type is Codex else "agy", command=["unused"]), lambda data: None)
    with pytest.raises(HarnessError, match="Agent offered no allow option"):
        await client.request_permission("session", ToolCallUpdate(tool_call_id="call"), [
            PermissionOption(option_id="deny", name="Reject", kind="reject_once")])


@pytest.mark.parametrize("acl_case", ["native", "windows", "windows-failure"])
async def test_native_login_managed_sdk_authentication_and_cache_acl(tmp_path, monkeypatch, acl_case):
    from contextlib import asynccontextmanager

    from development_harness.adapters import antigravity
    windows_acl = acl_case != "native" or os.name == "nt"
    if windows_acl:
        # Replace only the provider's os reference; changing global os.name breaks pathlib.
        monkeypatch.setattr(antigravity, "os", SimpleNamespace(
            name="nt", environ={"USERDOMAIN": "fixture-domain", "USERNAME": "fixture-user"}))
    acl_error = HarnessError("command_failed", "icacls exited with 1")
    acl_run = AsyncMock(side_effect=acl_error if acl_case == "windows-failure" else None)
    monkeypatch.setattr(antigravity, "run", acl_run)
    Config().save(tmp_path)
    config_path = tmp_path / ".harness/config.json"
    original_config = config_path.read_bytes()
    tools = tmp_path / ".harness/tool-cache/agy-acp/1.3.0"
    tools.mkdir(parents=True)
    executable = tools / ("agy_acp_server.exe" if windows_acl else "agy_acp_server.par")
    executable.touch()
    cache = tmp_path / ".harness/credentials/gemini"
    events = []

    class NativeAgent:
        async def initialize(self, **kwargs):
            events.append("initialize")
            from acp.schema import InitializeResponse
            return InitializeResponse(protocol_version=1)

        async def authenticate(self, method_id):
            events.append(("authenticate", method_id))

        async def new_session(self, cwd, mcp_servers):
            assert cwd == str(tmp_path) and mcp_servers == []
            folder = cache / "antigravity-acp"
            folder.mkdir()
            (folder / "acp_token.json").write_text("fixture")
            (folder / "settings.json").write_text("fixture")
            from acp.schema import NewSessionResponse
            return NewSessionResponse(session_id="native-session")

    @asynccontextmanager
    async def transport(profile, emit, command, env):
        assert command == [str(executable), *([] if windows_acl else ["--uid="])]
        assert env == {"GEMINI_HOME": str(cache)}
        try:
            yield NativeAgent()
        finally:
            events.append("close")

    monkeypatch.setattr(antigravity.platform, "system", lambda: "Windows" if windows_acl else "Linux")
    monkeypatch.setattr(antigravity, "transport", transport)
    expected_acl = ["icacls", str(cache), "/inheritance:r", "/grant:r", "fixture-domain\\fixture-user:(OI)(CI)(F)"]
    if acl_case == "windows-failure":
        with pytest.raises(HarnessError) as error:
            await antigravity.login(tmp_path, "agy")
        assert error.value is acl_error
        acl_run.assert_awaited_once_with(expected_acl)
        assert events == []  # Authentication must never begin with an unprotected cache.
        assert config_path.read_bytes() == original_config
        assert not (cache / "antigravity-acp").exists()
        return
    result = await antigravity.login(tmp_path, "agy")
    assert result["authenticated"]
    assert events == ["initialize", ("authenticate", "oauth-personal"), "close"]
    profile = Config.load(tmp_path).profiles["agy"]
    assert profile.auth_method == "oauth-personal"
    assert set(profile.credential_files.values()) == {
        "/root/.gemini/antigravity-acp/acp_token.json", "/root/.gemini/antigravity-acp/settings.json"}
    if windows_acl:
        acl_run.assert_awaited_once_with(expected_acl)
    else:
        acl_run.assert_not_awaited()
        assert cache.stat().st_mode & 0o777 == 0o700
        for file in (cache / "antigravity-acp").iterdir():
            assert file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("provider,client_name", [("codex", "Codex"), ("agy", "Antigravity")])
@pytest.mark.parametrize("failure", ["spawn", "connect", "initialize", "authenticate", "session/new", "session/set_mode", "session/set_config_option", "session/prompt"])
async def test_owned_process_and_stderr_finish_on_all_startup_and_prompt_failures(provider, client_name, failure, monkeypatch):
    from importlib import import_module
    module = import_module("development_harness.adapters." + {"agy": "antigravity"}.get(provider, provider))
    client_type = getattr(module, client_name)
    clients = []
    original = client_type.__init__

    def record(self, *args):
        original(self, *args)
        clients.append(self)

    monkeypatch.setattr(client_type, "__init__", record)
    if failure == "connect":
        def broken(*args):
            raise RuntimeError("connect failure")
        monkeypatch.setattr(module, "connect_to_agent", broken)

    class Backend(FixtureBackend):
        def agent_command(self, *args):
            return ["/nonexistent-provider"] if failure == "spawn" else [*COMMAND, failure]

    profile = Profile(provider=provider, command=COMMAND, auth_method="fixture-auth", mode="full", model="fixture-model")
    with pytest.raises(Exception):
        async with ADAPTERS[provider](profile, lambda data: None, LaunchContext(Backend(), "failure", {})) as agent:
            caps = await initialize(agent)
            sid, _ = await session(agent, profile, caps, "/workspace", None, lambda data: None)
            await agent.prompt(session_id=sid, prompt=[TextContentBlock(type="text", text="failure")])
    client = clients[0]
    assert client.proc is None or client.proc.returncode is not None
    assert client.stderr_task is None or client.stderr_task.done()


@pytest.mark.parametrize("provider,client_name", [("codex", "Codex"), ("agy", "Antigravity")])
async def test_managed_transport_cleanup_survives_repeated_cancellation(provider, client_name, monkeypatch):
    from importlib import import_module
    module = import_module("development_harness.adapters." + {"agy": "antigravity"}.get(provider, provider))
    client_type = getattr(module, client_name)
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    clients = []
    original_close = client_type.close

    async def close(self):
        clients.append(self)
        closing.set()
        await release.wait()
        await original_close(self)

    monkeypatch.setattr(client_type, "close", close)

    async def run():
        async with ADAPTERS[provider](Profile(provider=provider, command=COMMAND), lambda data: None, LaunchContext(FixtureBackend(), "cancel", {})) as agent:
            await initialize(agent)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert clients[0].proc.returncode is not None and clients[0].stderr_task.done()
