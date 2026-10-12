"""Protocol fixture, deliberately independent of the client SDK."""
import json
import sys


def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method, id = request.get("method"), request.get("id")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"close": {}}},
                  "agentInfo": {"name": "protocol-fixture", "version": "1"}, "authMethods": []}
    elif method == "session/new":
        result = {"sessionId": "fixture-session", "modes": {"currentModeId": "full",
                  "availableModes": [{"id": "full", "name": "Full container permissions"}]}}
    elif method == "authenticate":
        assert request["params"]["methodId"] == "fixture-auth"
        result = {}
    elif method == "session/load" and request["params"]["sessionId"] == "saved-session":
        result = {}
    elif method == "session/load" and request["params"]["sessionId"] == "denied-session":
        send({"id": id, "error": {"code": -32603, "message": "Authentication denied"}})
        continue
    elif method == "session/load":
        send({"id": id, "error": {"code": -32602, "message": "Session not found"}})
        continue
    elif method == "session/prompt":
        send({"method": "session/update", "params": {"sessionId": "fixture-session", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "fixture response"}}}})
        send({"id": "permission", "method": "session/request_permission", "params": {
            "sessionId": "fixture-session", "toolCall": {"toolCallId": "call", "title": "Run inside container"},
            "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]}})
        response = json.loads(sys.stdin.readline())
        if response.get("result", {}).get("outcome", {}).get("optionId") != "allow":
            raise RuntimeError("Permission response was not valid")
        result = {"stopReason": "end_turn"}
    elif method == "session/set_mode":
        assert request["params"]["modeId"] == "full"
        result = {}
    elif method == "session/set_config_option":
        assert request["params"]["configId"] == "model"
        assert request["params"]["value"] == "fixture-model"
        result = {"configOptions": []}
    elif method == "session/close":
        result = {}
    elif method == "session/cancel":
        continue
    else:
        send({"id": id, "error": {"code": -32601, "message": "Method not found"}})
        continue
    send({"id": id, "result": result})
