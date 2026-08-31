"""Auto-responder: poke the standing ChatGPT chat when the mailbox fills.

The one thing nothing local can do is make ChatGPT look at the mailbox: the
app only calls in when a human types or a scheduled task fires. This watcher
closes that gap on macOS by typing the check-advice line into the ChatGPT
desktop app's composer through cua-driver, which drives the app in the
background without stealing focus. One send per batch of pending requests,
with a cooldown so a slow answer is not nudged twice.

Typing into a window is the one action here whose destination cannot be
proven: the app exposes no chat identity through accessibility, so the
watcher can see that a composer exists but not which conversation owns it.
A review called that out as the weakest component in the system, and the
default follows the conclusion: **notify, do not type**. Injection is an
explicit opt-in, and even then it refuses a composer that already holds
text so it can never clobber something a person is writing.

Without injection, a ChatGPT scheduled task or a human saying "check
advice" does the same job with more latency and a destination you chose.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time

from . import advice

CHATGPT_BUNDLE = "com.openai.chat"
# Phrasing matters more than it should. Mechanical tool instructions
# ("call codex_read_recent with session_id=...") trip ChatGPT's
# client-side safety checks and the read is refused with no error text.
# The same request in ordinary language goes through. Keep this plain.
NUDGE_TEXT = "Check the advice mailbox and answer whatever is pending there."


def _cua(*args: str, timeout: float = 30.0) -> str:
    try:
        r = subprocess.run(["cua-driver", *args], capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _cua_available() -> bool:
    try:
        subprocess.run(["cua-driver", "status"], capture_output=True,
                       timeout=10)
        return True
    except OSError:
        return False


def _ensure_daemon() -> None:
    out = _cua("status")
    if "running" not in out:
        try:
            subprocess.run(["open", "-n", "-g", "-a", "CuaDriver", "--args",
                            "serve"], capture_output=True, timeout=10)
        except OSError:
            return
        for _ in range(15):
            if "running" in _cua("status"):
                return
            time.sleep(0.5)


def _composer_text(pid: int, wid: int, index: int) -> str:
    """Read the composer's current value, to tell our draft from a human's."""
    out = _cua("get_window_state", json.dumps({"pid": pid, "window_id": wid}))
    try:
        tree = json.loads(out).get("tree_markdown", "")
    except ValueError:
        return ""
    m = re.search(rf"\[{index}\] AXTextArea[^\n]*= \"([^\"]*)\"", tree)
    return m.group(1) if m else ""


def _composer_busy(tree: str) -> bool:
    """An enabled Send button means the composer already holds text."""
    for line in tree.splitlines():
        if re.search(r"\[\d+\] AXButton \(Send\)", line) and "DISABLED" not in line:
            return True
    return False


def notify(pending: int) -> tuple[bool, str]:
    """Post a macOS notification. Destination-safe: it types nowhere."""
    body = (f"{pending} advice request(s) waiting. Say 'check advice' in the "
            "supervisor chat.")
    script = (f'display notification "{body}" with title "codex-remote" '
              'sound name "Ping"')
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True,
                       timeout=10)
        return True, "notified"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"notification failed: {e}"


def nudge_once() -> tuple[bool, str]:
    """Type one check-advice line into a ChatGPT window that has a composer.

    Opt-in only. The destination is unproven, so this refuses to type when
    the composer is not empty.
    """
    _ensure_daemon()
    app = _cua("launch_app", json.dumps({"bundle_id": CHATGPT_BUNDLE}))
    try:
        pid = json.loads(app)["pid"]
    except (ValueError, KeyError):
        return False, "ChatGPT app is not reachable"
    wins = _cua("list_windows", json.dumps({"pid": pid}))
    try:
        candidates = [w for w in json.loads(wins)["windows"]
                      if w.get("is_on_screen") and w["bounds"]["width"] > 400]
    except (ValueError, KeyError):
        candidates = []
    if not candidates:
        return False, "no on-screen ChatGPT window; open the supervisor chat"

    # More than one window can be open (settings, companion chats). The
    # right one is whichever actually has a composer; probe until found.
    wid = None
    tree = ""
    for w in candidates:
        state = _cua("get_window_state",
                     json.dumps({"pid": pid, "window_id": w["window_id"]}))
        try:
            t = json.loads(state).get("tree_markdown", "")
        except ValueError:
            continue
        if re.search(r"\[(\d+)\] AXTextArea actions", t):
            wid, tree = w["window_id"], t
            break
    if wid is None:
        return False, "no ChatGPT window with a composer; open the chat"

    # Stay in whatever chat is open. Opening a new chat would be the easy
    # way past a busy composer, but a new chat resets the model tier, and
    # the tier is the whole reason the operator pinned this chat by hand.
    field = re.search(r"\[(\d+)\] AXTextArea actions", tree)
    if not field:
        return False, "no composer in this chat"
    if _composer_busy(tree):
        current = _composer_text(pid, wid, int(field.group(1)))
        if current.strip() != NUDGE_TEXT.strip():
            return False, ("the composer holds something you are writing; "
                           "leaving it alone and retrying later")
        # Our own leftover from a send that never went through: safe to reuse.
    _cua("set_value", json.dumps({
        "pid": pid, "window_id": wid,
        "element_index": int(field.group(1)), "value": NUDGE_TEXT}))
    time.sleep(1.0)
    state = _cua("get_window_state",
                 json.dumps({"pid": pid, "window_id": wid}))
    try:
        tree = json.loads(state).get("tree_markdown", "")
    except ValueError:
        return False, "lost the window while typing"
    send = None
    for line in tree.splitlines():
        m = re.search(r"\[(\d+)\] AXButton \(Send\)", line)
        if m and "DISABLED" not in line:
            send = int(m.group(1))
            break
    if send is None:
        return False, "Send button never enabled"
    _cua("click", json.dumps({"pid": pid, "window_id": wid,
                              "element_index": send}))
    return True, "nudged"


def watch(interval: float = 3.0, cooldown: float = 120.0,
          inject: bool = False) -> int:
    """Poll the mailbox and alert whenever unanswered requests wait.

    Notifies by default. inject=True additionally types the check-advice
    line into a ChatGPT window, which is faster and less safe: see the
    module docstring.
    """
    if sys.platform != "darwin":
        print("the watcher is macOS-only; use a ChatGPT scheduled task "
              "instead", file=sys.stderr)
        return 1
    if inject and not _cua_available():
        print("cua-driver not found, so --inject is unavailable; falling "
              "back to notifications", file=sys.stderr)
        inject = False
    mode = "notify + inject" if inject else "notify only"
    print(f"watching {advice.advice_dir()} ({mode}, interval "
          f"{interval:.0f}s, cooldown {cooldown:.0f}s); Ctrl-C to stop",
          flush=True)
    last_alert = 0.0
    try:
        while True:
            pending = advice.list_pending()["pending"]
            if pending and time.time() - last_alert >= cooldown:
                ok, why = notify(len(pending))
                if inject:
                    injected, inj_why = nudge_once()
                    why = f"{why}; {'typed' if injected else inj_why}"
                    ok = ok or injected
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] {len(pending)} pending -> {why}", flush=True)
                if ok:
                    last_alert = time.time()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
