"""Unit tests: registry identity/generation and transcript cursoring."""

import pytest

from codex_bridge import paths, registry
from codex_bridge.transcript import TranscriptStore, read_slice


def test_session_creation_and_stable_id(bridge_home):
    e = registry.register(
        "gm-test", launcher_pid=999999, child_pid=999998, cwd="/tmp",
        cmd=["fake"], paste_mode="plain",
    )
    assert e["session_id"] == "gm-test"
    assert e["generation"] == 1
    got = registry.get("gm-test")
    assert got is not None and got["session_id"] == "gm-test"


def test_generation_increments_on_restart(bridge_home):
    registry.register("s", launcher_pid=999999, child_pid=1, cwd="/tmp",
                      cmd=["x"], paste_mode="plain")
    registry.mark_exited("s", 1, 0)
    e2 = registry.register("s", launcher_pid=999999, child_pid=2, cwd="/tmp",
                           cmd=["x"], paste_mode="plain")
    assert e2["generation"] == 2


def test_register_refuses_live_duplicate(bridge_home):
    import os
    registry.register("dup", launcher_pid=os.getpid(), child_pid=1, cwd="/tmp",
                      cmd=["x"], paste_mode="plain")
    with pytest.raises(RuntimeError, match="already running"):
        registry.register("dup", launcher_pid=os.getpid(), child_pid=2, cwd="/tmp",
                          cmd=["x"], paste_mode="plain")


def test_invalid_session_id_rejected(bridge_home):
    with pytest.raises(ValueError):
        registry.validate_session_id("../evil")
    with pytest.raises(ValueError):
        registry.validate_session_id("")


def test_transcript_cursoring(bridge_home, tmp_path):
    p = tmp_path / "t.log"
    ts = TranscriptStore(p)
    ts.append(b"hello ")
    ts.append(b"world")
    from_c, next_c, raw, trunc = ts.read(0, 100)
    assert (from_c, next_c, raw, trunc) == (0, 11, b"hello world", False)
    # Incremental read from a cursor.
    from_c, next_c, raw, _ = ts.read(6, 100)
    assert raw == b"world" and from_c == 6 and next_c == 11
    # Cursor beyond end returns empty at total.
    from_c, next_c, raw, _ = ts.read(999, 100)
    assert raw == b"" and next_c == 11
    # Default tail read.
    from_c, next_c, raw, _ = ts.read(None, 5)
    assert raw == b"world"
    ts.close()


def test_transcript_rotation_keeps_absolute_cursors(bridge_home, tmp_path):
    p = tmp_path / "t.log"
    ts = TranscriptStore(p, max_bytes=1000, keep_bytes=400)
    total_written = 0
    for i in range(30):
        chunk = (f"chunk-{i:03d}:" + "x" * 90 + "\n").encode()
        ts.append(chunk)
        total_written += len(chunk)
    assert ts.total == total_written
    assert p.stat().st_size < 1000
    # A cursor before base_offset is clamped and flagged truncated.
    from_c, next_c, raw, trunc = ts.read(0, 50)
    assert trunc is True and from_c == ts.base_offset
    # The newest data is still readable at its absolute cursor.
    from_c, next_c, raw, trunc = ts.read(total_written - 20, 100)
    assert trunc is False and raw.endswith(b"x\n") and next_c == total_written
    ts.close()
    # Reader-side read_slice agrees after reopen.
    from_c, next_c, raw, trunc = read_slice(p, total_written - 20, 100)
    assert len(raw) == 20 and next_c == total_written
