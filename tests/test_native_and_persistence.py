"""Native-transport unit tests (codex queue mocked) and persistence tests:
idempotency records surviving restarts, audit log hygiene, NOT_WRITABLE."""

import json
import os
import time

from codex_bridge import native, registry, service
from codex_bridge.idempotency import IdempotencyStore
from codex_bridge.launcher import encode_message

THREAD = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000"


def make_fake_codex(tmp_path, monkeypatch, log_name="queue-calls.jsonl", rc=0):
    """Install a fake `codex` binary that records queue calls as JSON."""
    calls = tmp_path / log_name
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({rc})\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("BRIDGE_CODEX_BIN", str(fake))
    return calls


def adopt_fake_native(tmp_path, monkeypatch, session_id="gm", read_only=False):
    """Register a native session whose rollout is a temp file and whose
    bound pid is this test process (alive)."""
    codex_home = tmp_path / "codex-home"
    rollout_dir = codex_home / "sessions" / "2026" / "08" / "26"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / f"rollout-2026-08-26T18-40-57-{THREAD}.jsonl"
    rollout.write_text(json.dumps({
        "timestamp": "t", "type": "event_msg",
        "payload": {"type": "agent_message", "message": "hello from codex"},
    }) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(native, "pids_holding", lambda p: [os.getpid()])
    # Keep tests off the real machine's process table.
    monkeypatch.setattr(native, "discover", lambda: [])
    resp = service.adopt_native(session_id, THREAD, read_only=read_only)
    assert resp["ok"], resp
    return rollout


def test_adopt_list_read_native(bridge_home, tmp_path, monkeypatch):
    rollout = adopt_fake_native(tmp_path, monkeypatch)
    sess = service.get_session("gm")["session"]
    assert sess["transport"] == "native"
    assert sess["thread_id"] == THREAD
    assert sess["capabilities"] == ["READ", "WRITE"]
    assert sess["status"] == "BUSY"  # rollout mtime is fresh
    r = service.read_recent("gm", after_cursor=0)
    assert "hello from codex" in r["text"]
    raw = service.read_recent("gm", after_cursor=0, plain=False)
    assert "agent_message" in raw["text"]
    assert r["next_cursor"] == rollout.stat().st_size


def test_native_send_via_codex_queue(bridge_home, tmp_path, monkeypatch):
    adopt_fake_native(tmp_path, monkeypatch)
    calls = make_fake_codex(tmp_path, monkeypatch)
    msg = "multiline native\nsecond line\nthird 'quoted' $LINE `backticks`"
    resp = service.send_message("gm", msg, expected_generation=1,
                                idempotency_key="k1")
    assert resp["ok"], resp
    argv = json.loads(calls.read_text().strip())
    # Delivered verbatim as one argv element: no shell, no quoting damage.
    assert argv == ["queue", "--thread", THREAD, "--message", msg]

    # Duplicate key: acknowledged, not re-queued.
    resp2 = service.send_message("gm", msg, idempotency_key="k1")
    assert resp2["ok"] and resp2["deduplicated"] is True
    assert len(calls.read_text().splitlines()) == 1

    # Generation mismatch on a real registered session.
    resp3 = service.send_message("gm", "x", expected_generation=7)
    assert not resp3["ok"] and resp3["error"] == "generation_mismatch"


def test_native_queue_failure_surfaces(bridge_home, tmp_path, monkeypatch):
    adopt_fake_native(tmp_path, monkeypatch)
    make_fake_codex(tmp_path, monkeypatch, rc=3)
    resp = service.send_message("gm", "will fail")
    assert not resp["ok"] and resp["error"] == "queue_failed"


def test_not_writable_rejected(bridge_home, tmp_path, monkeypatch):
    adopt_fake_native(tmp_path, monkeypatch, session_id="ro", read_only=True)
    calls = make_fake_codex(tmp_path, monkeypatch)
    resp = service.send_message("ro", "nope")
    assert not resp["ok"] and resp["error"] == "NOT_WRITABLE"
    assert not calls.exists()


def test_native_exited_rejected(bridge_home, tmp_path, monkeypatch):
    adopt_fake_native(tmp_path, monkeypatch)
    # Bind to a dead pid and make rollout holders empty.
    registry.update("gm", pid=999999999)
    monkeypatch.setattr(native, "pids_holding", lambda p: [])
    sess = service.get_session("gm")["session"]
    assert sess["status"] == "EXITED"
    resp = service.send_message("gm", "too late")
    assert not resp["ok"] and resp["error"] == "session_exited"


def test_readopt_bumps_generation(bridge_home, tmp_path, monkeypatch):
    adopt_fake_native(tmp_path, monkeypatch)
    resp = service.adopt_native("gm", THREAD)
    assert resp["ok"] and resp["session"]["generation"] == 2


def test_idempotency_survives_restart(bridge_home):
    store = IdempotencyStore("s1")
    store.record("key-a", "hello", {"ok": True, "cursor": 5})
    # A fresh store instance (as after a daemon/launcher restart) still knows.
    verdict, resp = IdempotencyStore("s1").check("key-a", "hello")
    assert verdict == "duplicate" and resp["cursor"] == 5
    verdict, _ = IdempotencyStore("s1").check("key-a", "other")
    assert verdict == "conflict"


def test_audit_log_hashes_not_bodies(bridge_home, tmp_path, monkeypatch):
    adopt_fake_native(tmp_path, monkeypatch)
    make_fake_codex(tmp_path, monkeypatch)
    secret = "super-secret-prompt-body"
    service.send_message("gm", secret, idempotency_key="ka")
    log = (bridge_home / "audit.log").read_text()
    assert secret not in log
    rec = [json.loads(l) for l in log.splitlines() if '"send"' in l][-1]
    assert rec["session_id"] == "gm"
    assert rec["idempotency_key"] == "ka"
    assert len(rec["message_sha256"]) == 64
    assert rec["result"] == "ok"


def test_encode_message_bracketed_paste():
    assert encode_message("hi", "bracketed") == b"hi\r"
    multi = encode_message("a\nb", "bracketed")
    assert multi == b"\x1b[200~a\nb\x1b[201~\r"
    assert encode_message("a\nb", "plain") == b"a\nb\r"


def test_readopt_rebinds_dead_pid(bridge_home, tmp_path, monkeypatch):
    """Reboot recovery: bound pid dies, the rollout gains a new owner, and
    readopt re-binds to it with generation + 1 while a live pid is a no-op."""
    adopt_fake_native(tmp_path, monkeypatch)
    # Live pid: readopt must change nothing.
    r = service.readopt("gm")
    assert r["ok"] and r.get("unchanged") is True
    assert service.get_session("gm")["session"]["generation"] == 1
    # Simulate the reboot: bound pid gone, new process holds the rollout.
    registry.update("gm", pid=999999999)
    r = service.readopt("gm")
    assert r["ok"], r
    s = r["session"]
    assert s["generation"] == 2
    assert s["pid"] == os.getpid()
    # readopt_all covers the same path in bulk.
    registry.update("gm", pid=999999999)
    r = service.readopt_all()
    assert r["readopted"]["gm"]["session"]["generation"] == 3


def test_readopt_rejects_unknown_and_pty(bridge_home):
    assert service.readopt("nope")["error"] == "unknown_session"
    registry.register("ptys", launcher_pid=999999, child_pid=1, cwd="/tmp",
                      cmd=["x"], paste_mode="plain")
    assert service.readopt("ptys")["error"] == "not_a_native_session"


# -- auto-heal: a replaced codex process must not wedge remote clients ------


def test_autoheal_rebinds_replaced_process(bridge_home, tmp_path, monkeypatch):
    """A client with no shell (mobile, scheduled task) must not see
    stale_binding forever when codex restarted under the same conversation."""
    adopt_fake_native(tmp_path, monkeypatch)
    before = service.get_session("gm")["session"]["generation"]
    registry.update("gm", pid=999999999)
    monkeypatch.setattr(native, "codex_holders", lambda p: [4242])
    monkeypatch.setattr(native, "pids_holding", lambda p: [4242])
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid == 4242)

    sess = service.get_session("gm")["session"]
    assert sess["pid"] == 4242
    assert sess["generation"] == before + 1
    assert sess["status"] != "UNKNOWN"


def test_autoheal_lets_the_next_send_through(bridge_home, tmp_path, monkeypatch):
    adopt_fake_native(tmp_path, monkeypatch)
    registry.update("gm", pid=999999999)
    monkeypatch.setattr(native, "codex_holders", lambda p: [4242])
    monkeypatch.setattr(native, "pids_holding", lambda p: [4242])
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid == 4242)
    sent = {}
    monkeypatch.setattr(native, "queue_message",
                        lambda thread, msg: (sent.setdefault("msg", msg), True)[1] and (True, ""))

    resp = service.send_message("gm", "steer from the phone")
    assert resp["ok"], resp
    assert sent["msg"] == "steer from the phone"


def test_autoheal_can_be_turned_off(bridge_home, tmp_path, monkeypatch):
    adopt_fake_native(tmp_path, monkeypatch)
    registry.update("gm", pid=999999999)
    monkeypatch.setattr(native, "codex_holders", lambda p: [4242])
    monkeypatch.setattr(native, "pids_holding", lambda p: [4242])
    monkeypatch.setenv("BRIDGE_NO_AUTOHEAL", "1")

    sess = service.get_session("gm")["session"]
    assert sess["status"] == "UNKNOWN" and "readopt" in sess["note"]
    assert service.send_message("gm", "x")["error"] == "stale_binding"


def test_autoheal_fails_closed_on_ambiguity(bridge_home, tmp_path, monkeypatch):
    """Two codex processes on one rollout is a situation for a human."""
    adopt_fake_native(tmp_path, monkeypatch)
    registry.update("gm", pid=999999999)
    monkeypatch.setattr(native, "pids_holding", lambda p: [111, 222])
    monkeypatch.setattr(native, "codex_holders", lambda p: [111, 222])

    assert service.send_message("gm", "x")["error"] == "stale_binding"
    res = service.readopt("gm")
    assert res["error"] == "ambiguous_holders" and res["pids"] == [111, 222]


def test_remote_doctor_is_redacted(bridge_home):
    from codex_bridge.server import bridge_call

    report = bridge_call("doctor")
    for c in report["checks"]:
        assert "path" not in c and "url" not in c and "detail" not in c
        if c["id"].startswith("session:"):
            assert len(c["id"]) <= len("session:") + 8


def test_remote_steering_is_denied_by_default(bridge_home, tmp_path, monkeypatch):
    """A leaked bearer token must not be able to steer a live session."""
    adopt_fake_native(tmp_path, monkeypatch)
    monkeypatch.delenv("BRIDGE_REMOTE_STEERING", raising=False)
    res = service.send_message("gm", "steer", source="mcp")
    assert res["error"] == "remote_steering_disabled"
    # the local operator is unaffected
    monkeypatch.setattr(native, "queue_message", lambda t, m: (True, ""))
    assert service.send_message("gm", "steer", source="cli")["ok"]
    # and the operator can opt in for remote
    monkeypatch.setenv("BRIDGE_REMOTE_STEERING", "allow")
    assert service.send_message("gm", "steer", source="mcp")["ok"]


def test_mailbox_answers_ignore_the_steering_gate(bridge_home, monkeypatch):
    from codex_bridge import advice

    monkeypatch.delenv("BRIDGE_REMOTE_STEERING", raising=False)
    advice.create("q", request_id="gate-1")
    res = service.send_message(advice.MAILBOX_SESSION_ID, "id:gate-1\nverdict",
                               source="mcp")
    assert res["ok"], res
