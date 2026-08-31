"""Client for the per-session control socket owned by a PTY launcher.

Protocol: one JSON request line in, one JSON response line out, then the
connection closes. Ops: status, read, send, interrupt.
"""

from __future__ import annotations

import json
import socket
from typing import Any


class ControlUnavailable(Exception):
    pass


def request(socket_path: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(socket_path)
            s.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as e:
        raise ControlUnavailable(str(e)) from e
    if not buf:
        raise ControlUnavailable("empty response from launcher")
    try:
        return json.loads(buf)
    except json.JSONDecodeError as e:
        raise ControlUnavailable(f"bad response from launcher: {e}") from e


def try_status(socket_path: str, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        resp = request(socket_path, {"op": "status"}, timeout=timeout)
        return resp if resp.get("ok") else None
    except ControlUnavailable:
        return None
