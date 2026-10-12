from __future__ import annotations

import hashlib
import shutil
import time
import uuid

from .backend import Docker
from .catalog import COMMANDS, schema
from .config import Config
from .domain import HarnessError, Stage
from .engine import POST_MERGE, TERMINAL, Engine
from .git import Git
from .instructions import ORCHESTRATOR
from .json_types import (
    JSONObject,
    array,
    integer,
    json_object,
    json_value,
    optional_integer,
    optional_string,
    string,
)
from .process import run
from .tracker import Beads


def public_agent(a: JSONObject) -> JSONObject:
    return {k: v for k, v in a.items() if k not in ("credential_hash", "result")}


class Commands:
    def __init__(self, engine: Engine) -> None:
        self.e, self.s = engine, engine.store

    async def invoke(self, command: str, args: JSONObject, key: str | None = None) -> JSONObject:
        if command not in COMMANDS:
            raise HarnessError("unknown_command", command)
        definitions = COMMANDS[command][1]
        if set(args) - definitions.keys():
            raise HarnessError("invalid_arguments", "Unknown arguments", fields=[field for field in sorted(set(args) - definitions.keys())])
        for name, definition in definitions.items():
            if definition["required"] and args.get(name) is None:
                raise HarnessError("invalid_arguments", f"Missing {name}")
            if name not in args and definition["default"] is not None:
                args[name] = definition["default"]
            if args.get(name) is not None:
                expected = {"str": str, "int": int, "bool": bool, "object": dict}[definition["type"]]
                if not isinstance(args[name], expected) or (expected is int and isinstance(args[name], bool)):
                    raise HarnessError("invalid_arguments", f"Invalid type for {name}")
        async with self.e.command_lock:
            if key and command == "agent create" and not optional_string(args.get("id")):
                args["id"] = string(args["role"]) + "-" + hashlib.sha256(key.encode()).hexdigest()[:10]
            if key:
                execute, previous = self.s.begin_command(key, command, args)
                if not execute:
                    assert previous is not None
                    return previous
            try:
                result = await self.dispatch(command, args, key or uuid.uuid4().hex)
            except Exception:
                # A side effect may already have completed; retain intent until explicitly reconciled.
                raise
            if key:
                self.s.finish_command(key, result)
            return result

    async def readiness(self) -> JSONObject:
        checks = {tool: shutil.which(tool) is not None for tool in ("git", "docker", "uv", "mise", "perles")}
        checks["bd"] = shutil.which(self.e.config.bd_command[0]) is not None
        checks["git_initialized"] = await self.e.git.initialized() if checks["git"] else False
        checks["target_commit"] = False
        checks["docker_running"] = False
        checks["worker_image"] = False
        if checks["git_initialized"]:
            code, _, _ = await self.e.git.call("rev-parse", "--verify", "refs/heads/" + self.e.config.target_branch, check=False)
            checks["target_commit"] = code == 0
        if checks["docker"]:
            code, _, _ = await run(["docker", "info", "--format", "{{.OSType}}"], check=False, timeout=30)
            checks["docker_running"] = code == 0
            code, _, _ = await run(["docker", "image", "inspect", self.e.config.image], check=False, timeout=30)
            checks["worker_image"] = code == 0
        return {"project": str(self.e.root), "checks": json_object(checks),
                "beads_initialized": (self.e.root / ".beads").is_dir(),
                "validation_configured": bool(self.e.config.validation_commands),
                "execution": self.s.execution_state()}

    async def cancel(self, id: str) -> JSONObject:
        e = self.s.require("execution", id)
        if string(e["stage"]) in TERMINAL:
            return e
        if string(e["stage"]) in POST_MERGE:
            raise HarnessError("already_merged", "Merged issues must finish resolution and cleanup")
        self.e.pause_issue(id, "cancel_requested", "Cancelling and archiving issue changes")
        await self.e.interrupt(id)
        # Keep the stopped container and worktree as the cancellation archive. Deleting them
        # would discard uncommitted work; explicit completed-issue cleanup handles successful merges.
        with self.s.transaction():
            e = self.s.require("execution", id)
            e.update(stage=Stage.CANCELLED, cancelled_at=time.time(), archive_retained=True)
            self.s.put("execution", id, e)
            for role in ("developer", "reviewer"):
                if e.get(role):
                    a = self.s.require("agent", string(e[role]))
                    a.update(issue_id=None, session_id=None, credential_hash=None, result=None,
                             messages=[], state="stopped" if a.get("retiring") else "idle")
                    self.s.put("agent", string(a["id"]), a)
            self.s.emit("issue.cancelled", {"archive_retained": True}, id)
        return e

    async def dispatch(self, command: str, a: JSONObject, key: str) -> JSONObject:
        e, s = self.e, self.s
        if command == "instructions":
            return {"instructions": ORCHESTRATOR, "poll_interval": e.config.poll_interval}
        if command == "schema":
            return schema()
        if command == "command inspect":
            return s.command(string(a["idempotency_key"]))
        if command == "command reconcile":
            original = s.command(string(a["idempotency_key"]))
            if string(original["state"]) == "done":
                return json_object(original["result"])
            operation, arguments = string(original["operation"]), json_object(original["args"])
            if operation == "issue create":
                result = await self.dispatch(operation, arguments, string(original["key"]))
            elif operation == "agent create":
                existing = s.get("agent", string(arguments.get("id", "")))
                if existing:
                    if existing["role"] != string(arguments["role"]) or existing["profile"] != string(arguments["profile"]):
                        raise HarnessError("reconciliation_conflict", "Agent identity has different properties")
                    result = public_agent(existing)
                else:
                    result = await self.dispatch(operation, arguments, string(original["key"]))
            elif operation in ("agent retire", "updates ack", "request respond"):
                result = await self.dispatch(operation, arguments, string(original["key"]))
            elif operation == "issue update":
                current = await e.tracker.read(string(arguments["id"]))
                if not all(current.get(k) == v for k, v in json_object(arguments["changes"]).items()):
                    raise HarnessError("reconciliation_required", "Actual issue differs; inspect it before deciding another update")
                result = current
            else:
                raise HarnessError("reconciliation_required", "This operation requires inspection; original intent is preserved", operation=operation)
            s.finish_command(string(original["key"]), result)
            s.emit("command.reconciled", {"key": string(original["key"]), "operation": operation})
            return result
        if command in ("inspect", "doctor"):
            return await self.readiness()
        if command == "config show":
            return json_object(e.config.model_dump())
        if command == "config update":
            if s.execution_state()["enabled"] or any(not t.done() for t in e.tasks.values()):
                raise HarnessError("pause_required", "Pause execution before changing configuration")
            config = Config.model_validate({**json_object(e.config.model_dump()), **json_object(a["changes"])})
            if any(optional_string(x.get("issue_id")) for x in e.agents()) and any(k in json_object(a["changes"]) for k in ("target_branch", "remote", "profiles", "image")):
                raise HarnessError("active_issues", "Finish or cancel locked issues before changing their environment")
            for role, count in (("developer", config.developers), ("reviewer", config.reviewers)):
                active = [x for x in e.agents() if string(x["role"]) == role and string(x["state"]) != "stopped"]
                for agent in active[count:]:
                    agent.update(retiring=True)
                    if not optional_string(agent.get("issue_id")):
                        agent["state"] = "stopped"
                    s.put("agent", string(agent["id"]), agent)
            config.save(e.root)
            e.config = config
            e.git = Git(e.root, config.target_branch, config.remote)
            if isinstance(e.backend, Docker):
                e.backend.config = config
            if isinstance(e.tracker, Beads):
                e.tracker.command = config.bd_command
            s.emit("configuration.updated", {"fields": [field for field in sorted(json_object(a["changes"]))]})
            return json_object(config.model_dump())
        if command == "adapters list":
            return {"profiles": {k: json_object(v.model_dump()) for k, v in e.config.profiles.items()}, "protocol": "ACP v1"}
        if command == "issue create":
            if not string(a["title"]).strip() or not string(a["acceptance"]).strip() or not 0 <= integer(a["priority"]) <= 4:
                raise HarnessError("invalid_issue", "Title, acceptance criteria, and priority 0..4 are required")
            issue = await e.tracker.create(string(a["title"]), string(a["description"]), string(a["acceptance"]), integer(a["priority"]), key)
            s.put("issue", string(issue["id"]), issue)
            s.emit("issue.created", {}, string(issue["id"]))
            return issue
        if command == "issue show":
            return await e.tracker.read(string(a["id"]))
        if command == "issue list":
            return {"issues": json_value(await e.tracker.list())}
        if command in ("issue update", "dependency add", "dependency remove"):
            id = string(a["id"])
            execution = s.get("execution", id)
            if execution and string(execution["stage"]) not in TERMINAL | POST_MERGE:
                self.e.pause_issue(id, "specification_update", "Issue requirements are being updated")
                await e.interrupt(id)
            if command == "issue update":
                if execution and json_object(a["changes"]).get("status") == "closed" and string(execution["stage"]) not in TERMINAL:
                    raise HarnessError("managed_resolution", "Cancel execution or let integration close the issue")
                result = await e.tracker.update(id, json_object(a["changes"]))
            else:
                result = await e.tracker.dependency(id, string(a["depends_on"]), command.endswith("remove"))
            # Leave the old mirror until reconciliation notices the changed specification.
            await e.sync_issues()
            if execution and string(execution["stage"]) not in TERMINAL | POST_MERGE:
                current = s.require("execution", id)
                if optional_string(current.get("developer")) and string(current["stage"]) != Stage.PREPARING:
                    if string(current["stage"]) != Stage.DEVELOPING:
                        s.transition(id, Stage.DEVELOPING, approval=None)
                current = s.require("execution", id)
                current.update(paused=False, pause_reason=None)
                e.save_execution(current)
                for request in s.pending_requests(id):
                    if json_object(request["data"]).get("reason") == "specification_update":
                        s.answer(string(request["id"]), "Specification updated; use current requirements")
            return result
        if command == "issue delete":
            if s.get("execution", string(a["id"])):
                await self.cancel(string(a["id"]))
            return await e.tracker.delete(string(a["id"]))
        if command == "agent create":
            return public_agent(e.create_agent(string(a["role"]), string(a["profile"]), optional_string(a.get("id"))))
        if command == "agent list":
            return {"agents": [public_agent(x) for x in e.agents()]}
        if command == "agent show":
            return public_agent(s.require("agent", string(a["id"])))
        if command == "agent retire":
            agent = s.require("agent", string(a["id"]))
            agent["retiring"] = True
            if not optional_string(agent.get("issue_id")):
                agent["state"] = "stopped"
            s.put("agent", string(a["id"]), agent)
            return public_agent(agent)
        if command == "agent send":
            agent = s.require("agent", string(a["id"]))
            if not optional_string(agent.get("issue_id")):
                raise HarnessError("unassigned_agent", "Messages must belong to an active issue")
            array(agent["messages"]).append(string(a["message"]))
            s.put("agent", string(a["id"]), agent)
            s.emit("agent.message_queued", {"message": string(a["message"])}, string(agent["issue_id"]), string(agent["id"]))
            return {"queued": True}
        if command == "agent messages":
            agent = s.require("agent", string(a["id"]))
            issue = a.get("issue") or optional_string(agent.get("issue_id"))
            return {"queued": array(agent["messages"]), "transcript": s.get("transcript", string(a["id"]) + ":" + str(issue))}
        if command == "agent interrupt":
            agent = s.require("agent", string(a["id"]))
            if not optional_string(agent.get("issue_id")):
                return {"interrupted": False}
            result = e.pause_issue(string(agent["issue_id"]), "interrupted", "Agent interrupted; supply direction or resume")
            await e.interrupt(string(agent["issue_id"]))
            return result
        if command == "execution pause":
            if optional_string(a.get("id")):
                result = e.pause_issue(string(a["id"]), "user_pause", "Issue paused by developer")
                await e.interrupt(string(a["id"]))
                return result
            return await e.pause_project("user_pause", "Project paused by developer")
        if command in ("execution start", "execution resume"):
            if optional_string(a.get("id")):
                obj = s.require("execution", string(a["id"]))
                if string(obj["stage"]) in TERMINAL:
                    raise HarnessError("terminal_issue", "Cannot resume a terminal issue")
                obj.update(paused=False, pause_reason=None, failures=0, revision_cycles=0)
                e.save_execution(obj)
                for request in s.pending_requests(string(a["id"])):
                    s.answer(string(request["id"]), "Explicitly resumed by developer")
                return obj
            readiness = await self.readiness()
            required = ("git", "git_initialized", "target_commit", "docker_running", "worker_image", "bd")
            missing = [k for k in required if not json_object(readiness["checks"])[k]]
            if missing or not readiness["validation_configured"] or not readiness["beads_initialized"]:
                raise HarnessError("not_ready", "Configure prerequisites and trusted validation before execution", readiness=readiness)
            e.ensure_pool()
            await e.sync_issues()
            for request in s.pending_requests():
                if optional_string(request["issue_id"]) is None and json_object(request["data"]).get("reason") == "provider_usage_exhausted":
                    s.answer(string(request["id"]), "Explicitly resumed; provider availability will be checked on dispatch")
            return s.set_execution_state(True)
        if command == "execution cancel":
            return await self.cancel(string(a["id"]))
        if command == "execution show":
            return s.require("execution", string(a["id"]))
        if command == "execution list":
            return {"project": s.execution_state(), "executions": json_value(e.executions())}
        if command == "updates poll":
            result = s.poll(string(a["consumer"]), optional_integer(a.get("cursor")), integer(a["limit"]))
            result.update(poll_interval=e.config.poll_interval, agents=[public_agent(x) for x in e.agents()],
                          issues=[{k: x.get(k) for k in ("id", "stage", "paused", "pause_reason")} for x in e.executions()])
            return result
        if command == "updates ack":
            return s.ack(string(a["consumer"]), integer(a["cursor"]))
        if command == "request respond":
            request = s.answer(string(a["id"]), string(a["response"]))
            id = optional_string(request["issue_id"])
            if id:
                obj = s.require("execution", id)
                if string(obj["stage"]) not in TERMINAL:
                    obj.update(paused=False, pause_reason=None, failures=0, revision_cycles=0,
                               feedback=string(a["response"]))
                    e.save_execution(obj)
            return request
        if command == "artifact show":
            return s.require("artifact", string(a["id"]))
        if command == "backup":
            folder = e.root / ".harness" / "backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            folder.mkdir(parents=True)
            s.backup(folder / "journal.sqlite")
            (folder / "config.json").write_text(e.config.model_dump_json(indent=2))
            return {"path": str(folder), "scope": "Harness journal and config; back up Beads with bd backup separately"}
        raise HarnessError("local_command", "This command must run through the local CLI")
