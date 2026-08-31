"""Bounded append-only transcript with stable absolute cursors.

A cursor is the absolute number of bytes emitted since this generation
started. The on-disk file only keeps the most recent tail: when it grows past
max_bytes it is rewritten (atomically) to the last keep_bytes, and
base_offset in the sidecar meta file records how many bytes were dropped.
Cursors therefore never move backwards and survive rotation; reads below
base_offset are clamped and flagged truncated.

The launcher process is the sole writer. Readers (the MCP server, for EXITED
sessions) open the file per read and derive total = base_offset + file size.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_KEEP_BYTES = 1_000_000

# CSI, OSC and lone ESC sequences produced by TUI redraws.
_ANSI_RE = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    rb"|\x1b[@-Z\\-_]"  # other C1 escapes
)


def strip_ansi(data: bytes) -> str:
    cleaned = _ANSI_RE.sub(b"", data)
    text = cleaned.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of blank lines left behind by full-screen redraws.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def read_base_offset(path: Path) -> int:
    try:
        with open(_meta_path(path)) as f:
            return int(json.load(f).get("base_offset", 0))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return 0


def _write_base_offset(path: Path, base_offset: int) -> None:
    tmp = _meta_path(path).with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump({"base_offset": base_offset}, f)
    os.replace(tmp, _meta_path(path))


def read_slice(
    path: Path, after_cursor: int | None, limit: int
) -> tuple[int, int, bytes, bool]:
    """Read up to limit bytes starting at absolute cursor after_cursor.

    after_cursor None means "the last limit bytes". Returns
    (from_cursor, next_cursor, raw_bytes, truncated) where truncated is True
    when the requested cursor fell below what is still on disk.
    """
    base = read_base_offset(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    total = base + size
    if after_cursor is None:
        start = max(base, total - limit)
        truncated = False
    else:
        start = min(max(after_cursor, 0), total)
        truncated = start < base
        start = max(start, base)
    end = min(start + limit, total)
    if end <= start or size == 0:
        return start, start, b"", truncated
    with open(path, "rb") as f:
        f.seek(start - base)
        raw = f.read(end - start)
    return start, start + len(raw), raw, truncated


class TranscriptStore:
    """Writer-side handle used by the launcher (sole writer)."""

    def __init__(
        self,
        path: Path,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep_bytes: int = DEFAULT_KEEP_BYTES,
    ) -> None:
        if keep_bytes >= max_bytes:
            raise ValueError("keep_bytes must be < max_bytes")
        self.path = path
        self.max_bytes = max_bytes
        self.keep_bytes = keep_bytes
        path.parent.mkdir(parents=True, exist_ok=True)
        self.base_offset = read_base_offset(path)
        self._f = open(path, "ab")

    @property
    def total(self) -> int:
        return self.base_offset + self._f.tell()

    def append(self, data: bytes) -> int:
        self._f.write(data)
        self._f.flush()
        if self._f.tell() > self.max_bytes:
            self._rotate()
        return self.total

    def _rotate(self) -> None:
        self._f.close()
        size = self.path.stat().st_size
        with open(self.path, "rb") as f:
            f.seek(size - self.keep_bytes)
            tail = f.read()
        tmp = self.path.with_suffix(".rotating")
        with open(tmp, "wb") as f:
            f.write(tail)
        os.replace(tmp, self.path)
        self.base_offset += size - len(tail)
        _write_base_offset(self.path, self.base_offset)
        self._f = open(self.path, "ab")

    def read(self, after_cursor: int | None, limit: int) -> tuple[int, int, bytes, bool]:
        return read_slice(self.path, after_cursor, limit)

    def close(self) -> None:
        self._f.close()
