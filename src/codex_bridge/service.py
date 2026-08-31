"""Transport-agnostic session operations.

This is the single implementation behind both the MCP tools and the CLI.
Every operation resolves the session from the registry, dispatches on its
transport ("pty" -> launcher control socket, "native" -> codex queue +
rollout file) and returns plain dicts. Errors are structured, never raised,
so both surfaces report them identically.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from . import advice, control, native, paths, registry
from .auditlog import audit
from .idempotency import IdempotencyStore
from .transcript import read_slice, strip_ansi

DEFAULT_READ_LIMIT = 4000
MAX_READ_LIMIT = 1 << 16
MAX_MESSAGE_BYTES = 256 * 1024

CAPS = {"pty": ["READ", "WRITE", "INTERRUPT"], "native": ["READ", "WRITE"]}


def _err(code: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, **extra}


def remote_steering_allowed() -> bool:
    """Whether a remote caller may write to or interrupt a Codex session.

    One credential currently reaches every capability. The first privilege
    line goes around mutation of live sessions: answering the mailbox is
    data, but queueing a message into someone's editor is authority.
    Default deny, so a leaked token cannot steer; the operator opts in.
    """
    return os.environ.get("BRIDGE_REMOTE_STEERING", "").lower() in (
        "1", "true", "allow", "yes")


def _caps(entry: dict[str, Any]) -> list[str]:
    return entry.get("capabilities") or CAPS.get(entry.get("transport", "pty"), ["READ"])


# -- session summaries -----------------------------------------------------


def _pty_summary(entry: dict[str, Any]) -> dict[str, Any]:
    live = control.try_status(entry["socket_path"])
    if live:
        status = live["status"]
        cursor = live["cursor"]
    elif entry.get("status_hint") == "EXITED":
        status = "EXITED"
        p = Path(entry["transcript_path"])
        from .transcript import read_base_offset

        cursor = read_base_offset(p) + (p.stat().st_size if p.exists() else 0)
    elif registry.pid_alive(entry.get("launcher_pid")):
        status = "UNKNOWN"
        cursor = None
    else:
        status = "EXITED"
        cursor = None
    return {"status": status, "cursor": cursor, "pid": entry.get("pid")}


_healing: set[str] = set()


def _autoheal_native(entry: dict[str, Any]) -> dict[str, Any]:
    """Re-bind a native session whose codex process was replaced.

    The rollout file identifies one conversation. A different pid holding
    that same file is that same conversation resumed after a restart, so
    binding to it is a registry correction, not a change of target. The
    generation still increments, which is what rejects writes prepared
    against the dead process.

    Without this, a client that cannot run the CLI (the ChatGPT mobile app,
    a scheduled task) sees stale_binding on every call with no way out. Set
    BRIDGE_NO_AUTOHEAL=1 to require an explicit readopt instead.
    """
    if os.environ.get("BRIDGE_NO_AUTOHEAL"):
        return entry
    sid = entry["session_id"]
    # readopt ends by rendering a session view, which lands back here. Guard
    # the reentry rather than letting a rebind recurse into itself.
    if sid in _healing:
        return entry
    _healing.add(sid)
    try:
        healed = readopt(sid)
    finally:
        _healing.discard(sid)
    if not healed.get("ok"):
        return entry
    fresh = registry.get(sid) or entry
    audit("autoheal", session_id=sid, generation=fresh.get("generation"),
          source="bridge",
          detail=f"rebound {entry.get('pid')} -> {fresh.get('pid')}")
    return fresh


def _healed(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the entry, re-bound first if its codex process was replaced."""
    if entry.get("transport") != "native":
        return entry
    if registry.pid_alive(entry.get("pid")):
        return entry
    # Fail closed on ambiguity: heal only when exactly one codex process
    # holds the rollout. Zero means exited; more than one means something
    # unusual (a copied rollout, two resumes) that a human should look at.
    if len(native.codex_holders(Path(entry["rollout_path"]))) != 1:
        return entry
    return _autoheal_native(entry)


def _native_summary(entry: dict[str, Any]) -> dict[str, Any]:
    rollout = Path(entry["rollout_path"])
    pid = entry.get("pid")
    alive = registry.pid_alive(pid)
    if not alive:
        holders = native.pids_holding(rollout)
        if holders:
            return {"status": "UNKNOWN", "cursor": _size(rollout), "pid": pid,
                    "note": f"bound pid {pid} is gone but pid(s) {holders} hold the "
                            "rollout; run `codex-remote readopt` to re-bind"}
    status, last = native.rollout_status(rollout, pid, alive)
    return {"status": status, "cursor": _size(rollout), "pid": pid,
            "last_activity_at": last}


def _size(p: Path) -> int | None:
    try:
        return p.stat().st_size
    except OSError:
        return None


def session_view(entry: dict[str, Any]) -> dict[str, Any]:
    entry = _healed(entry)
    transport = entry.get("transport", "pty")
    summary = _native_summary(entry) if transport == "native" else _pty_summary(entry)
    view = {
        "session_id": entry["session_id"],
        "generation": entry["generation"],
        "transport": transport,
        "capabilities": _caps(entry),
        "cwd": entry.get("cwd"),
        "cmd": entry.get("cmd"),
        "started_at": entry.get("started_at"),
        "last_activity_at": entry.get("last_activity_at"),
        "thread_id": entry.get("thread_id"),
        "tty": entry.get("tty"),
        "exit_code": entry.get("exit_code"),
    }
    view.update(summary)
    return view


def _mailbox_view() -> dict[str, Any]:
    # The bridge owns mailbox state, so it is the right place to retire
    # abandoned requests. Listing is the natural periodic touchpoint.
    advice.collect_garbage()
    entry = advice.mailbox_entry()
    pending = advice.list_pending()["pending"]
    return {
        **entry,
        "status": "WAITING_FOR_INPUT" if pending else "RUNNING",
        "pending_requests": len(pending),
    }


def list_sessions() -> dict[str, Any]:
    sessions = [session_view(e) for e in registry.load_all().values()]
    sessions.append(_mailbox_view())
    return {"ok": True, "sessions": sessions}


def get_session(session_id: str) -> dict[str, Any]:
    if session_id == advice.MAILBOX_SESSION_ID:
        return {"ok": True, "session": _mailbox_view()}
    entry = registry.get(session_id)
    if entry is None:
        return _err("unknown_session", session_id=session_id)
    return {"ok": True, "session": session_view(entry)}


# -- read ------------------------------------------------------------------


def read_recent(
    session_id: str,
    after_cursor: int | None = None,
    limit: int | None = None,
    plain: bool = True,
) -> dict[str, Any]:
    if session_id == advice.MAILBOX_SESSION_ID:
        if after_cursor:
            return _err("cursors_unsupported", session_id=session_id,
                        hint="The mailbox is a snapshot, not a transcript. "
                             "Read it with no cursor.")
        text = advice.render_pending()
        return {
            "ok": True,
            "session_id": session_id,
            "generation": 1,
            "status": "RUNNING",
            "from_cursor": 0,
            "next_cursor": len(text.encode()),
            "text": text,
            "truncated": False,
        }
    entry = registry.get(session_id)
    if entry is None:
        return _err("unknown_session", session_id=session_id)
    limit = min(int(limit or DEFAULT_READ_LIMIT), MAX_READ_LIMIT)
    transport = entry.get("transport", "pty")

    if transport == "native":
        rollout = Path(entry["rollout_path"])
        if not rollout.exists():
            return _err("transcript_unavailable", session_id=session_id)
        out = native.read_rollout(rollout, after_cursor, limit, plain)
        return {"ok": True, "session_id": session_id,
                "generation": entry["generation"], **out}

    live = control.try_status(entry["socket_path"])
    if live:
        resp = control.request(
            entry["socket_path"],
            {"op": "read", "after_cursor": after_cursor, "limit": limit},
        )
        if not resp.get("ok"):
            return resp
        raw = base64.b64decode(resp.pop("raw_b64", ""))
        resp["text"] = strip_ansi(raw) if plain else raw.decode("utf-8", errors="replace")
        return resp
    # Exited (or launcher gone): read the transcript file directly.
    p = Path(entry["transcript_path"])
    if not p.exists():
        return _err("transcript_unavailable", session_id=session_id)
    from_c, next_c, raw, truncated = read_slice(p, after_cursor, limit)
    return {
        "ok": True,
        "session_id": session_id,
        "generation": entry["generation"],
        "status": "EXITED" if entry.get("status_hint") == "EXITED" else "UNKNOWN",
        "from_cursor": from_c,
        "next_cursor": next_c,
        "text": strip_ansi(raw) if plain else raw.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


# -- send ------------------------------------------------------------------


def send_message(
    session_id: str,
    message: str,
    expected_generation: int | None = None,
    idempotency_key: str | None = None,
    source: str = "mcp",
) -> dict[str, Any]:
    if session_id == advice.MAILBOX_SESSION_ID:
        if expected_generation is not None:
            # A synthetic generation carries no concurrency meaning, and
            # accepting the field would imply session semantics that do not
            # hold here. Routing is by request id, which must be pending.
            return _err("generation_unsupported", session_id=session_id,
                        hint="The mailbox has no generation. Omit "
                             "expected_generation; routing is by request id.")
        res = advice.answer_via_message(message, idempotency_key=idempotency_key)
        audit("send", session_id=session_id, generation=1,
              result="ok" if res.get("ok") else res.get("error"),
              source=source, idempotency_key=idempotency_key)
        return res
    if source == "mcp" and not remote_steering_allowed():
        audit("send", session_id=session_id, result="remote_steering_disabled",
              source=source, idempotency_key=idempotency_key)
        return _err("remote_steering_disabled", session_id=session_id,
                    hint="Remote callers cannot write to Codex sessions. Set "
                         "BRIDGE_REMOTE_STEERING=allow on the daemon to "
                         "enable it. Answering the advice mailbox is "
                         "unaffected.")
    entry = registry.get(session_id)
    if entry is None:
        audit("send", session_id=session_id, result="unknown_session", source=source,
              idempotency_key=idempotency_key)
        return _err("unknown_session", session_id=session_id)
    if not isinstance(message, str) or not message:
        return _err("empty_message", session_id=session_id)
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        return _err("message_too_large", session_id=session_id,
                    max_bytes=MAX_MESSAGE_BYTES)
    if "WRITE" not in _caps(entry):
        audit("send", session_id=session_id, generation=entry["generation"],
              result="not_writable", source=source, idempotency_key=idempotency_key)
        return _err("NOT_WRITABLE", session_id=session_id,
                    capabilities=_caps(entry))
    if expected_generation is not None and int(expected_generation) != entry["generation"]:
        audit("send", session_id=session_id, generation=entry["generation"],
              result="generation_mismatch", source=source,
              idempotency_key=idempotency_key)
        return _err("generation_mismatch", session_id=session_id,
                    expected_generation=int(expected_generation),
                    actual_generation=entry["generation"])

    transport = entry.get("transport", "pty")
    if transport == "native":
        return _send_native(entry, message, expected_generation, idempotency_key, source)
    return _send_pty(entry, message, expected_generation, idempotency_key, source)


def _send_pty(entry, message, expected_generation, idempotency_key, source):
    session_id = entry["session_id"]
    if entry.get("status_hint") == "EXITED" or not registry.pid_alive(entry.get("launcher_pid")):
        audit("send", session_id=session_id, generation=entry["generation"],
              result="session_exited", source=source, idempotency_key=idempotency_key)
        return _err("session_exited", session_id=session_id)
    try:
        resp = control.request(
            entry["socket_path"],
            {
                "op": "send",
                "message": message,
                "expected_generation": expected_generation,
                "idempotency_key": idempotency_key,
            },
            timeout=15.0,
        )
    except control.ControlUnavailable as e:
        audit("send", session_id=session_id, generation=entry["generation"],
              result="launcher_unreachable", source=source,
              idempotency_key=idempotency_key)
        return _err("launcher_unreachable", session_id=session_id, detail=str(e))
    # The launcher already audit-logged and deduplicated the actual write.
    return resp


def _send_native(entry, message, expected_generation, idempotency_key, source):
    session_id = entry["session_id"]
    entry = _healed(entry)
    summary = _native_summary(entry)
    if summary["status"] in ("EXITED",):
        audit("send", session_id=session_id, generation=entry["generation"],
              result="session_exited", source=source, idempotency_key=idempotency_key)
        return _err("session_exited", session_id=session_id)
    if summary["status"] == "UNKNOWN":
        audit("send", session_id=session_id, generation=entry["generation"],
              result="stale_binding", source=source, idempotency_key=idempotency_key)
        return _err("stale_binding", session_id=session_id,
                    detail=summary.get("note", "process binding is stale; re-adopt"))
    idem = IdempotencyStore(session_id)
    if idempotency_key:
        verdict, stored = idem.check(str(idempotency_key), message)
        if verdict == "duplicate":
            resp = dict(stored or {})
            resp.update({"ok": True, "deduplicated": True})
            return resp
        if verdict == "conflict":
            return _err("idempotency_key_reuse", session_id=session_id)
    ok, detail = native.queue_message(entry["thread_id"], message)
    result = "ok" if ok else "queue_failed"
    audit("send", session_id=session_id, generation=entry["generation"],
          idempotency_key=idempotency_key, message=message, result=result,
          source=source)
    if not ok:
        return _err("queue_failed", session_id=session_id, detail=detail)
    resp = {
        "ok": True,
        "session_id": session_id,
        "generation": entry["generation"],
        "status": summary["status"],
        "cursor": summary.get("cursor"),
        "deduplicated": False,
        "delivery": "queued via codex queue; an idle session submits it as the "
                    "next turn, a busy session submits it when the current turn ends",
    }
    if idempotency_key:
        idem.record(str(idempotency_key), message, resp)
    registry.update(session_id, last_activity_at=registry.utcnow_iso())
    return resp


# -- interrupt -------------------------------------------------------------


def interrupt(session_id: str, expected_generation: int | None = None,
              source: str = "mcp") -> dict[str, Any]:
    if source == "mcp" and not remote_steering_allowed():
        return _err("remote_steering_disabled", session_id=session_id,
                    hint="Remote callers cannot interrupt Codex sessions. "
                         "Set BRIDGE_REMOTE_STEERING=allow on the daemon.")
    entry = registry.get(session_id)
    if entry is None:
        return _err("unknown_session", session_id=session_id)
    if "INTERRUPT" not in _caps(entry):
        return _err("interrupt_not_supported", session_id=session_id,
                    transport=entry.get("transport"),
                    capabilities=_caps(entry))
    if expected_generation is not None and int(expected_generation) != entry["generation"]:
        return _err("generation_mismatch", session_id=session_id,
                    expected_generation=int(expected_generation),
                    actual_generation=entry["generation"])
    try:
        resp = control.request(entry["socket_path"], {"op": "interrupt"})
    except control.ControlUnavailable as e:
        return _err("launcher_unreachable", session_id=session_id, detail=str(e))
    audit("interrupt", session_id=session_id, generation=entry["generation"],
          result="ok" if resp.get("ok") else str(resp.get("error")), source=source)
    return resp


def remove_session(session_id: str) -> dict[str, Any]:
    """Drop a registry entry. Registry-only: never signals any process.
    Live PTY sessions must be stopped first so a launcher does not keep
    running unregistered."""
    entry = registry.get(session_id)
    if entry is None:
        return _err("unknown_session", session_id=session_id)
    if entry.get("transport", "pty") == "pty" and entry.get("status_hint") != "EXITED" \
            and registry.pid_alive(entry.get("launcher_pid")):
        return _err("session_still_running", session_id=session_id,
                    detail="stop the launcher first")
    with registry._locked():
        data = registry._read_raw()
        data["sessions"].pop(session_id, None)
        registry._write_raw(data)
    audit("remove", session_id=session_id, generation=entry.get("generation"),
          source="cli")
    return {"ok": True, "removed": session_id}


# -- adopt (register a running native codex session) -----------------------


def adopt_native(
    session_id: str,
    thread_id: str,
    pid: int | None = None,
    read_only: bool = False,
    probe_info: bool = True,
) -> dict[str, Any]:
    try:
        registry.validate_session_id(session_id)
    except ValueError as e:
        return _err("invalid_session_id", detail=str(e))
    thread_id = thread_id.lower()
    if not native.THREAD_ID_RE.fullmatch(thread_id):
        return _err("invalid_thread_id", thread_id=thread_id)
    rollout = native.find_rollout(thread_id)
    if rollout is None:
        return _err("rollout_not_found", thread_id=thread_id)
    if pid is None:
        holders = native.pids_holding(rollout)
        pid = holders[0] if holders else None
    cwd = None
    tty = None
    if pid and probe_info:
        # Two batched lsof calls; skipped on the heal path, where the prior
        # entry already knows its cwd and tty and latency matters.
        for proc in native.discover():
            if proc["pid"] == pid:
                cwd = proc.get("cwd")
                tty = proc.get("tty")
    caps = ["READ"] if read_only else ["READ", "WRITE"]
    with registry._locked():
        data = registry._read_raw()
        prev = data["sessions"].get(session_id)
        if prev and prev.get("transport", "pty") != "native":
            return _err("session_id_in_use_by_pty_session", session_id=session_id)
        generation = (prev["generation"] + 1) if prev else 1
        entry = {
            "session_id": session_id,
            "generation": generation,
            "transport": "native",
            "capabilities": caps,
            "thread_id": thread_id,
            "rollout_path": str(rollout),
            "launcher_pid": None,
            "pid": pid,
            "cwd": cwd,
            "tty": tty,
            "cmd": ["codex"],
            "started_at": registry.utcnow_iso(),
            "last_activity_at": registry.utcnow_iso(),
            "socket_path": None,
            "transcript_path": None,
            "status_hint": "RUNNING" if pid else "UNKNOWN",
            "exit_code": None,
            "exited_at": None,
        }
        data["sessions"][session_id] = entry
        registry._write_raw(data)
    audit("adopt", session_id=session_id, generation=generation, source="cli",
          detail=f"thread={thread_id} pid={pid} caps={','.join(caps)}")
    return {"ok": True, "session": session_view(entry)}


def readopt(session_id: str) -> dict[str, Any]:
    """Re-bind a native session to whatever process now owns its rollout.

    For use after a reboot or Codex restart: same session_id, same thread,
    new pid, generation + 1. Registry-only; never touches the process.
    Refuses when the currently bound pid is still alive (nothing to fix).
    """
    entry = registry.get(session_id)
    if entry is None:
        return _err("unknown_session", session_id=session_id)
    if entry.get("transport", "pty") != "native":
        return _err("not_a_native_session", session_id=session_id)
    if registry.pid_alive(entry.get("pid")):
        return {"ok": True, "unchanged": True, "session": session_view(entry),
                "detail": "bound pid is still alive; no re-adopt needed"}
    holders = native.codex_holders(Path(entry["rollout_path"]))
    if len(holders) > 1:
        return _err("ambiguous_holders", session_id=session_id, pids=holders,
                    hint="More than one codex process holds this rollout. "
                         "Pick one explicitly: codex-remote adopt --pid <pid>")
    res = adopt_native(session_id, entry["thread_id"],
                       pid=holders[0] if holders else None,
                       read_only="WRITE" not in _caps(entry),
                       probe_info=False)
    if res.get("ok"):
        # Keep what the dead binding knew; the conversation did not move.
        registry.update(session_id, cwd=entry.get("cwd"), tty=entry.get("tty"))
    return res


def readopt_all() -> dict[str, Any]:
    """Re-bind every native session whose bound pid is gone. Used by
    bridge-up at login so a reboot heals without manual steps."""
    results = {}
    for sid, entry in registry.load_all().items():
        if entry.get("transport") != "native":
            continue
        if registry.pid_alive(entry.get("pid")):
            continue
        results[sid] = readopt(sid)
    return {"ok": True, "readopted": results}
