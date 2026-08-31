"""Codex-native transport: talk to real Codex CLI sessions without owning
their PTY.

Write path : `codex queue --thread <uuid> --message <text>` run as an argv
             subprocess (no shell, so no interpolation or quoting hazards;
             multiline text rides in a single argv element).
Read path  : bounded cursor slices of the session's rollout JSONL under
             $CODEX_HOME/sessions/. We never open that file for writing.
Identity   : the rollout filename embeds the thread UUID; the running codex
             process holds the rollout open, so `lsof <rollout>` binds a PID.

Verified against codex-cli 0.150.1 on this machine.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .transcript import read_slice

THREAD_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I
)

BUSY_WINDOW_SECONDS = 60.0


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def codex_bin() -> str:
    b = os.environ.get("BRIDGE_CODEX_BIN") or shutil.which("codex")
    if not b:
        raise RuntimeError("codex binary not found; set BRIDGE_CODEX_BIN")
    return b


def find_rollout(thread_id: str) -> Path | None:
    sessions = codex_home() / "sessions"
    if not sessions.is_dir():
        return None
    matches = list(sessions.glob(f"**/*{thread_id}*.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def pids_holding(path: Path) -> list[int]:
    try:
        out = subprocess.run(
            ["lsof", "-t", str(path)], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(line) for line in out.split() if line.strip().isdigit()]


def codex_holders(path: Path) -> list[int]:
    """Pids holding the rollout that are actually codex processes.

    pids_holding returns anything with the file open, including a tail or an
    editor. Re-binding to one of those would route messages into a process
    that cannot receive them, so the heal path filters by command.
    """
    out = []
    for pid in pids_holding(path):
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "/bin/codex" in cmd or cmd.strip().startswith("codex"):
            out.append(pid)
    return out


def discover() -> list[dict[str, Any]]:
    """Best-effort list of running interactive codex processes and the
    thread each one has open. Read-only inspection.

    One ps pass plus two batched lsof calls for every candidate at once. The
    per-pid variant took two lsof invocations per process, which on a machine
    running dozens of codex workers turned discovery into a minute-long stall.
    """
    try:
        ps = subprocess.run(
            ["ps", "-axo", "pid=,tty=,command="], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    by_pid: dict[int, dict[str, Any]] = {}
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, tty, cmd = parts
        if "/bin/codex" not in cmd or "codex-bridge" in cmd:
            continue
        if any(w in cmd for w in ("mcp-server", "app-server", "exec-server", "code-mode-host")):
            continue
        pid = int(pid_s)
        entry: dict[str, Any] = {"pid": pid, "tty": tty, "command": cmd[:120]}
        by_pid[pid] = entry
        found.append(entry)
    if not by_pid:
        return found
    pid_arg = ",".join(str(p) for p in by_pid)

    def _lsof(args: list[str]) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True,
                                  timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    current: dict[str, Any] | None = None
    for lline in _lsof(["lsof", "-p", pid_arg, "-Fpn"]).splitlines():
        if lline.startswith("p"):
            current = by_pid.get(int(lline[1:]))
        elif lline.startswith("n") and current is not None:
            if "/sessions/" in lline and lline.endswith(".jsonl"):
                m = THREAD_ID_RE.search(lline)
                if m:
                    current["thread_id"] = m.group(1).lower()
                    current["rollout_path"] = lline[1:]
    current = None
    for lline in _lsof(["lsof", "-a", "-p", pid_arg, "-d", "cwd", "-Fpn"]).splitlines():
        if lline.startswith("p"):
            current = by_pid.get(int(lline[1:]))
        elif lline.startswith("n") and current is not None:
            current["cwd"] = lline[1:]
    return found


def queue_message(thread_id: str, message: str, timeout: float = 60.0) -> tuple[bool, str]:
    """Queue a user message for the given codex thread. Returns (ok, detail)."""
    cmd = [codex_bin(), "queue", "--thread", thread_id, "--message", message]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "codex queue timed out"
    except OSError as e:
        return False, f"failed to run codex queue: {e}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, f"codex queue exited {proc.returncode}: {detail[:300]}"
    return True, (proc.stdout or "").strip()[:300]


def rollout_status(rollout_path: Path, pid: int | None, pid_alive: bool) -> tuple[str, str | None]:
    """(status, last_activity_iso). BUSY when the rollout was appended to
    recently; RUNNING when the process is alive but the rollout is quiet.
    WAITING_FOR_INPUT is not reported for native sessions because an idle
    prompt and a long-running tool look identical from outside."""
    try:
        mtime = rollout_path.stat().st_mtime
    except OSError:
        return ("EXITED" if not pid_alive else "UNKNOWN"), None
    last = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(mtime))
    if not pid_alive:
        return "EXITED", last
    if time.time() - mtime <= BUSY_WINDOW_SECONDS:
        return "BUSY", last
    return "RUNNING", last


def read_rollout(
    rollout_path: Path, after_cursor: int | None, limit: int, plain: bool
) -> dict[str, Any]:
    from_cursor, next_cursor, raw, truncated = read_slice(rollout_path, after_cursor, limit)
    text = raw.decode("utf-8", errors="replace")
    if plain:
        text = extract_plain(text)
    return {
        "from_cursor": from_cursor,
        "next_cursor": next_cursor,
        "text": text,
        "truncated": truncated,
    }


_PLAIN_TYPES = ("user_message", "agent_message", "message")


def extract_plain(jsonl_text: str) -> str:
    """Pull human-readable conversation text out of a rollout JSONL slice.
    Lines that fail to parse (slice boundaries) or carry non-message events
    are skipped; use plain=false to see the raw event stream."""
    out: list[str] = []
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = evt.get("payload", evt)
        ptype = payload.get("type", "")
        if ptype in _PLAIN_TYPES:
            text = payload.get("message") or payload.get("text")
            if not text and isinstance(payload.get("content"), list):
                text = "".join(
                    c.get("text", "") for c in payload["content"] if isinstance(c, dict)
                )
            if text:
                role = payload.get("role") or ("user" if "user" in ptype else "agent")
                role = "user" if role == "user" else "agent"
                out.append(f"[{role}] {text}")
    return "\n".join(out)
