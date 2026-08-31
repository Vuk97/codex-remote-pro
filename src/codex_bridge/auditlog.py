"""Append-only audit log: metadata and message hashes, never message bodies."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from . import paths
from .registry import utcnow_iso


def message_digest(message: str) -> dict[str, Any]:
    raw = message.encode("utf-8")
    return {
        "message_sha256": hashlib.sha256(raw).hexdigest(),
        "message_bytes": len(raw),
    }


def audit(
    op: str,
    *,
    session_id: str | None = None,
    generation: int | None = None,
    idempotency_key: str | None = None,
    message: str | None = None,
    result: str = "ok",
    source: str = "mcp",
    detail: str | None = None,
) -> None:
    event: dict[str, Any] = {
        "ts": utcnow_iso(),
        "op": op,
        "session_id": session_id,
        "generation": generation,
        "idempotency_key": idempotency_key,
        "result": result,
        "source": source,
    }
    if message is not None:
        event.update(message_digest(message))
    if detail:
        event["detail"] = detail[:500]
    paths.ensure_dirs()
    line = json.dumps(event, sort_keys=True) + "\n"
    # O_APPEND keeps concurrent writers (daemon + launchers) line-atomic for
    # small records.
    fd = os.open(paths.bridge_home() / "audit.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
