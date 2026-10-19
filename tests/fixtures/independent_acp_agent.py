"""A deliberately SDK-free ACP v1 peer for cross-implementation contract tests.

Run with Python on stdio. It advertises no Harness extensions. Prompts containing
"permission" ask for an explicit agent-defined approval choice before replying.
"disconnect" exits mid-turn, exercising ambiguous network failure behavior.
"tool" streams generic tool activity. This is a test peer, not an agent runtime.
"""

import json
import sys


def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)


def update(session_id, value):
    send({"method": "session/update", "params": {"sessionId": session_id, "update": value}})


sessions = {}
pending = {}
sequence = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    params = request.get("params", {})
    request_id = request.get("id")
    if method is None:
        waiting = pending.pop(request_id, None)
        if waiting:
            original_id, session_id = waiting
            outcome = request.get("result", {}).get("outcome", {})
            text = "Approved" if outcome.get("optionId") == "allow-this" else "Rejected"
            update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
            send({"id": original_id, "result": {"stopReason": "end_turn"}})
        continue
    if method == "initialize":
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                # Embedded context is advertised, so explicit file attachments are
                # legal. Text prompts remain the only streamed output kind.
                "promptCapabilities": {"embeddedContext": True},
            },
            "agentInfo": {"name": "Independent ACP fixture", "version": "1.0.0"},
            "authMethods": [],
        }
    elif method == "session/new":
        sequence += 1
        session_id = f"independent-{sequence}"
        sessions[session_id] = []
        result = {"sessionId": session_id}
    elif method == "session/load":
        for text in sessions.get(params["sessionId"], []):
            update(params["sessionId"], {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
        result = {}
    elif method == "session/prompt":
        session_id = params["sessionId"]
        text = " ".join(block.get("text", "") for block in params["prompt"] if block["type"] == "text")
        if text == "disconnect":
            sys.exit(0)
        if "permission" in text:
            permission_id = f"permission-{sequence}"
            pending[permission_id] = (request_id, session_id)
            send({"id": permission_id, "method": "session/request_permission", "params": {
                "sessionId": session_id,
                "toolCall": {"toolCallId": "operation", "title": "Fixture operation", "status": "pending"},
                "options": [{"optionId": "allow-this", "name": "Allow this operation", "kind": "allow_once"},
                            {"optionId": "reject-this", "name": "Reject this operation", "kind": "reject_once"}],
            }})
            continue
        if "tool" in text:
            update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "fixture-tool", "title": "Independent lookup", "status": "in_progress", "kind": "other"})
            update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "fixture-tool", "status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": "Lookup complete"}}]})
        response = f"Independent reply: {text}"
        sessions[session_id].append(response)
        for chunk in ("Independent reply: ", text):
            update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": chunk}})
        result = {"stopReason": "end_turn"}
    elif method == "session/cancel":
        continue
    else:
        if request_id is not None:
            send({"id": request_id, "error": {"code": -32601, "message": "Method not found"}})
        continue
    if request_id is not None:
        send({"id": request_id, "result": result})
