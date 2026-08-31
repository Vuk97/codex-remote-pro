"""Health checks for the whole delivery path, honest about what they cannot see.

Modeled on the taxonomy that works in the field: every check returns ok,
warning, or error; the daemon check is three-state (healthy, stopped,
unknown) so "my probe failed" is never reported as "it is down"; an
unauthenticated MCP probe expects a 401, which proves both the endpoint and
the auth layer in one request; and the connector check is a permanent
warning, because no local probe can prove what the ChatGPT side has
attached. Deep links beat click-path documentation, so checks print them.
"""

from __future__ import annotations

import json
import stat
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import advice, native, registry
from .auth import load_token
from .paths import bridge_home

DEFAULT_PORT = 8788

# The exact pages an operator needs, instead of a description of menus.
LINKS = {
    "connectors": "https://chatgpt.com/#settings/Connectors",
    "developer-mode": "https://chatgpt.com/#settings/Security",
    "create-connector": (
        "https://chatgpt.com/plugins#settings/Connectors?create-connector=true"
    ),
}


def _warn_after(live: dict[str, Any]) -> int:
    """When waiting stops being normal. Half the TTL by default; a
    long-interval scheduled task sets ADVICE_WARN_AFTER_SECONDS so expected
    gaps do not read as failures."""
    import os

    raw = os.environ.get("ADVICE_WARN_AFTER_SECONDS")
    if raw:
        try:
            return max(60, int(raw))
        except ValueError:
            pass
    return int(live["ttl_seconds"]) // 2


def _check(cid: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"id": cid, "status": status, "message": message, **extra}


def _get(url: str, token: str | None = None, timeout: float = 8.0,
         method: str = "GET", body: bytes | None = None,
         ctype: str | None = None) -> tuple[int | None, str]:
    req = urllib.request.Request(url, data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if ctype:
        req.add_header("Content-Type", ctype)
        req.add_header("Accept", "application/json, text/event-stream")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2048).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, str(e)


def _daemon_state(port: int, token: str | None) -> tuple[str, str]:
    """healthy | stopped | unknown, with a reason.

    stopped means the port answered nothing at all. unknown means something
    answered but not the way this bridge does, which is exactly when
    restarting blindly makes things worse.
    """
    code, body = _get(f"http://127.0.0.1:{port}/healthz", token=token,
                      timeout=3.0)
    if code == 200:
        try:
            if json.loads(body).get("ok") is True:
                return "healthy", body.strip()
        except ValueError:
            pass
        return "unknown", "healthz answered 200 with an unexpected body"
    if code is not None:
        return "unknown", f"healthz answered HTTP {code}"
    if "refused" in body.lower():
        return "stopped", "nothing is listening"
    return "unknown", f"probe failed: {body}"


def _redact(report: dict[str, Any]) -> dict[str, Any]:
    """Strip recon value for remote callers: no paths, pids, or full URLs.

    An authenticated single-user bridge could arguably return everything,
    but a leaked bearer token should not also hand over a map of the
    machine. States and verdicts survive; identifiers do not.
    """
    out = {"ok": report["ok"], "status": report["status"], "checks": []}
    for c in report["checks"]:
        cid = c["id"]
        if cid.startswith("session:"):
            cid = "session:" + cid.split(":", 1)[1][:8]
        kept = {"id": cid, "status": c["status"], "message": c["message"]}
        if "links" in c:
            kept["links"] = c["links"]
        out["checks"].append(kept)
    return out


def run_checks(port: int = DEFAULT_PORT, redact: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    token = load_token()

    # 1. Bearer token exists and is private.
    token_path = bridge_home() / "token"
    if not token:
        checks.append(_check("token", "error",
                             "no bearer token; run: codex-remote token generate"))
    elif token_path.exists():
        mode = stat.S_IMODE(token_path.stat().st_mode)
        if mode & 0o077:
            checks.append(_check("token", "error",
                                 f"token file is mode {oct(mode)}; chmod 600 it",
                                 path=str(token_path)))
        else:
            checks.append(_check("token", "ok", "token present, mode 0600"))
    else:
        checks.append(_check("token", "ok", "token present (keychain)"))

    # 2. Daemon, three states.
    state, reason = _daemon_state(port, token)
    checks.append(_check(
        "daemon",
        {"healthy": "ok", "stopped": "error", "unknown": "warning"}[state],
        {"healthy": f"daemon healthy on 127.0.0.1:{port}",
         "stopped": f"daemon is not running on 127.0.0.1:{port}",
         "unknown": "something answered the daemon port but not like this "
                    "bridge; do NOT blindly restart"}[state],
        detail=reason))

    # 3. MCP endpoint: an unauthenticated POST must get 401. That single
    # request proves the MCP route exists AND that auth fronts it.
    if state == "healthy":
        code, _ = _get(f"http://127.0.0.1:{port}/mcp", method="POST",
                       body=b"{}", ctype="application/json", timeout=3.0)
        if code == 401:
            checks.append(_check("mcp-auth", "ok",
                                 "unauthenticated /mcp correctly answers 401"))
        else:
            checks.append(_check("mcp-auth", "error",
                                 f"unauthenticated /mcp answered {code}; "
                                 "auth is not fronting the endpoint"))

    # 4. Tunnel process and public URL.
    try:
        cf = subprocess.run(["pgrep", "-f", "cloudflared tunnel"],
                            capture_output=True, text=True, timeout=5)
        tunnel_up = bool(cf.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        tunnel_up = False
    url_file = bridge_home() / "current-url.txt"
    public_url = url_file.read_text().strip() if url_file.exists() else None
    if not tunnel_up:
        checks.append(_check("tunnel", "error", "cloudflared is not running"))
    elif not public_url:
        checks.append(_check("tunnel", "warning",
                             "cloudflared runs but no public URL is recorded"))
    else:
        code, body = _get(public_url.replace("/mcp", "/healthz"), token=token)
        if code == 200:
            checks.append(_check("tunnel", "ok",
                                 "public URL reaches this bridge", url=public_url))
        elif code == 401:
            checks.append(_check("tunnel", "warning",
                                 "public URL reaches a bridge but rejects this "
                                 "token; is it yours?", url=public_url))
        else:
            checks.append(_check("tunnel", "error",
                                 f"public URL probe failed ({code or body})",
                                 url=public_url))

    # 5. Sessions: report healed and stale bindings.
    for sid, entry in registry.load_all().items():
        if entry.get("transport") != "native":
            continue
        pid = entry.get("pid")
        if registry.pid_alive(pid):
            checks.append(_check(f"session:{sid}", "ok",
                                 f"bound to live pid {pid}"))
        elif native.pids_holding(Path(entry["rollout_path"])):
            # a holder exists: autoheal re-binds on the next touch
            checks.append(_check(f"session:{sid}", "warning",
                                 "process replaced; will auto-heal on next use"))
        else:
            checks.append(_check(f"session:{sid}", "warning",
                                 "no process holds this rollout; session looks "
                                 "exited"))

    # 6. Mailbox permissions, backlog, and liveness. Correct is not enough:
    # a synchronous caller strands if nothing ever drains the mailbox.
    adv = advice.advice_dir()
    if adv.is_dir():
        mode = stat.S_IMODE(adv.stat().st_mode)
        live = advice.liveness()
        if mode != 0o700:
            checks.append(_check("mailbox", "error",
                                 f"advice dir is mode {oct(mode)}, want 0700"))
        elif (live["pending"]
                and live["oldest_pending_seconds"] > _warn_after(live)):
            since = live["seconds_since_drain"]
            drain = (f"last drain {since // 60}m ago" if since is not None
                     else "no drain ever recorded")
            checks.append(_check(
                "mailbox", "warning",
                f"{live['pending']} pending, oldest "
                f"{live['oldest_pending_seconds'] // 60}m, {drain}; is the "
                "responder (scheduled task / watcher / human) alive?"))
        else:
            since = live["seconds_since_drain"]
            drain = (f"last drain {since // 60}m ago" if since is not None
                     else "no drain recorded yet")
            checks.append(_check("mailbox", "ok",
                                 f"0700, {live['pending']} pending, {drain}"))
    else:
        checks.append(_check("mailbox", "ok", "no advice dir yet (created on "
                             "first advise)"))

    # 7. Connector: honestly unknowable from here.
    checks.append(_check(
        "connector", "warning",
        "no local probe can prove the ChatGPT connector is attached to this "
        "bridge; verify in the app",
        links=LINKS))

    if redact:
        pass  # redaction applied on the assembled report below
    worst = "ok"
    for c in checks:
        if c["status"] == "error":
            worst = "error"
            break
        if c["status"] == "warning":
            worst = "warning"
    report = {"ok": worst != "error", "status": worst, "checks": checks}
    return _redact(report) if redact else report
