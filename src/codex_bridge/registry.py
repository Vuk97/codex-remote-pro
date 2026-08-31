"""Persistent session registry.

sessions.json binds a stable session_id to a generation, process identity,
cwd, socket path and transcript path. A restart of the same session_id
increments generation. All read-modify-write cycles hold an exclusive flock
on a sidecar lock file and replace the file atomically.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import paths

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

VALID_STATUSES = ("RUNNING", "BUSY", "WAITING_FOR_INPUT", "EXITED", "UNKNOWN")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_session_id(session_id: str) -> None:
    if not SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"invalid session_id {session_id!r}: use 1-64 chars of [A-Za-z0-9._-], "
            "starting with an alphanumeric"
        )


@contextmanager
def _locked() -> Iterator[None]:
    paths.ensure_dirs()
    with open(paths.registry_lock_path(), "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_raw() -> dict[str, Any]:
    try:
        with open(paths.registry_path()) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "sessions": {}}
    if not isinstance(data, dict) or "sessions" not in data:
        return {"version": 1, "sessions": {}}
    return data


def _write_raw(data: dict[str, Any]) -> None:
    tmp = paths.registry_path().with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, paths.registry_path())


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_all() -> dict[str, dict[str, Any]]:
    with _locked():
        return _read_raw()["sessions"]


def get(session_id: str) -> dict[str, Any] | None:
    return load_all().get(session_id)


def register(
    session_id: str,
    *,
    launcher_pid: int,
    child_pid: int,
    cwd: str,
    cmd: list[str],
    paste_mode: str,
) -> dict[str, Any]:
    """Create or roll over a session entry; returns the new entry.

    Raises RuntimeError if a live launcher already owns this session_id.
    """
    validate_session_id(session_id)
    with _locked():
        data = _read_raw()
        prev = data["sessions"].get(session_id)
        if prev and prev.get("status_hint") != "EXITED" and pid_alive(prev.get("launcher_pid")):
            raise RuntimeError(
                f"session {session_id!r} is already running under launcher pid "
                f"{prev['launcher_pid']} (generation {prev['generation']}); "
                "stop it first or pick a different --session id"
            )
        generation = (prev["generation"] + 1) if prev else 1
        entry = {
            "session_id": session_id,
            "generation": generation,
            "launcher_pid": launcher_pid,
            "pid": child_pid,
            "cwd": cwd,
            "cmd": cmd,
            "paste_mode": paste_mode,
            "started_at": utcnow_iso(),
            "last_activity_at": utcnow_iso(),
            "socket_path": str(paths.socket_path(session_id)),
            "transcript_path": str(paths.transcript_path(session_id, generation)),
            "status_hint": "RUNNING",
            "exit_code": None,
            "exited_at": None,
        }
        data["sessions"][session_id] = entry
        _write_raw(data)
        return entry


def update(session_id: str, **fields: Any) -> dict[str, Any] | None:
    with _locked():
        data = _read_raw()
        entry = data["sessions"].get(session_id)
        if entry is None:
            return None
        entry.update(fields)
        _write_raw(data)
        return entry


def mark_exited(session_id: str, generation: int, exit_code: int | None) -> None:
    with _locked():
        data = _read_raw()
        entry = data["sessions"].get(session_id)
        # Only the owning generation may mark itself exited; a newer generation
        # must not be clobbered by a stale launcher shutting down late.
        if entry is None or entry.get("generation") != generation:
            return
        entry["status_hint"] = "EXITED"
        entry["exit_code"] = exit_code
        entry["exited_at"] = utcnow_iso()
        _write_raw(data)
