from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Generator
from pathlib import Path

from .domain import HarnessError, Stage, check_transition
from .json_types import (
    JSONObject,
    JSONValue,
    boolean,
    decode_object,
    integer,
    json_object,
    json_value,
    optional_string,
    string,
)


def project_status(root: Path) -> JSONObject:
    """Read a journal snapshot without initializing the store or starting a service."""
    executions: list[JSONObject] = []
    agents: list[JSONObject] = []
    pending = 0
    execution: JSONObject = {"enabled": False, "reason": "not_started"}
    path = root / ".harness" / "journal.sqlite"
    if path.exists():
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)) as db:
            db.execute("BEGIN")
            version = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if not version or version[0] not in ("1", "2"):
                raise HarnessError("unsupported_schema", "Journal belongs to another schema version; use a compatible installed version")
            row = db.execute("SELECT value FROM meta WHERE key='execution'").fetchone()
            if row:
                execution = decode_object(string(json_value(row[0])))
            executions = [decode_object(string(json_value(row[0]))) for row in db.execute("SELECT data FROM objects WHERE kind='execution'")]
            agents = [decode_object(string(json_value(row[0]))) for row in db.execute("SELECT data FROM objects WHERE kind='agent'")]
            pending = integer(json_value(db.execute("SELECT COUNT(*) FROM requests WHERE resolved=0").fetchone()[0]))
    stages = {stage.value: 0 for stage in Stage}
    for issue in executions:
        stages[string(issue["stage"])] = stages.get(string(issue["stage"]), 0) + 1
    roles = {role: 0 for role in ("developer", "reviewer")}
    states: dict[str, int] = {}
    for agent in agents:
        roles[string(agent["role"])] = roles.get(string(agent["role"]), 0) + 1
        states[string(agent["state"])] = states.get(string(agent["state"]), 0) + 1
    return {
        "project": str(root), "execution": execution,
        "issues": {"total": len(executions), "by_stage": json_object(dict(sorted(stages.items()))),
                   "paused": sum(bool(issue.get("paused")) for issue in executions),
                   "terminal": sum(string(issue["stage"]) in (Stage.DONE, Stage.CANCELLED) for issue in executions)},
        "agents": {"total": len(agents), "by_role": json_object(dict(sorted(roles.items()))),
                   "by_state": json_object(dict(sorted(states.items()))),
                   "locked": sum(bool(optional_string(agent.get("issue_id"))) for agent in agents)},
        "pending_guidance": pending,
    }


class Store:
    """Host-only SQLite journal. Each transition and its update commit together."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            version = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if version and version[0] not in ("1", "2"):
                self.db.close()
                raise HarnessError("unsupported_schema", "Journal belongs to another schema version; use a compatible installed version")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS objects(
            kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(kind,id)
        );
        CREATE TABLE IF NOT EXISTS updates(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
            type TEXT NOT NULL, issue_id TEXT, agent_id TEXT, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS consumers(id TEXT PRIMARY KEY, cursor INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(
            id TEXT PRIMARY KEY, issue_id TEXT, data TEXT NOT NULL,
            response TEXT, resolved INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS commands(
            key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            operation TEXT NOT NULL, state TEXT NOT NULL, result TEXT, args TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS operations(
            id TEXT PRIMARY KEY, issue_id TEXT, kind TEXT NOT NULL,
            state TEXT NOT NULL, data TEXT NOT NULL
        );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(commands)")}
        if "args" not in columns:
            self.backup(path.with_suffix(".schema1-backup.sqlite"))
            self.db.execute("ALTER TABLE commands ADD COLUMN args TEXT NOT NULL DEFAULT '{}'")
        self.db.execute("INSERT INTO meta VALUES('schema','2') ON CONFLICT(key) DO UPDATE SET value='2'")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('execution',?)",
                        (json.dumps({"enabled": False, "reason": "not_started"}),))

    @contextlib.contextmanager
    def transaction(self) -> Generator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()

    def get(self, kind: str, id: str) -> JSONObject | None:
        row = self.db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, id)).fetchone()
        return decode_object(string(json_value(row[0]))) if row else None

    def require(self, kind: str, id: str) -> JSONObject:
        obj = self.get(kind, id)
        if obj is None:
            raise HarnessError("not_found", f"{kind} {id} not found")
        return obj

    def list(self, kind: str) -> list[JSONObject]:
        return [decode_object(string(json_value(r[0]))) for r in self.db.execute(
            "SELECT data FROM objects WHERE kind=? ORDER BY id", (kind,))]

    def put(self, kind: str, id: str, data: JSONObject) -> None:
        self.db.execute("INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) "
                        "DO UPDATE SET data=excluded.data", (kind, id, json.dumps(data)))

    def emit(self, type: str, data: JSONObject, issue_id: str | None = None,
             agent_id: str | None = None) -> int:
        cursor = self.db.execute("INSERT INTO updates(at,type,issue_id,agent_id,data) VALUES(?,?,?,?,?)",
                               (time.time(), type, issue_id, agent_id, json.dumps(data)))
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def transition(self, id: str, stage: str, **changes: JSONValue) -> JSONObject:
        with self.transaction():
            obj = self.require("execution", id)
            check_transition(string(obj["stage"]), stage)
            old = string(obj["stage"])
            obj.update(changes, stage=stage, updated_at=time.time())
            self.put("execution", id, obj)
            self.emit("execution.transition", {"from": old, "to": stage}, issue_id=id)
        return obj

    def execution_state(self) -> JSONObject:
        state = decode_object(string(json_value(self.db.execute("SELECT value FROM meta WHERE key='execution'").fetchone()[0])))

        boolean(state["enabled"])
        optional_string(state.get("reason"))
        return state

    def set_execution_state(self, enabled: bool, reason: str | None = None, **details: JSONValue) -> JSONObject:
        state = {"enabled": enabled, "reason": reason, **details}
        with self.transaction():
            self.db.execute("UPDATE meta SET value=? WHERE key='execution'", (json.dumps(state),))
            self.emit("project.resumed" if enabled else "project.paused", state)
        return state

    def request(self, issue_id: str | None, data: JSONObject) -> str:
        # A restart must not multiply the same unresolved request.
        existing = self.pending_requests(issue_id)
        for entry in existing:
            if json_object(entry["data"]) == data:
                return string(entry["id"])
        id = uuid.uuid4().hex
        self.db.execute("INSERT INTO requests(id,issue_id,data) VALUES(?,?,?)",
                        (id, issue_id, json.dumps(data)))
        self.emit("guidance.requested", {"request_id": id, **data}, issue_id)
        return id

    def pending_requests(self, issue_id: str | None = None) -> list[JSONObject]:
        query = "SELECT * FROM requests WHERE resolved=0"
        args = ()
        if issue_id is not None:
            query += " AND issue_id=?"
            args = (issue_id,)
        return [{**json_object(dict(r)), "data": decode_object(string(json_value(r["data"])))} for r in self.db.execute(query, args)]

    def answer(self, id: str, response: str) -> JSONObject:
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (id,)).fetchone()
        if not row:
            raise HarnessError("not_found", "Request not found")
        if row["resolved"]:
            if row["response"] != response:
                raise HarnessError("already_answered", "Request already has a different response")
            return json_object(dict(row))
        self.db.execute("UPDATE requests SET response=?,resolved=1 WHERE id=?", (response, id))
        self.emit("guidance.answered", {"request_id": id, "response": response}, optional_string(json_value(row["issue_id"])))
        return {**json_object(dict(row)), "response": response, "resolved": 1}

    def poll(self, consumer: str, cursor: int | None = None, limit: int = 50) -> JSONObject:
        row = self.db.execute("SELECT cursor FROM consumers WHERE id=?", (consumer,)).fetchone()
        cursor = cursor if cursor is not None else (integer(json_value(row[0])) if row else 0)
        highest = integer(json_value(self.db.execute("SELECT COALESCE(MAX(id),0) FROM updates").fetchone()[0]))
        assert cursor is not None
        if not 0 <= cursor <= highest or not 1 <= limit <= 200:
            raise HarnessError("invalid_cursor", "Cursor or page limit is out of range")
        rows = list(self.db.execute("SELECT * FROM updates WHERE id>? ORDER BY id LIMIT ?",
                                   (cursor, limit + 1)))
        updates = [{**json_object(dict(r)), "data": decode_object(string(json_value(r["data"])))} for r in rows[:limit]]
        next_cursor = integer(updates[-1]["id"]) if updates else cursor
        return {"updates": json_value(updates), "next_cursor": next_cursor, "has_more": len(rows) > limit,
                "requests": json_value(self.pending_requests()), "execution": self.execution_state()}

    def ack(self, consumer: str, cursor: int) -> JSONObject:
        self.poll(consumer, cursor, 1)  # Validate range without discarding requests.
        old = self.db.execute("SELECT cursor FROM consumers WHERE id=?", (consumer,)).fetchone()
        cursor = max(cursor, integer(json_value(old[0])) if old else 0)
        self.db.execute("INSERT INTO consumers VALUES(?,?) ON CONFLICT(id) "
                        "DO UPDATE SET cursor=excluded.cursor", (consumer, cursor))
        return {"consumer": consumer, "cursor": cursor}

    def begin_command(self, key: str, operation: str, args: JSONObject) -> tuple[bool, JSONObject | None]:
        fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM commands WHERE key=?", (key,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint or row["operation"] != operation:
                raise HarnessError("idempotency_conflict", "Idempotency key was used for different arguments")
            if row["state"] == "done":
                return False, decode_object(string(json_value(row["result"])))
            raise HarnessError("operation_pending", "Previous attempt needs reconciliation; do not repeat",
                               operation=operation, idempotency_key=key)
        self.db.execute("INSERT INTO commands(key,fingerprint,operation,state,args) VALUES(?,?,?,'pending',?)",
                        (key, fingerprint, operation, json.dumps(args)))
        return True, None

    def finish_command(self, key: str, result: JSONObject) -> None:
        self.db.execute("UPDATE commands SET state='done',result=? WHERE key=?", (json.dumps(result), key))

    def abandon_command(self, key: str) -> None:
        self.db.execute("DELETE FROM commands WHERE key=? AND state='pending'", (key,))

    def command(self, key: str) -> JSONObject:
        row = self.db.execute("SELECT * FROM commands WHERE key=?", (key,)).fetchone()
        if not row:
            raise HarnessError("not_found", "Command key not found")
        return {**json_object(dict(row)), "args": decode_object(string(json_value(row["args"]))),
                "result": decode_object(string(json_value(row["result"]))) if row["result"] is not None else None}

    def operation(self, issue_id: str, kind: str, data: JSONObject) -> str:
        id = uuid.uuid4().hex
        self.db.execute("INSERT INTO operations VALUES(?,?,?,'pending',?)",
                        (id, issue_id, kind, json.dumps(data)))
        return id

    def complete_operation(self, id: str) -> None:
        self.db.execute("UPDATE operations SET state='done' WHERE id=?", (id,))

    def pending_operations(self) -> list[JSONObject]:
        return [{**json_object(dict(r)), "data": decode_object(string(json_value(r["data"])))}
                for r in self.db.execute("SELECT * FROM operations WHERE state='pending'")]

    def backup(self, destination: Path) -> None:
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
