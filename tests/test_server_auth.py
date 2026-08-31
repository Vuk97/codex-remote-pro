"""MCP server surface and authentication tests."""

import asyncio
import json

import httpx
import pytest

from codex_bridge.auth import BearerAuthMiddleware
from codex_bridge.server import build_app, mcp

TOKEN = "test-token-0123456789abcdefghij"


def test_expected_tools_exposed():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "codex_list_sessions",
        "codex_get_session",
        "codex_read_recent",
        "codex_send_message",
        "codex_interrupt",
        "advice_list_pending",
        "advice_respond",
        "bridge_inventory",
        "bridge_call",
    }


def test_short_token_refused():
    with pytest.raises(RuntimeError, match="bearer token"):
        BearerAuthMiddleware(lambda *a: None, "short")


@pytest.fixture()
def client(bridge_home, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://bridge.test")


def test_auth_required(client):
    async def go():
        r = await client.get("/healthz")
        assert r.status_code == 401
        r = await client.post("/mcp", json={})
        assert r.status_code == 401
        r = await client.get("/healthz", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        r = await client.get("/healthz", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
    asyncio.run(go())


def test_url_token_mode(bridge_home, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("BRIDGE_ALLOW_URL_TOKEN", "1")
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://bridge.test")

    async def go():
        r = await client.get(f"/t/{TOKEN}/healthz")
        assert r.status_code == 200
        r = await client.get("/t/wrong-token-aaaaaaaaaaaaaaaaaa/healthz")
        assert r.status_code == 401
    asyncio.run(go())


def test_mcp_initialize_over_http(bridge_home, monkeypatch):
    """A minimal MCP handshake through the authenticated endpoint."""
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    app = build_app()
    inner = app.app  # the Starlette app; its lifespan starts the MCP session manager
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge.test"
    )

    async def go():
        async with inner.router.lifespan_context(inner):
            await run_handshake()

    async def run_handshake():
        r = await client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert r.status_code == 200
        body = r.text
        assert "codex-session-bridge" in body
    asyncio.run(go())


def test_router_inventory_and_call(bridge_home):
    from codex_bridge.server import bridge_call, bridge_inventory

    inv = bridge_inventory()
    names = {o["name"] for o in inv["operations"]}
    assert {"list_sessions", "send_message", "readopt", "doctor",
            "advice_respond"} <= names

    assert bridge_call("list_sessions")["ok"]
    bad = bridge_call("no_such_op")
    assert bad["error"] == "unknown_operation" and "list_sessions" in bad["operations"]
    missing = bridge_call("get_session", {})
    assert missing["error"] == "missing_argument" and "session_id" in str(missing["params"])


def test_router_reaches_the_mailbox(bridge_home):
    from codex_bridge import advice
    from codex_bridge.server import bridge_call

    advice.create("route me", request_id="q-router")
    pending = bridge_call("advice_list_pending")
    assert [p["id"] for p in pending["pending"]] == ["q-router"]
    assert bridge_call("advice_respond", {
        "request_id": "q-router", "answer": "routed"})["ok"]
    assert advice.get("q-router")["request"]["answer"] == "routed"


def test_doctor_runs_and_is_honest(bridge_home):
    from codex_bridge import doctor

    report = doctor.run_checks(port=1)  # nothing listens on port 1
    ids = {c["id"]: c for c in report["checks"]}
    assert ids["daemon"]["status"] in ("error", "warning")
    # the connector check must never claim ok: it cannot be verified locally
    assert ids["connector"]["status"] == "warning"
    assert "links" in ids["connector"]


def test_daemon_stop_uses_the_pidfile(bridge_home, monkeypatch):
    """Stopping must name one pid, never pattern-kill every bridge."""
    from codex_bridge import server

    assert server.stop() == 1  # nothing recorded
    server.pidfile().parent.mkdir(parents=True, exist_ok=True)
    server.pidfile().write_text("999999999")   # a pid that cannot exist
    assert server.stop() == 0
    assert not server.pidfile().exists()

    killed = {}
    server.pidfile().write_text("4242")
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.setdefault("pid", pid))
    assert server.stop() == 0
    assert killed["pid"] == 4242
