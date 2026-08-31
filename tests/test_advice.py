"""Advice mailbox: create, list, respond, idempotency, wait."""

import json
import threading
import time

from codex_bridge import advice


def test_create_and_get(bridge_home):
    res = advice.create("Which storage layout?")
    assert res["ok"] and res["id"].startswith("adv-")
    got = advice.get(res["id"])
    assert got["ok"]
    assert got["request"]["status"] == "pending"
    assert got["request"]["question"] == "Which storage layout?"


def test_explicit_id_and_collision(bridge_home):
    assert advice.create("q", request_id="wh-1")["ok"]
    dup = advice.create("q2", request_id="wh-1")
    assert not dup["ok"] and dup["error"] == "id_exists"


def test_rejects_bad_input(bridge_home):
    assert advice.create("")["error"] == "empty_question"
    assert advice.create("q", request_id="bad id!")["error"] == "invalid_id"
    assert advice.create("x" * (advice.MAX_QUESTION_BYTES + 1))["error"] == "question_too_large"
    assert advice.respond("wh-none", "a")["error"] == "unknown_id"
    assert advice.get("wh-none")["error"] == "unknown_id"


def test_respond_and_idempotency(bridge_home):
    rid = advice.create("q", request_id="wh-2")["id"]
    assert advice.respond(rid, "pick B", idempotency_key="k1")["ok"]
    got = advice.get(rid)["request"]
    assert got["status"] == "answered" and got["answer"] == "pick B"
    # same key retries clean, new key is rejected
    again = advice.respond(rid, "pick B", idempotency_key="k1")
    assert again["ok"] and again.get("duplicate")
    other = advice.respond(rid, "pick A", idempotency_key="k2")
    assert not other["ok"] and other["error"] == "already_answered"


def test_list_pending_hides_answered_and_stale(bridge_home):
    a = advice.create("fresh")["id"]
    b = advice.create("answered")["id"]
    advice.respond(b, "done")
    c = advice.create("stale")["id"]
    rec = advice.get(c)["request"]
    rec["created_at"] = time.time() - advice.pending_ttl_seconds() - 60
    advice._write_atomic(advice._path(c), rec)

    ids = [r["id"] for r in advice.list_pending()["pending"]]
    assert ids == [a]
    ids_all = [r["id"] for r in advice.list_pending(include_stale=True)["pending"]]
    assert set(ids_all) == {a, c}


def test_wait_unblocks_on_answer(bridge_home):
    rid = advice.create("slow one")["id"]

    def answer_later():
        time.sleep(0.3)
        advice.respond(rid, "the verdict")

    t = threading.Thread(target=answer_later)
    t.start()
    res = advice.wait(rid, timeout_seconds=5.0, poll_seconds=0.05)
    t.join()
    assert res["ok"] and res["request"]["answer"] == "the verdict"


def test_wait_times_out(bridge_home):
    rid = advice.create("never answered")["id"]
    res = advice.wait(rid, timeout_seconds=0.2, poll_seconds=0.05)
    assert not res["ok"] and res["error"] == "timeout"


# -- virtual session surface ----------------------------------------------


def test_mailbox_listed_as_session(bridge_home):
    from codex_bridge import service

    ids = [s["session_id"] for s in service.list_sessions()["sessions"]]
    assert advice.MAILBOX_SESSION_ID in ids
    got = service.get_session(advice.MAILBOX_SESSION_ID)
    assert got["ok"] and got["session"]["capabilities"] == ["READ", "WRITE"]


def test_mailbox_read_renders_pending(bridge_home):
    from codex_bridge import service

    empty = service.read_recent(advice.MAILBOX_SESSION_ID)
    assert empty["ok"] and "no pending requests" in empty["text"]
    advice.create("Storage layout?", request_id="q-1")
    res = service.read_recent(advice.MAILBOX_SESSION_ID)
    assert "REQUEST id=q-1" in res["text"] and "Storage layout?" in res["text"]


def test_mailbox_send_answers_request(bridge_home):
    from codex_bridge import service

    advice.create("Which one?", request_id="q-2")
    res = service.send_message(advice.MAILBOX_SESSION_ID, "id:q-2\nPick B, it is cheaper to undo.")
    assert res["ok"]
    assert advice.get("q-2")["request"]["answer"] == "Pick B, it is cheaper to undo."


def test_mailbox_send_rejects_bad_shape(bridge_home):
    from codex_bridge import service

    advice.create("Which one?", request_id="q-3")
    assert service.send_message(advice.MAILBOX_SESSION_ID, "id:q-3")["error"] == "empty_answer"
    assert service.send_message(
        advice.MAILBOX_SESSION_ID, "id:q-nonexistent\nbody")["error"] == "unknown_id"
    # a first line that is not a bare id is a shape error, not a lookup miss
    for bad in ("!!!\nbody", "no such id here\nbody"):
        assert service.send_message(
            advice.MAILBOX_SESSION_ID, bad)["error"] == "missing_request_id"


def test_mailbox_strict_envelope(bridge_home):
    from codex_bridge import service

    advice.create("Which one?", request_id="q-4")
    # only 'id:<id>' routes; the permissive forms are refused
    for bad in ("q-4\nbody", "answer q-4\nbody", "id = q-4\nbody", "ID:q-4\nbody"):
        assert service.send_message(
            advice.MAILBOX_SESSION_ID, bad)["error"] == "missing_request_id"
    assert service.send_message(advice.MAILBOX_SESSION_ID, "id:q-4\nPick A.")["ok"]
    assert advice.get("q-4")["request"]["answer"] == "Pick A."
    # a second answer to the same request is refused
    assert service.send_message(
        advice.MAILBOX_SESSION_ID, "id:q-4\nPick B.")["error"] == "already_answered"


def test_mailbox_rejects_cursor_and_generation(bridge_home):
    from codex_bridge import service

    advice.create("q", request_id="q-5")
    assert service.read_recent(
        advice.MAILBOX_SESSION_ID, after_cursor=99)["error"] == "cursors_unsupported"
    assert service.read_recent(advice.MAILBOX_SESSION_ID)["ok"]
    assert service.send_message(
        advice.MAILBOX_SESSION_ID, "id:q-5\nbody",
        expected_generation=7)["error"] == "generation_unsupported"
    assert service.send_message(
        advice.MAILBOX_SESSION_ID, "id:q-5\nbody")["ok"]


def test_mailbox_honors_idempotency_key(bridge_home):
    from codex_bridge import service

    advice.create("q", request_id="q-6")
    assert service.send_message(
        advice.MAILBOX_SESSION_ID, "id:q-6\nverdict", idempotency_key="k9")["ok"]
    assert advice.get("q-6")["request"]["answer_idempotency_key"] == "k9"


def test_rejects_control_characters(bridge_home):
    bad = advice.create("what about \x1b]0;pwned\x07 this?")
    assert bad["error"] == "question_has_control_characters"
    assert "U+001B" in bad["codepoints"]
    rid = advice.create("clean question\nwith newline")["id"]
    res = advice.respond(rid, "answer with \x00 nul")
    assert res["error"] == "answer_has_control_characters"


def test_concurrent_responders_answer_once(bridge_home):
    import threading

    advice.create("race me", request_id="q-race")
    results, lock = [], threading.Lock()

    def answer(n):
        r = advice.respond("q-race", f"verdict {n}")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=answer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r.get("ok")) == 1
    assert all(r["error"] == "already_answered" for r in results if not r.get("ok"))


def test_answer_carries_provenance(bridge_home):
    rid = advice.create("q", request_id="q-prov")["id"]
    advice.respond(rid, "the verdict", responder="connector")
    rec = advice.get(rid)["request"]
    assert rec["provenance"] == "external-advice" and rec["responder"] == "connector"


def test_advice_dir_is_private(bridge_home):
    import stat

    advice.create("q", request_id="q-perm")
    assert stat.S_IMODE(advice.advice_dir().stat().st_mode) == 0o700
    assert stat.S_IMODE(advice._path("q-perm").stat().st_mode) == 0o600


# -- crash consistency: partial state must converge, never stick ------------


def test_orphan_claim_is_reclaimed_after_lease(bridge_home):
    """Responder died holding a claim: the request must not be stuck forever."""
    advice.create("q", request_id="q-orphan")
    assert advice._take_claim("q-orphan", "dead-responder")
    # a second responder is refused while the lease is live
    assert advice.respond("q-orphan", "late")["error"] == "already_answered"
    # age the claim past its lease, as a crash would leave it
    claim = advice._claim_path("q-orphan")
    claim.write_text(json.dumps({
        "responder": "dead-responder",
        "claimed_at": time.time() - advice.CLAIM_LEASE_SECONDS - 1,
    }))
    res = advice.respond("q-orphan", "recovered verdict")
    assert res["ok"]
    assert advice.get("q-orphan")["request"]["answer"] == "recovered verdict"


def test_committed_answer_beats_stale_claim(bridge_home):
    advice.create("q", request_id="q-committed")
    advice.respond("q-committed", "the verdict")
    advice._claim_path("q-committed").write_text(json.dumps({
        "responder": "x", "claimed_at": time.time() - advice.CLAIM_LEASE_SECONDS - 1,
    }))
    assert advice.respond("q-committed", "second")["error"] == "already_answered"
    assert advice.get("q-committed")["request"]["answer"] == "the verdict"


def test_leftover_tmp_file_does_not_corrupt_reads(bridge_home):
    """Crash between write and rename leaves a .tmp; the record still reads."""
    advice.create("q", request_id="q-tmp")
    advice._path("q-tmp").with_suffix(".tmp").write_text("{partial")
    assert advice.get("q-tmp")["request"]["status"] == "pending"
    assert [r["id"] for r in advice.list_pending()["pending"]] == ["q-tmp"]


def test_late_answer_after_expiry_is_refused(bridge_home, monkeypatch):
    monkeypatch.setenv("ADVICE_TTL_SECONDS", "1")
    advice.create("q", request_id="q-expired")
    rec = advice.get("q-expired")["request"]
    rec["created_at"] = time.time() - 10
    advice._write_atomic(advice._path("q-expired"), rec)
    res = advice.respond("q-expired", "too late")
    assert res["error"] == "expired_request"
    assert advice.get("q-expired")["request"]["answer"] is None


def test_gc_removes_abandoned_but_keeps_answered(bridge_home, monkeypatch):
    monkeypatch.setenv("ADVICE_TTL_SECONDS", "1")
    advice.create("abandoned", request_id="q-gc")
    advice.create("answered", request_id="q-keep")
    advice.respond("q-keep", "verdict")
    old = time.time() - advice.GC_GRACE_SECONDS - 100
    for rid in ("q-gc", "q-keep"):
        rec = advice.get(rid)["request"]
        rec["created_at"] = old
        advice._write_atomic(advice._path(rid), rec)
    res = advice.collect_garbage()
    assert res["removed"] == ["q-gc"]
    assert not advice._path("q-gc").exists()
    assert advice.get("q-keep")["request"]["answer"] == "verdict"


def test_backpressure_caps_outstanding(bridge_home):
    for i in range(advice.MAX_OUTSTANDING_PER_SOURCE):
        assert advice.create(f"q{i}", request_id=f"src-{i}", source="promodel")["ok"]
    over = advice.create("one too many", source="promodel")
    assert over["error"] == "source_quota_exceeded"
    # another source still gets through, until the global cap
    assert advice.create("other source", source="cli")["ok"]


def test_global_mailbox_cap(bridge_home):
    for i in range(advice.MAX_OUTSTANDING):
        advice.create(f"q{i}", request_id=f"g-{i}",
                      source=f"s{i % 5}")
    res = advice.create("blocked", source="fresh")
    assert res["error"] == "mailbox_full"
