import sys
from pathlib import Path

from development_harness.adapters import ACPAdapter, usage_exhausted
from development_harness.config import Profile


async def test_real_stdio_protocol_negotiation_permission_updates_and_restore_fallback():
    updates = []
    adapter = ACPAdapter(Profile(command=[sys.executable], mode="full"), updates.append)
    try:
        capabilities = await adapter.connect([sys.executable, str(Path(__file__).parent / "fixtures" / "acp_agent.py")], {})
        assert capabilities["protocolVersion"] == 1
        session, restored = await adapter.session("lost-session")
        assert session == "fixture-session" and not restored
        result = await adapter.prompt(session, "Hello", 10)
        assert result["stopReason"] == "end_turn"
        assert any(x.get("update", {}).get("sessionUpdate") == "agent_message_chunk" for x in updates)
        await adapter.cancel(session)
    finally:
        await adapter.close()
    assert adapter.proc.returncode is not None


def test_provider_usage_classification():
    assert usage_exhausted({"error": "insufficient_quota"})
    assert usage_exhausted("Resource_exhausted: rate limit")
    assert not usage_exhausted("authentication required")
