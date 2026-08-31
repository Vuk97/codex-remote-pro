"""Filesystem layout for the bridge.

Everything lives under BRIDGE_HOME (default ~/.codex-session-bridge) so tests
can point the whole system at a temp directory with one env var.
"""

from __future__ import annotations

import os
from pathlib import Path


def bridge_home() -> Path:
    return Path(os.environ.get("BRIDGE_HOME", str(Path.home() / ".codex-session-bridge")))


def registry_path() -> Path:
    return bridge_home() / "sessions.json"


def registry_lock_path() -> Path:
    return bridge_home() / "sessions.lock"


def sockets_dir() -> Path:
    return bridge_home() / "sockets"


def transcripts_dir() -> Path:
    return bridge_home() / "transcripts"


def socket_path(session_id: str) -> Path:
    return sockets_dir() / f"{session_id}.sock"


def transcript_path(session_id: str, generation: int) -> Path:
    return transcripts_dir() / f"{session_id}.g{generation}.log"


def ensure_dirs() -> None:
    for d in (bridge_home(), sockets_dir(), transcripts_dir()):
        d.mkdir(parents=True, exist_ok=True)
