"""Bearer-token authentication.

Token resolution order: BRIDGE_TOKEN env var, then BRIDGE_HOME/token file
(created 0600 by `codex-bridge token generate`), then the macOS Keychain
item `codex-session-bridge`. The server refuses to start without a token
of at least 24 characters; there is no unauthenticated mode.

Optionally (BRIDGE_ALLOW_URL_TOKEN=1) the token may ride in the URL path
as /t/<token>/mcp for MCP clients that cannot set headers. Off by default;
prefer header injection at the tunnel edge.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess

from . import paths

MIN_TOKEN_LEN = 24
KEYCHAIN_SERVICE = "codex-session-bridge"


def token_file() -> str:
    return str(paths.bridge_home() / "token")


def load_token() -> str | None:
    tok = os.environ.get("BRIDGE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(token_file()) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def generate_token(use_keychain: bool = False) -> str:
    tok = secrets.token_urlsafe(32)
    if use_keychain:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
             "-a", os.environ.get("USER", "bridge"), "-w", tok],
            check=True, capture_output=True, timeout=10,
        )
    else:
        paths.ensure_dirs()
        fd = os.open(token_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok + "\n")
    return tok


class BearerAuthMiddleware:
    """Pure ASGI middleware; rejects anything without the bearer token."""

    def __init__(self, app, token: str, allow_url_token: bool = False) -> None:
        if not token or len(token) < MIN_TOKEN_LEN:
            raise RuntimeError(
                f"refusing to start: bearer token missing or shorter than "
                f"{MIN_TOKEN_LEN} chars; run `codex-bridge token generate`"
            )
        self.app = app
        self.token = token
        self.allow_url_token = allow_url_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.allow_url_token and scope["path"].startswith("/t/"):
            parts = scope["path"].split("/", 3)
            if len(parts) >= 3 and hmac.compare_digest(parts[2], self.token):
                scope = dict(scope)
                scope["path"] = "/" + (parts[3] if len(parts) > 3 else "")
                return await self.app(scope, receive, send)
            return await self._deny(send)
        auth = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value
        expected = f"Bearer {self.token}".encode()
        if auth and hmac.compare_digest(auth, expected):
            return await self.app(scope, receive, send)
        return await self._deny(send)

    @staticmethod
    async def _deny(send):
        body = json.dumps({"error": "unauthorized"}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
