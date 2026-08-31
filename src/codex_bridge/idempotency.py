"""Persistent, bounded idempotency records.

One JSON file per session under BRIDGE_HOME/idempotency/. Records survive
bridge restarts so a ChatGPT retry after a daemon or launcher restart is
still deduplicated. Entries are pruned by age and count.

Rules on send with an idempotency_key:
  - same key, same message hash    -> return the stored ack, do not resend
  - same key, different message    -> reject (idempotency_key_reuse)
  - unknown key                    -> perform the send, store the ack
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import paths

MAX_RECORDS = 1000
MAX_AGE_SECONDS = 7 * 24 * 3600


def _store_path(session_id: str) -> Path:
    d = paths.bridge_home() / "idempotency"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id}.json"


def _sha(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


class IdempotencyStore:
    def __init__(self, session_id: str) -> None:
        self.path = _store_path(session_id)

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        now = time.time()
        entries = {
            k: v for k, v in data.items() if now - v.get("ts", 0) <= MAX_AGE_SECONDS
        }
        if len(entries) > MAX_RECORDS:
            keep = sorted(entries.items(), key=lambda kv: kv[1].get("ts", 0))[-MAX_RECORDS:]
            entries = dict(keep)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(entries, f)
        tmp.replace(self.path)

    def check(self, key: str, message: str) -> tuple[str, dict[str, Any] | None]:
        """Returns (verdict, stored_response). verdict is one of
        'new', 'duplicate', 'conflict'."""
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            rec = self._load().get(key)
        if rec is None:
            return "new", None
        if rec.get("message_sha256") == _sha(message):
            return "duplicate", rec.get("response")
        return "conflict", None

    def record(self, key: str, message: str, response: dict[str, Any]) -> None:
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self._load()
            data[key] = {
                "ts": time.time(),
                "message_sha256": _sha(message),
                "response": response,
            }
            self._save(data)
