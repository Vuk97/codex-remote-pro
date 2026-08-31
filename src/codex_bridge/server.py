"""MCP server (streamable HTTP) exposing the five codex_* tools.

Binds 127.0.0.1 by default. Every request must carry the bearer token; auth
happens in ASGI middleware before the MCP transport sees the request, which
also neutralizes DNS-rebinding (an unauthenticated cross-origin request
gets 401 before reaching any tool).

The tool surface is intentionally closed: no shell, no filesystem writes,
no arbitrary process control. Writes are limited to queueing a chat message
to an explicitly registered session, plus optional Ctrl-C to a bridge-owned
PTY session.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import advice, registry, service
from .auth import BearerAuthMiddleware, load_token

INSTRUCTIONS = """Bridge to the user's local Codex CLI sessions.
codex_list_sessions -> discover sessions (ids, generation, status, capabilities).
codex_read_recent -> incremental transcript reads via cursors; pass the
previous next_cursor as after_cursor to page forward.
codex_send_message -> queue one user chat message to one explicit session.
Always pass expected_generation (from list/get) and a fresh idempotency_key
(any unique string) so retries are safe. Message text is delivered verbatim;
multiline is fine. There is no shell access through this bridge.
The advice mailbox: local agents park questions here and block on your answer.
Reach it either way. Dedicated tools: advice_list_pending / advice_respond.
Or, if those tools are not in your schema, as the session "advice-mailbox":
codex_read_recent on it renders the pending questions, and codex_send_message
to it delivers one answer, first line the request id, the rest the answer.
bridge_inventory / bridge_call -> everything else. The bridge grows operations
over time and your cached schema does not refresh, so when you need something
these tools do not cover, call bridge_inventory and invoke by name."""

mcp = MCPServer(
    "codex-session-bridge",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)


@mcp.tool(
    name="codex_list_sessions",
    description="List registered Codex CLI sessions on this machine with "
    "session_id, generation, status (RUNNING/BUSY/WAITING_FOR_INPUT/EXITED/"
    "UNKNOWN), capabilities (READ/WRITE/INTERRUPT), cwd and transcript cursor.",
)
def codex_list_sessions() -> dict[str, Any]:
    return service.list_sessions()


@mcp.tool(
    name="codex_get_session",
    description="Get one session's full status by session_id.",
)
def codex_get_session(session_id: str) -> dict[str, Any]:
    return service.get_session(session_id)


@mcp.tool(
    name="codex_read_recent",
    description="Read recent session output incrementally. Omit after_cursor "
    "for the latest tail; then pass next_cursor back as after_cursor to page "
    "forward. limit is in transcript bytes (default 4000, max 65536). "
    "plain=true (default) returns readable text; plain=false returns the raw "
    "stream (rollout JSONL events for native sessions, terminal bytes for "
    "PTY sessions).",
)
def codex_read_recent(
    session_id: str,
    after_cursor: int | None = None,
    limit: int | None = None,
    plain: bool = True,
) -> dict[str, Any]:
    return service.read_recent(session_id, after_cursor=after_cursor, limit=limit, plain=plain)


@mcp.tool(
    name="codex_send_message",
    description="Queue one user chat message to one explicit Codex session. "
    "Message text is delivered verbatim (multiline safe, never shell-"
    "interpreted). Pass expected_generation to guard against a restarted "
    "session and a unique idempotency_key so retries never double-send. "
    "Rejects unknown, exited, stale-generation and non-writable sessions.",
)
def codex_send_message(
    session_id: str,
    message: str,
    expected_generation: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return service.send_message(
        session_id,
        message,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key,
        source="mcp",
    )


@mcp.tool(
    name="codex_interrupt",
    description="Send Ctrl-C to one bridge-owned (PTY) session. Only works "
    "for sessions whose capabilities include INTERRUPT.",
)
def codex_interrupt(
    session_id: str,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    return service.interrupt(session_id, expected_generation=expected_generation, source="mcp")


@mcp.tool(
    name="advice_list_pending",
    description="List advice requests local agents are waiting on: id, "
    "question, age_seconds. Answer each with advice_respond. Requests older "
    "than 24h are hidden (their asker has moved on).",
)
def advice_list_pending() -> dict[str, Any]:
    return advice.list_pending()


@mcp.tool(
    name="advice_respond",
    description="Answer one pending advice request by id. The answer is "
    "delivered verbatim to the waiting local agent. Pass a fresh "
    "idempotency_key (any unique string); retrying with the same key is "
    "safe, answering an already-answered id with a new key is rejected.",
)
def advice_respond(
    request_id: str,
    answer: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return advice.respond(request_id, answer, idempotency_key=idempotency_key)


# -- router ----------------------------------------------------------------
#
# A connector captures this server's tool schema when it is created and
# never refreshes it, so any tool added later is invisible to existing
# connectors. These two tools are the permanent escape hatch: their schema
# never changes, and everything new becomes an OPERATION discovered at call
# time. The cached schema is a router, not a catalog.

OPERATIONS: dict[str, dict[str, Any]] = {
    "list_sessions": {
        "call": lambda a: service.list_sessions(),
        "params": {},
        "doc": "List sessions, including the advice mailbox.",
    },
    "get_session": {
        "call": lambda a: service.get_session(a["session_id"]),
        "params": {"session_id": "str, required"},
        "doc": "One session's status.",
    },
    "read_recent": {
        "call": lambda a: service.read_recent(
            a["session_id"], after_cursor=a.get("after_cursor"),
            limit=a.get("limit")),
        "params": {"session_id": "str, required",
                   "after_cursor": "int, optional",
                   "limit": "int, optional"},
        "doc": "Incremental transcript read (snapshot for the mailbox).",
    },
    "send_message": {
        "call": lambda a: service.send_message(
            a["session_id"], a["message"],
            expected_generation=a.get("expected_generation"),
            idempotency_key=a.get("idempotency_key")),
        "params": {"session_id": "str, required", "message": "str, required",
                   "expected_generation": "int, session sends only",
                   "idempotency_key": "str, recommended"},
        "doc": "Queue one message to a session, or answer the mailbox "
               "(first line id:<request-id>).",
    },
    "readopt": {
        "call": lambda a: service.readopt(a["session_id"]),
        "params": {"session_id": "str, required"},
        "doc": "Re-bind a native session whose codex process was replaced. "
               "Usually unnecessary: reads and sends heal automatically.",
    },
    "advice_list_pending": {
        "call": lambda a: advice.list_pending(),
        "params": {},
        "doc": "Advice requests local agents are waiting on.",
    },
    "advice_respond": {
        "call": lambda a: advice.respond(
            a["request_id"], a["answer"],
            idempotency_key=a.get("idempotency_key"),
            responder=a.get("responder_identity") or "connector"),
        "params": {"request_id": "str, required", "answer": "str, required",
                   "idempotency_key": "str, recommended",
                   "responder_identity": "str, your model identity line; "
                                          "self-reported, stored for audit"},
        "doc": "Answer one pending advice request. Include "
               "responder_identity so the asker can audit which model "
               "answered.",
    },
    "doctor": {
        "call": lambda a: _doctor(),
        "params": {},
        "doc": "Bridge health report: daemon, auth, tunnel, sessions, "
               "mailbox. Redacted for remote callers; run codex-remote "
               "doctor locally for paths and pids.",
    },
}


def _doctor() -> dict[str, Any]:
    from . import doctor as doctor_mod

    # Remote callers get states and verdicts, not paths and pids: a leaked
    # bearer token should not also hand over a map of the machine.
    return doctor_mod.run_checks(redact=True)


@mcp.tool(
    name="bridge_inventory",
    description="List every operation this bridge currently supports: name, "
    "parameters, one-line doc. The set grows over time; call this rather "
    "than assuming. Invoke any of them with bridge_call.",
)
def bridge_inventory() -> dict[str, Any]:
    return {"ok": True, "operations": [
        {"name": name, "params": op["params"], "doc": op["doc"]}
        for name, op in sorted(OPERATIONS.items())
    ]}


@mcp.tool(
    name="bridge_call",
    description="Invoke one operation by exact name from bridge_inventory, "
    "with its arguments as an object. Unknown names return the current "
    "inventory so you can re-discover.",
)
def bridge_call(operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    op = OPERATIONS.get(operation)
    if op is None:
        return {"ok": False, "error": "unknown_operation", "operation": operation,
                "operations": sorted(OPERATIONS)}
    try:
        return op["call"](arguments or {})
    except KeyError as e:
        return {"ok": False, "error": "missing_argument", "operation": operation,
                "missing": str(e), "params": op["params"]}


@mcp.custom_route("/v1/models", methods=["GET"])
async def list_models(_: Request) -> JSONResponse:
    return JSONResponse({"object": "list", "data": [
        {"id": "chatgpt-pro", "object": "model", "owned_by": "codex-remote"},
    ]})


@mcp.custom_route("/v1/responses", methods=["POST"])
async def responses_api(request: Request):
    """The Responses wire; codex-cli >= 0.151 speaks only this."""
    from starlette.responses import StreamingResponse

    from . import promodel

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": {"message": "invalid JSON"}}, status_code=400)
    return StreamingResponse(promodel.responses_stream(body),
                             media_type="text/event-stream")


@mcp.custom_route("/v1/chat/completions", methods=["POST"])
async def chat_completions(request: Request):
    """Pro as a Codex model provider; see promodel.py for the mechanism."""
    from starlette.responses import StreamingResponse

    from . import promodel

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": {"message": "invalid JSON"}}, status_code=400)
    if body.get("stream"):
        return StreamingResponse(promodel.stream_completion(body),
                                 media_type="text/event-stream")
    return JSONResponse(await promodel.completion(body))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    # pid and version let a doctor probe verify it is talking to THIS
    # process, so port reuse by a stranger is caught instead of trusted.
    return JSONResponse({"ok": True, "sessions": len(registry.load_all()),
                         "pid": os.getpid(), "version": mcp.version})


def build_app():
    token = load_token()
    security = TransportSecuritySettings(
        # Auth middleware runs first and rejects anything without the bearer
        # token, which is what actually stops DNS rebinding; Host checking
        # would only break tunnel domains.
        enable_dns_rebinding_protection=False,
    )
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=security,
    )
    return BearerAuthMiddleware(
        inner,
        token or "",
        allow_url_token=os.environ.get("BRIDGE_ALLOW_URL_TOKEN") == "1",
    )


def pidfile() -> Path:
    from .paths import bridge_home

    return bridge_home() / "daemon.pid"


def stop() -> int:
    """Stop the daemon this bridge started, by pid, not by pattern.

    A pattern kill would take out every bridge on the machine; the pidfile
    names exactly the one this BRIDGE_HOME owns.
    """
    import signal

    p = pidfile()
    if not p.exists():
        print("no daemon pidfile; nothing recorded as running")
        return 1
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        print("unreadable pidfile; remove it by hand:", p)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        p.unlink(missing_ok=True)
        print(f"daemon {pid} was already gone; cleared the pidfile")
        return 0
    except PermissionError:
        print(f"pid {pid} is not yours to stop")
        return 1
    p.unlink(missing_ok=True)
    print(f"stopped daemon {pid}")
    return 0


def run(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    host = host or os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = port or int(os.environ.get("BRIDGE_PORT", "8788"))
    pf = pidfile()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(os.getpid()))
    try:
        uvicorn.run(build_app(), host=host, port=port, log_level="info")
    finally:
        if pf.exists() and pf.read_text().strip() == str(os.getpid()):
            pf.unlink(missing_ok=True)
