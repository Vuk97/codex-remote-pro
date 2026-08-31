"""PTY launcher: runs a target command under a bridge-owned PTY.

One launcher process per session. It is the sole owner of the PTY master,
the sole writer of the transcript, and serves a per-session unix control
socket (status / read / send / interrupt). In interactive mode it also
relays the user's terminal to the PTY, so the session looks and feels like
a normal terminal run of the target command.

Message bytes are written straight to the PTY master; there is no shell in
the path, so message content is never interpreted. Multiline messages are
wrapped in bracketed-paste markers by default (what a TUI expects from a
paste) or written verbatim in plain mode.
"""

from __future__ import annotations

import base64
import collections
import fcntl
import json
import os
import pty
import selectors
import signal
import socket
import struct
import sys
import termios
import time
import tty as tty_mod
from typing import Any

from . import paths, registry
from .auditlog import audit
from .idempotency import IdempotencyStore
from .transcript import TranscriptStore

BUSY_WINDOW = 2.0
IDLE_WINDOW = 10.0
MAX_MESSAGE_BYTES = 256 * 1024
SUBMIT_DELAY = 0.3


def encode_message(message: str, paste_mode: str, submit: bool = True) -> bytes:
    data = message.encode("utf-8")
    if paste_mode == "bracketed" and ("\n" in message or "\r" in message):
        data = b"\x1b[200~" + data + b"\x1b[201~"
    if submit:
        data += b"\r"
    return data


class SessionHost:
    def __init__(
        self,
        session_id: str,
        cmd: list[str],
        cwd: str,
        paste_mode: str = "bracketed",
        interactive: bool = True,
    ) -> None:
        self.session_id = session_id
        self.cmd = cmd
        self.cwd = cwd
        self.paste_mode = paste_mode
        self.interactive = interactive and sys.stdin.isatty()
        self.child_pid = -1
        self.master_fd = -1
        self.generation = 0
        self.exit_code: int | None = None
        self.child_exited = False
        self.last_output = time.monotonic()
        self.last_send = 0.0
        self.transcript: TranscriptStore | None = None
        self.idem: IdempotencyStore | None = None
        self._saved_termios: list[Any] | None = None
        # Outbound queue for the PTY, drained by the main loop. Sends enqueue
        # here instead of writing synchronously so a large message can never
        # deadlock against unread child output. Items are [data, delay, not_before]:
        # delay holds the item back that many seconds after it reaches the
        # head, which lets the submit Enter arrive as its own keystroke after
        # the message body (TUIs treat a fast text+CR burst as a paste and
        # would swallow the CR as a soft newline instead of submitting).
        self.out_queue: collections.deque = collections.deque()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        paths.ensure_dirs()
        pid, master = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(self.cwd)
                os.execvp(self.cmd[0], self.cmd)
            except OSError as e:
                os.write(2, f"codex-bridge: exec failed: {e}\n".encode())
                os._exit(127)
        self.child_pid = pid
        self.master_fd = master
        self._set_winsize()
        entry = registry.register(
            self.session_id,
            launcher_pid=os.getpid(),
            child_pid=pid,
            cwd=self.cwd,
            cmd=self.cmd,
            paste_mode=self.paste_mode,
        )
        self.generation = entry["generation"]
        self.transcript = TranscriptStore(paths.transcript_path(self.session_id, self.generation))
        self.idem = IdempotencyStore(self.session_id)
        sock_path = paths.socket_path(self.session_id)
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        self.listener.listen(8)
        self.listener.setblocking(False)
        audit("session_start", session_id=self.session_id, generation=self.generation,
              source="launcher", detail=" ".join(self.cmd)[:200])

    def _set_winsize(self) -> None:
        if self.interactive:
            try:
                ws = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, ws)
                return
            except OSError:
                pass
        ws = struct.pack("HHHH", 40, 120, 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, ws)
        except OSError:
            pass

    # -- status ------------------------------------------------------------

    def status(self) -> str:
        if self.child_exited:
            return "EXITED"
        idle = time.monotonic() - self.last_output
        if idle < BUSY_WINDOW:
            return "BUSY"
        if idle > IDLE_WINDOW:
            return "WAITING_FOR_INPUT"
        return "RUNNING"

    def _status_payload(self) -> dict[str, Any]:
        assert self.transcript is not None
        return {
            "ok": True,
            "session_id": self.session_id,
            "generation": self.generation,
            "status": self.status(),
            "pid": self.child_pid,
            "cursor": self.transcript.total,
            "exit_code": self.exit_code,
        }

    # -- control ops -------------------------------------------------------

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "status":
            return self._status_payload()
        if op == "read":
            assert self.transcript is not None
            limit = min(int(req.get("limit") or 4000), 1 << 16)
            after = req.get("after_cursor")
            after = int(after) if after is not None else None
            from_c, next_c, raw, truncated = self.transcript.read(after, limit)
            return {
                "ok": True,
                "session_id": self.session_id,
                "generation": self.generation,
                "status": self.status(),
                "from_cursor": from_c,
                "next_cursor": next_c,
                "raw_b64": base64.b64encode(raw).decode(),
                "truncated": truncated,
            }
        if op == "send":
            return self._handle_send(req)
        if op == "interrupt":
            if self.child_exited:
                return {"ok": False, "error": "session_exited"}
            # Ctrl-C jumps the output queue.
            self._enqueue(b"\x03", front=True)
            self._drain_pending()
            audit("interrupt", session_id=self.session_id, generation=self.generation,
                  source="launcher")
            return {"ok": True, "session_id": self.session_id, "generation": self.generation}
        return {"ok": False, "error": "unknown_op"}

    def _handle_send(self, req: dict[str, Any]) -> dict[str, Any]:
        assert self.transcript is not None and self.idem is not None
        message = req.get("message")
        if not isinstance(message, str) or not message:
            return {"ok": False, "error": "empty_message"}
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            return {"ok": False, "error": "message_too_large"}
        if self.child_exited:
            return {"ok": False, "error": "session_exited"}
        expected = req.get("expected_generation")
        if expected is not None and int(expected) != self.generation:
            return {
                "ok": False,
                "error": "generation_mismatch",
                "expected_generation": int(expected),
                "actual_generation": self.generation,
            }
        key = req.get("idempotency_key")
        if key:
            verdict, stored = self.idem.check(str(key), message)
            if verdict == "duplicate":
                resp = dict(stored or {})
                resp.update({"ok": True, "deduplicated": True})
                return resp
            if verdict == "conflict":
                return {"ok": False, "error": "idempotency_key_reuse"}
        self._enqueue(encode_message(message, self.paste_mode, submit=False))
        # Enter goes separately after a beat so paste-detecting TUIs submit.
        self._enqueue(b"\r", delay=SUBMIT_DELAY)
        self._drain_pending()
        self.last_send = time.monotonic()
        resp = {
            "ok": True,
            "session_id": self.session_id,
            "generation": self.generation,
            "cursor": self.transcript.total,
            "status": self.status(),
            "delivery": "accepted; bytes are flushed to the PTY as the child drains input",
            "deduplicated": False,
        }
        if key:
            self.idem.record(str(key), message, resp)
        audit("send", session_id=self.session_id, generation=self.generation,
              idempotency_key=str(key) if key else None, message=message, source="launcher")
        return resp

    # -- main loop ---------------------------------------------------------

    def _enqueue(self, data: bytes, delay: float = 0.0, front: bool = False) -> None:
        item = [data, delay, None]
        if front:
            self.out_queue.appendleft(item)
        else:
            self.out_queue.append(item)

    def _drain_pending(self) -> None:
        while self.out_queue:
            item = self.out_queue[0]
            data, delay, not_before = item
            now = time.monotonic()
            if not_before is None:
                item[2] = not_before = now + delay
            if now < not_before:
                return
            try:
                n = os.write(self.master_fd, data[:8192])
            except BlockingIOError:
                return
            except OSError:
                self.out_queue.clear()
                return
            if n == len(data):
                self.out_queue.popleft()
            else:
                item[0] = data[n:]

    def run(self) -> int:
        self.start()
        assert self.transcript is not None
        os.set_blocking(self.master_fd, False)
        sel = selectors.DefaultSelector()
        sel.register(self.master_fd, selectors.EVENT_READ, "master")
        sel.register(self.listener, selectors.EVENT_READ, "accept")
        if self.interactive:
            self._saved_termios = termios.tcgetattr(sys.stdin.fileno())
            tty_mod.setraw(sys.stdin.fileno())
            sel.register(sys.stdin.fileno(), selectors.EVENT_READ, "stdin")
            signal.signal(signal.SIGWINCH, lambda *_: self._set_winsize())
        signal.signal(signal.SIGTERM, lambda *_: self._terminate())
        last_heartbeat = 0.0
        try:
            while not self.child_exited:
                self._drain_pending()
                want = selectors.EVENT_READ
                if self.out_queue:
                    want |= selectors.EVENT_WRITE
                sel.modify(self.master_fd, want, "master")
                timeout = 0.05 if self.out_queue else 1.0
                for key, mask in sel.select(timeout=timeout):
                    tag = key.data
                    if tag == "master":
                        if mask & selectors.EVENT_WRITE:
                            self._drain_pending()
                        if mask & selectors.EVENT_READ:
                            try:
                                data = os.read(self.master_fd, 65536)
                            except BlockingIOError:
                                continue
                            except OSError:
                                data = b""
                            if not data:
                                self._reap()
                                break
                            self.last_output = time.monotonic()
                            self.transcript.append(data)
                            if self.interactive:
                                os.write(sys.stdout.fileno(), data)
                    elif tag == "stdin":
                        data = os.read(sys.stdin.fileno(), 65536)
                        if data:
                            self._enqueue(data)
                            self._drain_pending()
                    elif tag == "accept":
                        try:
                            conn, _ = self.listener.accept()
                        except OSError:
                            continue
                        self._serve_conn(conn)
                now = time.monotonic()
                if now - last_heartbeat > 15:
                    last_heartbeat = now
                    registry.update(
                        self.session_id,
                        last_activity_at=registry.utcnow_iso(),
                        status_hint=self.status(),
                    )
        finally:
            self._cleanup()
        return self.exit_code if self.exit_code is not None else 1

    def _serve_conn(self, conn: socket.socket) -> None:
        # Single-shot request/response; peers are local and fast.
        try:
            conn.settimeout(5.0)
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < MAX_MESSAGE_BYTES + 4096:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return
            try:
                req = json.loads(buf)
            except json.JSONDecodeError:
                conn.sendall(b'{"ok": false, "error": "bad_json"}\n')
                return
            resp = self.handle_request(req)
            conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _reap(self) -> None:
        try:
            _, code = os.waitpid(self.child_pid, 0)
            self.exit_code = os.waitstatus_to_exitcode(code)
        except ChildProcessError:
            self.exit_code = -1
        self.child_exited = True

    def _terminate(self) -> None:
        if not self.child_exited:
            try:
                os.kill(self.child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _cleanup(self) -> None:
        if self._saved_termios is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_termios)
        if not self.child_exited:
            self._reap()
        if self.transcript is not None:
            self.transcript.close()
        registry.mark_exited(self.session_id, self.generation, self.exit_code)
        audit("session_exit", session_id=self.session_id, generation=self.generation,
              source="launcher", detail=f"exit_code={self.exit_code}")
        try:
            self.listener.close()
            paths.socket_path(self.session_id).unlink()
        except OSError:
            pass


def run_session(
    session_id: str,
    cmd: list[str],
    cwd: str | None = None,
    paste_mode: str = "bracketed",
    interactive: bool = True,
) -> int:
    host = SessionHost(
        session_id,
        cmd,
        cwd or os.getcwd(),
        paste_mode=paste_mode,
        interactive=interactive,
    )
    return host.run()
