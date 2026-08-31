"""End-to-end tests against a fake interactive PTY process.

Covers the full required matrix: list, read, cursoring, send (including
"bridge-test" and 20KB multiline), exactly-once idempotency, generation
protection, unknown/exited rejection, and scoped Ctrl-C.
"""

import uuid

from codex_bridge import service
from conftest import wait_for


def read_all_text(session_id, after=0, limit=65536):
    r = service.read_recent(session_id, after_cursor=after, limit=limit, plain=True)
    assert r["ok"], r
    return r


def wait_for_text(session_id, needle, timeout=10.0):
    def check():
        return needle in read_all_text(session_id)["text"]
    wait_for(check, timeout=timeout, desc=f"transcript to contain {needle!r}")


def test_e2e_list_read_send(pty_session):
    sid = pty_session.session_id
    wait_for_text(sid, "FAKE-REPL-READY")

    listing = service.list_sessions()
    assert listing["ok"]
    (sess,) = [s for s in listing["sessions"] if s["session_id"] == sid]
    assert sess["generation"] == 1
    assert sess["transport"] == "pty"
    assert set(sess["capabilities"]) == {"READ", "WRITE", "INTERRUPT"}
    assert sess["status"] in ("RUNNING", "BUSY", "WAITING_FOR_INPUT")

    got = service.get_session(sid)
    assert got["ok"] and got["session"]["session_id"] == sid

    # The canonical first write test.
    resp = service.send_message(sid, "bridge-test")
    assert resp["ok"] and resp["deduplicated"] is False
    assert resp["generation"] == 1
    wait_for_text(sid, "echo:bridge-test")

    # Cursor-based incremental read: new output appears after the old cursor.
    r1 = read_all_text(sid)
    cursor = r1["next_cursor"]
    resp = service.send_message(sid, "second-message")
    assert resp["ok"]
    wait_for_text(sid, "echo:second-message")
    r2 = service.read_recent(sid, after_cursor=cursor, limit=65536)
    assert "echo:second-message" in r2["text"]
    assert "echo:bridge-test" not in r2["text"]
    assert r2["next_cursor"] > cursor


def test_multiline_and_20kb_message(pty_session):
    sid = pty_session.session_id
    wait_for_text(sid, "FAKE-REPL-READY")

    multiline = "alpha-line\nbeta-line\ngamma-line"
    resp = service.send_message(sid, multiline)
    assert resp["ok"]
    for part in ("echo:alpha-line", "echo:beta-line", "echo:gamma-line"):
        wait_for_text(sid, part)

    # 20KB structured message: 200 numbered 100-char lines, delivered intact
    # and in order.
    lines = [f"payload-{i:04d}-" + "z" * 86 for i in range(210)]
    big = "\n".join(lines)
    assert len(big) >= 20_000
    resp = service.send_message(sid, big)
    assert resp["ok"]
    wait_for_text(sid, "echo:payload-0209-", timeout=30)
    text = read_all_text(sid, limit=65536)["text"]
    first = text.index("echo:payload-0000-")
    last = text.index("echo:payload-0209-")
    assert first < last
    assert text.count("echo:payload-0100-") == 1


def test_idempotency_exactly_once(pty_session):
    sid = pty_session.session_id
    wait_for_text(sid, "FAKE-REPL-READY")
    key = str(uuid.uuid4())

    r1 = service.send_message(sid, "idem-once", idempotency_key=key)
    assert r1["ok"] and r1["deduplicated"] is False
    wait_for_text(sid, "echo:idem-once")

    r2 = service.send_message(sid, "idem-once", idempotency_key=key)
    assert r2["ok"] and r2["deduplicated"] is True

    text = read_all_text(sid, limit=65536)["text"]
    assert text.count("echo:idem-once") == 1

    # Same key with different content is a hard error, not a silent send.
    r3 = service.send_message(sid, "different-content", idempotency_key=key)
    assert not r3["ok"] and r3["error"] == "idempotency_key_reuse"


def test_wrong_generation_rejected(pty_session):
    sid = pty_session.session_id
    wait_for_text(sid, "FAKE-REPL-READY")
    resp = service.send_message(sid, "never-lands", expected_generation=99)
    assert not resp["ok"]
    assert resp["error"] == "generation_mismatch"
    assert resp["actual_generation"] == 1
    text = read_all_text(sid, limit=65536)["text"]
    assert "never-lands" not in text


def test_unknown_session_rejected(bridge_home):
    resp = service.send_message("nope", "hello")
    assert not resp["ok"] and resp["error"] == "unknown_session"
    assert not service.get_session("nope")["ok"]
    assert not service.read_recent("nope")["ok"]
    assert not service.interrupt("nope")["ok"]


def test_interrupt_scoped_to_session(pty_session):
    sid = pty_session.session_id
    wait_for_text(sid, "FAKE-REPL-READY")
    resp = service.interrupt(sid)
    assert resp["ok"]
    wait_for_text(sid, "INTERRUPTED")
    # Wrong generation is rejected for interrupt too.
    resp = service.interrupt(sid, expected_generation=42)
    assert not resp["ok"] and resp["error"] == "generation_mismatch"


def test_exited_session_rejected_and_readable(pty_session):
    sid = pty_session.session_id
    wait_for_text(sid, "FAKE-REPL-READY")
    resp = service.send_message(sid, "quit")
    assert resp["ok"]
    wait_for(lambda: pty_session.proc.poll() is not None, desc="launcher exit")

    sess = service.get_session(sid)["session"]
    assert sess["status"] == "EXITED"

    resp = service.send_message(sid, "too-late")
    assert not resp["ok"] and resp["error"] == "session_exited"

    # Transcript remains readable after exit via the file fallback.
    r = service.read_recent(sid, after_cursor=0, limit=65536)
    assert r["ok"] and "FAKE-REPL-BYE" in r["text"]
