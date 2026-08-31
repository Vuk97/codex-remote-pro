import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE_REPL = Path(__file__).resolve().parent / "fake_repl.py"


@pytest.fixture(autouse=True)
def _allow_remote_steering(monkeypatch):
    """Most tests predate the steering gate and exercise the write path.

    Tests that assert the gate itself delete this env var explicitly.
    """
    monkeypatch.setenv("BRIDGE_REMOTE_STEERING", "allow")


@pytest.fixture()
def bridge_home(monkeypatch):
    # Unix socket paths are capped at 104 bytes on macOS, so keep BRIDGE_HOME
    # short instead of using pytest's deep tmp_path.
    home = Path(tempfile.mkdtemp(prefix="cbr-", dir="/tmp"))
    monkeypatch.setenv("BRIDGE_HOME", str(home))
    yield home
    shutil.rmtree(home, ignore_errors=True)


def wait_for(predicate, timeout=10.0, interval=0.05, desc="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {desc}")


class PtySession:
    """Helper that runs `codex-bridge run --headless` around the fake REPL."""

    def __init__(self, session_id: str, home: Path, paste_mode: str = "plain"):
        self.session_id = session_id
        self.home = home
        cmd = [
            sys.executable, "-m", "codex_bridge.cli", "run",
            "--session", session_id, "--headless", "--paste-mode", paste_mode,
            "--", sys.executable, "-u", str(FAKE_REPL),
        ]
        env = dict(os.environ, BRIDGE_HOME=str(home))
        self.proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.socket_path = home / "sockets" / f"{session_id}.sock"
        wait_for(self.socket_path.exists, desc="control socket")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


@pytest.fixture()
def pty_session(bridge_home):
    with PtySession("t1", bridge_home) as s:
        yield s
