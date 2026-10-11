import sqlite3

import pytest

from development_harness.domain import HarnessError, Stage
from development_harness.store import Store


def test_transition_and_event_are_atomic_and_illegal_transition_is_rejected(tmp_path):
    s = Store(tmp_path / "journal")
    s.put("execution", "a", {"id": "a", "stage": Stage.READY})
    s.transition("a", Stage.PREPARING)
    with pytest.raises(HarnessError, match="Cannot transition"):
        s.transition("a", Stage.MERGED)
    assert s.require("execution", "a")["stage"] == "preparing"
    assert len(s.poll("chat")["updates"]) == 1
    s.close()


def test_poll_reconnect_pagination_monotonic_ack_and_unanswered_requests(tmp_path):
    path = tmp_path / "journal"
    s = Store(path)
    s.emit("one", {})
    s.emit("two", {})
    request = s.request("a", {"question": "Which behavior?"})
    first = s.poll("chat", limit=1)
    assert first["has_more"] and first["next_cursor"] == 1
    assert s.poll("chat", 0, 1) == first
    s.ack("chat", 3)
    s.ack("chat", 1)
    s.close()
    s = Store(path)
    result = s.poll("chat")
    assert result["updates"] == [] and result["requests"][0]["id"] == request
    assert s.request("a", {"question": "Which behavior?"}) == request
    s.answer(request, "Use A")
    assert s.pending_requests() == []
    s.close()


def test_idempotency_rejects_conflicting_and_unreconciled_retries(tmp_path):
    s = Store(tmp_path / "journal")
    assert s.begin_command("key", "create", {"title": "a"}) == (True, None)
    with pytest.raises(HarnessError) as error:
        s.begin_command("key", "create", {"title": "a"})
    assert error.value.code == "operation_pending"
    s.finish_command("key", {"id": "a"})
    assert s.begin_command("key", "create", {"title": "a"}) == (False, {"id": "a"})
    with pytest.raises(HarnessError) as error:
        s.begin_command("key", "create", {"title": "b"})
    assert error.value.code == "idempotency_conflict"
    s.close()


def test_future_schema_is_rejected_without_rewriting_it(tmp_path):
    path = tmp_path / "journal"
    s = Store(path)
    s.db.execute("UPDATE meta SET value='99' WHERE key='schema'")
    s.close()
    with pytest.raises(HarnessError) as error:
        Store(path)
    assert error.value.code == "unsupported_schema"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "99"
