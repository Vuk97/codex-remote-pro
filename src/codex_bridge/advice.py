"""Advice mailbox: any local agent asks, the connector-side model answers.

A request is one JSON file under BRIDGE_HOME/advice/. Local writers create it
as pending; the supervising model (through the MCP tools) lists pending
requests and writes an answer back into the same file. Local readers poll.
Files are written atomically (tmp + rename) so a poller never sees a torn
record. No locks: each request has exactly one writer per transition.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .auditlog import audit
from .paths import bridge_home

MAX_QUESTION_BYTES = 32 * 1024
MAX_ANSWER_BYTES = 32 * 1024

# Pending requests older than the TTL are hidden from the responder so a
# supervisor that wakes up hours later does not answer questions nobody is
# waiting on. The file is kept; `answers --id` still reads it. Default is
# short on purpose: the asker is usually a blocked agent, not a queue.
DEFAULT_PENDING_TTL_SECONDS = 15 * 60
MAX_PENDING_TTL_SECONDS = 3600

# Blocking wait: default matches the TTL scale, poll fast enough that an
# answer lands within a second of arriving.
DEFAULT_WAIT_SECONDS = 300.0
MAX_WAIT_SECONDS = 3600.0
DEFAULT_POLL_SECONDS = 1.0

# A claim is a lease, not ownership: a responder that dies mid-answer must not
# make a request permanently unanswerable. After the lease expires and with no
# committed answer, the next responder may take it over.
CLAIM_LEASE_SECONDS = 120.0

# Expired requests are deleted after a grace window. Process liveness is not
# trustworthy across crashes, so the TTL is the authoritative rule.
GC_GRACE_SECONDS = 3600.0

# A runaway caller (or a Pro-model call that recursively asks Pro) could
# otherwise fill the mailbox faster than any responder drains it.
MAX_OUTSTANDING = 20
MAX_OUTSTANDING_PER_SOURCE = 10


def pending_ttl_seconds() -> int:
    raw = os.environ.get("ADVICE_TTL_SECONDS")
    if not raw:
        return DEFAULT_PENDING_TTL_SECONDS
    try:
        return max(1, min(int(raw), MAX_PENDING_TTL_SECONDS))
    except ValueError:
        return DEFAULT_PENDING_TTL_SECONDS

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def advice_dir() -> Path:
    return bridge_home() / "advice"


# Protocol constraint, not a security boundary: this is a text-only protocol,
# so C0 controls other than newline, tab and carriage return are refused at
# the edge. Callers get a typed error naming the codepoints. The bridge never
# rewrites or interprets a body; a sanitizing bridge would imply a safety
# guarantee it cannot make. Escaping stays the job of whatever renders the
# text into a terminal or a log.
_ALLOWED_CONTROLS = {"\n", "\t", "\r"}


def _err(code: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, **extra}


def _control_chars(text: str) -> list[str]:
    return sorted({
        c for c in text
        if (ord(c) < 0x20 or ord(c) == 0x7F) and c not in _ALLOWED_CONTROLS
    })


def _check_text(text: str, limit: int, kind: str) -> dict[str, Any] | None:
    if len(text.encode()) > limit:
        return _err(f"{kind}_too_large", limit_bytes=limit)
    bad = _control_chars(text)
    if bad:
        return _err(
            f"{kind}_has_control_characters",
            codepoints=[f"U+{ord(c):04X}" for c in bad],
            hint="Only newline, tab and carriage return are allowed.",
        )
    return None


def _valid_id(request_id: str) -> bool:
    return (
        0 < len(request_id) <= 80
        and all(c in _ID_ALPHABET for c in request_id.lower())
        and request_id == request_id.strip()
    )


def _path(request_id: str) -> Path:
    return advice_dir() / f"{request_id}.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_atomic(path: Path, record: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def create(question: str, request_id: str | None = None,
           source: str = "cli") -> dict[str, Any]:
    question = (question or "").strip()
    if not question:
        return _err("empty_question")
    bad = _check_text(question, MAX_QUESTION_BYTES, "question")
    if bad:
        return bad
    rid = request_id or f"adv-{uuid.uuid4().hex[:12]}"
    if not _valid_id(rid):
        return _err("invalid_id")
    outstanding = list_pending(include_stale=True)["pending"]
    if len(outstanding) >= MAX_OUTSTANDING:
        return _err("mailbox_full", outstanding=len(outstanding),
                    limit=MAX_OUTSTANDING,
                    hint="Nothing is draining the mailbox. Answer or let the "
                         "pending requests expire before asking more.")
    same_source = sum(1 for p in outstanding
                      if (_read(_path(p["id"])) or {}).get("source") == source)
    if same_source >= MAX_OUTSTANDING_PER_SOURCE:
        return _err("source_quota_exceeded", source=source,
                    outstanding=same_source,
                    limit=MAX_OUTSTANDING_PER_SOURCE)
    d = advice_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = _path(rid)
    if path.exists():
        return _err("id_exists", id=rid)
    record = {
        "id": rid,
        "status": "pending",
        "question": question,
        "source": source,
        "created_at": time.time(),
        "answer": None,
        "answered_at": None,
        "answer_idempotency_key": None,
    }
    _write_atomic(path, record)
    return {"ok": True, "id": rid}


def list_pending(include_stale: bool = False) -> dict[str, Any]:
    out = []
    now = time.time()
    if advice_dir().is_dir():
        for p in sorted(advice_dir().glob("*.json")):
            if p.name.startswith("."):
                continue  # .state.json is bookkeeping, not a request
            rec = _read(p)
            if not rec or rec.get("status") != "pending":
                continue
            age = now - float(rec.get("created_at") or now)
            if age > pending_ttl_seconds() and not include_stale:
                continue
            out.append({
                "id": rec["id"],
                "question": rec["question"],
                "age_seconds": int(age),
            })
    return {"ok": True, "pending": out}


def _state_path() -> Path:
    return advice_dir() / ".state.json"


def _record_drain() -> None:
    """Remember when a responder last delivered an answer.

    The system can be correct and still strand a caller if nothing on the
    ChatGPT side is running. Time since the last drain is the signal that
    separates "thinking" from "nobody is listening".
    """
    try:
        _write_atomic(_state_path(), {"last_drain_at": time.time()})
    except OSError:
        pass


def liveness() -> dict[str, Any]:
    """Is anything actually answering? Ages in seconds, None when unknown."""
    pending = list_pending(include_stale=True)["pending"]
    last = None
    rec = _read(_state_path())
    if rec:
        last = rec.get("last_drain_at")
    now = time.time()
    return {
        "pending": len(pending),
        "oldest_pending_seconds": max((p["age_seconds"] for p in pending),
                                      default=0),
        "last_drain_at": last,
        "seconds_since_drain": int(now - last) if last else None,
        "ttl_seconds": pending_ttl_seconds(),
    }


def _claim_path(request_id: str) -> Path:
    return advice_dir() / f"{request_id}.claim"


def is_expired(rec: dict[str, Any]) -> bool:
    if rec.get("status") != "pending":
        return False
    return (time.time() - float(rec.get("created_at") or 0)) > pending_ttl_seconds()


def _take_claim(request_id: str, responder: str) -> bool:
    """Acquire the claim lease. True if this caller now owns the answer."""
    path = _claim_path(request_id)
    payload = json.dumps({"responder": responder, "claimed_at": time.time()})
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, payload.encode())
        os.close(fd)
        return True
    except FileExistsError:
        pass
    # Someone claimed it. Take over only if their lease expired.
    try:
        held = json.loads(path.read_text())
        claimed_at = float(held.get("claimed_at") or 0)
    except (OSError, ValueError):
        claimed_at = 0.0
    if time.time() - claimed_at <= CLAIM_LEASE_SECONDS:
        return False
    try:
        path.unlink()
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, payload.encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Another taker won the same race; exactly one of us proceeds.
        return False


def collect_garbage() -> dict[str, Any]:
    """Delete expired, unanswered requests after the grace window.

    Runs on the bridge process, which owns mailbox state. An answered request
    is never collected here; its asker may still be reading it.
    """
    removed = []
    if not advice_dir().is_dir():
        return {"ok": True, "removed": removed}
    now = time.time()
    horizon = pending_ttl_seconds() + GC_GRACE_SECONDS
    for p in advice_dir().glob("*.json"):
        if p.name.startswith("."):
            continue
        rec = _read(p)
        if not rec or rec.get("status") != "pending":
            continue
        if now - float(rec.get("created_at") or now) <= horizon:
            continue
        for victim in (p, _claim_path(rec["id"]), p.with_suffix(".tmp")):
            try:
                victim.unlink()
            except OSError:
                pass
        removed.append(rec["id"])
    return {"ok": True, "removed": removed}


def respond(request_id: str, answer: str,
            idempotency_key: str | None = None,
            responder: str = "connector") -> dict[str, Any]:
    answer = (answer or "").strip()
    if not answer:
        return _err("empty_answer")
    bad = _check_text(answer, MAX_ANSWER_BYTES, "answer")
    if bad:
        return bad
    if not _valid_id(request_id):
        return _err("invalid_id")
    path = _path(request_id)
    rec = _read(path)
    if rec is None:
        return _err("unknown_id", id=request_id)
    if rec.get("status") == "answered":
        if idempotency_key and rec.get("answer_idempotency_key") == idempotency_key:
            return {"ok": True, "id": request_id, "duplicate": True}
        return _err("already_answered", id=request_id)
    if is_expired(rec):
        # The asker has already given up. Storing this would look like valid
        # advice to whoever reads the record next.
        audit("advice_late_answer", session_id=MAILBOX_SESSION_ID,
              result="expired_request", source=responder,
              detail=f"request_id={request_id}")
        return _err("expired_request", id=request_id,
                    hint="The request expired before this answer arrived; the "
                         "asker has stopped waiting. Ask it to re-request.")
    # Claim before writing so the pending -> answered transition is atomic
    # across concurrent supervisors: one request is answered once.
    if not _take_claim(request_id, responder):
        current = _read(path) or {}
        if (idempotency_key
                and current.get("answer_idempotency_key") == idempotency_key):
            return {"ok": True, "id": request_id, "duplicate": True}
        return _err("already_answered", id=request_id)
    rec.update(
        status="answered",
        answer=answer,
        answered_at=time.time(),
        answer_idempotency_key=idempotency_key,
        # Provenance travels beside the body, never mixed into it.
        responder=responder,
        provenance="external-advice",
    )
    _write_atomic(path, rec)
    _record_drain()
    return {"ok": True, "id": request_id}


def get(request_id: str) -> dict[str, Any]:
    if not _valid_id(request_id):
        return _err("invalid_id")
    rec = _read(_path(request_id))
    if rec is None:
        return _err("unknown_id", id=request_id)
    return {"ok": True, "request": rec}


# -- virtual session surface ----------------------------------------------
#
# COMPATIBILITY SHIM, not a faithful session.
#
# The connector's tool schema is captured when the connector is created and
# never refreshes, so a client registered before advice_* existed can never
# see those tools. The mailbox therefore also presents itself as a session so
# it is reachable through the already-cached read/send tools.
#
# This deliberately breaks the session invariant that a transcript is an
# append-only, cursor-addressable stream: the mailbox view is a mutable
# snapshot that changes as requests are answered. Rather than fake the
# contract, the shim refuses the parts it cannot honor: nonzero cursors and
# any expected_generation are typed errors, not silent no-ops.
#
# Remove the shim once every supported connector registration is known to
# carry a schema with advice_list_pending / advice_respond. The dedicated
# tools are the canonical API.

MAILBOX_SESSION_ID = "advice-mailbox"

# One exact envelope, no optional forms: a permissive parse lets quoted or
# copied text ahead of the intended id decide the route.
_ANSWER_HEADER = re.compile(r"^id:(?P<id>[A-Za-z0-9_-]{1,80})[ \t]*(?:\n|$)")


def mailbox_entry() -> dict[str, Any]:
    """Registry-shaped entry for the mailbox pseudo-session."""
    return {
        "session_id": MAILBOX_SESSION_ID,
        "generation": 1,
        "transport": "mailbox",
        "capabilities": ["READ", "WRITE"],
        "cwd": str(advice_dir()),
        "cmd": ["advice-mailbox"],
        "started_at": None,
        "last_activity_at": None,
        "thread_id": None,
        "tty": None,
        "exit_code": None,
        "status_hint": "RUNNING",
    }


def render_pending() -> str:
    """Pending questions as plain text, for read_recent."""
    pending = list_pending()["pending"]
    if not pending:
        return (
            "ADVICE MAILBOX: no pending requests.\n"
            "Local agents park questions here. When one appears, answer it by "
            f"sending a message to session {MAILBOX_SESSION_ID!r} whose first "
            "line is exactly 'id:<request-id>' and whose remaining lines are "
            "the answer.\n"
        )
    out = [
        f"ADVICE MAILBOX: {len(pending)} pending request(s).",
        "To answer, send a message to this session whose FIRST LINE is exactly "
        "'id:<request-id>' (no other form is accepted) and whose every "
        "following line is the answer, delivered verbatim to the waiting "
        "agent. One message per request.",
        "This session is a mailbox, not a transcript: reads are snapshots, so "
        "cursors and expected_generation are rejected rather than ignored.",
        "",
    ]
    for req in pending:
        out += [
            f"=== REQUEST id={req['id']} (waiting {req['age_seconds']}s) ===",
            f"reply first line must be: id:{req['id']}",
            req["question"],
            "=== END REQUEST ===",
            "",
        ]
    return "\n".join(out)


def answer_via_message(message: str,
                       idempotency_key: str | None = None) -> dict[str, Any]:
    """Route a session-style message to respond().

    The first line must be exactly ``id:<request-id>``. The id is never
    inferred from question or answer text, and it must name a request that is
    currently pending, so quoted content cannot redirect an answer.
    """
    m = _ANSWER_HEADER.match(message or "")
    if not m:
        return _err(
            "missing_request_id",
            hint="First line must be exactly 'id:<request-id>', then the answer.",
        )
    rid = m.group("id")
    current = get(rid)
    if not current.get("ok"):
        return current
    if current["request"]["status"] != "pending":
        return _err("already_answered", id=rid)
    body = message[m.end():].strip()
    if not body:
        return _err("empty_answer", id=rid,
                    hint="Put the answer on the lines after the id line.")
    res = respond(rid, body, idempotency_key=idempotency_key)
    if res.get("ok"):
        res["session_id"] = MAILBOX_SESSION_ID
    return res


def wait(request_id: str, timeout_seconds: float = DEFAULT_WAIT_SECONDS,
         poll_seconds: float = DEFAULT_POLL_SECONDS) -> dict[str, Any]:
    timeout_seconds = max(0.0, min(float(timeout_seconds), MAX_WAIT_SECONDS))
    deadline = time.monotonic() + timeout_seconds
    while True:
        res = get(request_id)
        if not res.get("ok"):
            return res
        if res["request"]["status"] == "answered":
            return res
        if time.monotonic() >= deadline:
            return _err("timeout", id=request_id,
                        waited_seconds=int(timeout_seconds))
        time.sleep(poll_seconds)
