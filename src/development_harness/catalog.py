"""The same command definitions drive argparse and the machine-readable catalog."""
from typing import Literal, TypedDict

from .json_types import JSONObject, JSONValue, json_object


class Argument(TypedDict):
    type: Literal["str", "int", "bool", "object"]
    required: bool
    default: JSONValue
    description: str


def arg(type: Literal["str", "int", "bool", "object"]="str", required: bool=False, default: JSONValue=None, help: str="") -> Argument:
    return {"type": type, "required": required, "default": default, "description": help}


COMMANDS: dict[str, tuple[str, dict[str, Argument]]] = {
    "status": ("Summarize durable project execution, issues, agents, and guidance", {}),
    "inspect": ("Inspect host prerequisites and project readiness", {}),
    "init": ("Initialize project configuration and Beads (Git requires explicit consent)", {
        "initialize_git": arg("bool", default=False), "target_branch": arg(),
        "skip_beads": arg("bool", default=False)}),
    "instructions": ("Read the orchestrator operating guide", {}),
    "schema": ("Read machine-readable command and configuration schemas", {}),
    "doctor": ("Inspect prerequisites and image availability", {}),
    "tools setup": ("Install pinned project-local Beads, Dolt, and Perles through mise", {}),
    "tui": ("Launch Perles against this project's Beads database", {}),
    "image build": ("Build the trusted worker image from the installed harness", {}),
    "adapters list": ("List modular ACP profiles", {}),
    "adapters probe": ("Negotiate ACP and optionally create a session without running an issue", {
        "profile": arg(required=True), "session": arg("bool", default=False)}),
    "adapters login": ("Sign into Antigravity through its official native ACP OAuth helper", {"profile": arg(default="agy")}),
    "config show": ("Read effective project configuration", {}),
    "config update": ("Update configuration while execution is paused", {"changes": arg("object", True)}),
    "issue create": ("Create a Beads issue with acceptance criteria", {
        "title": arg(required=True), "description": arg(default=""), "acceptance": arg(required=True),
        "priority": arg("int", default=2)}),
    "issue show": ("Read an issue", {"id": arg(required=True)}),
    "issue list": ("List all issues", {}),
    "issue update": ("Update issue fields and invalidate stale approval", {
        "id": arg(required=True), "changes": arg("object", True)}),
    "issue delete": ("Cancel active execution and delete its issue", {"id": arg(required=True)}),
    "dependency add": ("Add a dependency, rejecting cycles", {
        "id": arg(required=True), "depends_on": arg(required=True)}),
    "dependency remove": ("Remove a dependency", {"id": arg(required=True), "depends_on": arg(required=True)}),
    "agent create": ("Create a durable agent identity", {"role": arg(required=True), "profile": arg(required=True), "id": arg()}),
    "agent list": ("List agents and issue locks", {}),
    "agent show": ("Inspect an agent", {"id": arg(required=True)}),
    "agent retire": ("Drain an assigned agent or stop an idle agent", {"id": arg(required=True)}),
    "agent send": ("Queue a message for the next assignment turn", {"id": arg(required=True), "message": arg(required=True)}),
    "agent messages": ("Read retained issue conversation", {"id": arg(required=True), "issue": arg()}),
    "agent interrupt": ("Interrupt and pause the assigned issue", {"id": arg(required=True)}),
    "execution start": ("Start deterministic project execution", {}),
    "execution pause": ("Pause the project or one issue, preserving work", {"id": arg()}),
    "execution resume": ("Resume the project or a paused issue", {"id": arg()}),
    "execution cancel": ("Archive changes and abandon an issue without claiming a merge", {"id": arg(required=True)}),
    "execution list": ("Inspect project executions", {}),
    "execution show": ("Inspect an issue execution", {"id": arg(required=True)}),
    "updates poll": ("Poll durable updates and unresolved guidance", {
        "consumer": arg(default="orchestrator"), "cursor": arg("int"), "limit": arg("int", default=50)}),
    "updates ack": ("Persist the consumer's handled cursor", {
        "consumer": arg(default="orchestrator"), "cursor": arg("int", True)}),
    "request respond": ("Answer guidance and resume the affected issue", {
        "id": arg(required=True), "response": arg(required=True)}),
    "artifact show": ("Read retained evidence", {"id": arg(required=True)}),
    "backup": ("Create an online SQLite journal/config backup; Beads remains separately owned", {}),
    "command inspect": ("Inspect a durable command intent", {"idempotency_key": arg(required=True)}),
    "command reconcile": ("Reconcile a supported interrupted mutation against actual state", {"idempotency_key": arg(required=True)}),
    "service start": ("Start the independent host service", {}),
    "service status": ("Inspect the host service", {}),
    "service stop": ("Stop the service while preserving execution state", {}),
    "worker assignment": ("Read the current scoped assignment", {}),
    "worker submit": ("Submit one structured development, review, merge, or guidance result", {"result": arg("object", True)}),
}


def schema() -> JSONObject:
    from .config import Config
    from .domain import Result
    return {
        "version": 1,
        "commands": {name: {"description": description, "arguments": json_object(args)}
                     for name, (description, args) in COMMANDS.items()},
        "configuration": json_object(Config.model_json_schema()), "worker_result": json_object(Result.model_json_schema()),
        "input": "--input accepts a JSON object from a file, or - for stdin. Worker submit also accepts a bare result object.",
        "output": {"success": {"ok": True, "result": "command result"},
                   "failure": {"ok": False, "error": {"code": "stable code", "message": "description", "details": {}}}},
        "exit_codes": {"0": "success", "1": "operation failed", "2": "invalid arguments", "130": "interrupted"},
        "idempotency": "Use --key for retried mutations. Pending operations require reconciliation, not blind retry.",
    }
