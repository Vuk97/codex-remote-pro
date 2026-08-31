"""codex-bridge CLI.

Session supervision:
  daemon      start the MCP server (alias: serve)
  run         run a command under a bridge-owned PTY session
  adopt       register a running native codex session (read/write via codex queue)
  readopt     re-bind a native session whose codex process was replaced
  discover    show running codex processes and their thread ids (read-only)
  sessions    list registered sessions
  status      show one session
  send        send a message through the bridge (same path as MCP)
  read        read recent output
  interrupt   Ctrl-C a bridge-owned PTY session
  remove      drop a registry entry

Advice mailbox and Pro:
  advise      park a question for the supervising model
  answers     read or wait for answers
  respond     watch the mailbox and alert (opt-in --inject types the nudge)
  task-prompt print the app-free ChatGPT scheduled-task recipe
  pro-setup   wire ChatGPT Pro into Codex as a model provider

Operations:
  doctor      health-check the whole delivery path
  open        print exact ChatGPT settings URLs
  token       generate or show the bearer token
"""

from __future__ import annotations

import argparse
import json
import sys

from . import service


def _print(obj) -> int:
    json.dump(obj, sys.stdout, indent=2)
    print()
    return 0 if obj.get("ok", True) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="codex-remote")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", aliases=["serve"], help="start the MCP server")
    d.add_argument("--host", default=None)
    d.add_argument("--port", type=int, default=None)
    d.add_argument("--stop", action="store_true",
                   help="stop the daemon this BRIDGE_HOME started, by pid")

    r = sub.add_parser("run", help="run a command under a bridge-owned PTY session")
    r.add_argument("--session", required=True)
    r.add_argument("--cwd", default=None)
    r.add_argument("--paste-mode", choices=["bracketed", "plain"], default="bracketed")
    r.add_argument("--headless", action="store_true",
                   help="no terminal relay; for tests and background sessions")
    r.add_argument("cmdline", nargs=argparse.REMAINDER,
                   help="-- command and args to run")

    a = sub.add_parser("adopt", help="register a running native codex session")
    a.add_argument("--session", required=True, help="stable bridge session id")
    a.add_argument("--thread", required=True, help="codex thread UUID")
    a.add_argument("--pid", type=int, default=None,
                   help="codex process pid (auto-detected from the rollout file if omitted)")
    a.add_argument("--read-only", action="store_true",
                   help="register with READ capability only")

    sub.add_parser("discover", help="show running codex processes (read-only)")

    ra = sub.add_parser("readopt", help="re-bind native session(s) whose codex "
                        "process was replaced (reboot/restart); bumps generation")
    ra.add_argument("session_id", nargs="?", default=None,
                    help="session to re-bind; omit with --all")
    ra.add_argument("--all", action="store_true",
                    help="re-bind every native session with a dead pid")
    sub.add_parser("sessions", help="list registered sessions")

    st = sub.add_parser("status", help="show one session")
    st.add_argument("session_id")

    sd = sub.add_parser("send", help="send a message through the bridge")
    sd.add_argument("session_id")
    sd.add_argument("message", nargs="?",
                    help="message text; use --stdin for multiline input")
    sd.add_argument("--stdin", action="store_true", help="read message from stdin")
    sd.add_argument("--expected-generation", type=int, default=None)
    sd.add_argument("--idempotency-key", default=None)

    it = sub.add_parser("interrupt", help="Ctrl-C a bridge-owned PTY session")
    it.add_argument("session_id")
    it.add_argument("--expected-generation", type=int, default=None)

    rd = sub.add_parser("read", help="read recent output")
    rd.add_argument("session_id")
    rd.add_argument("--after-cursor", type=int, default=None)
    rd.add_argument("--limit", type=int, default=None)
    rd.add_argument("--raw", action="store_true")

    rm = sub.add_parser("remove", help="remove a session entry from the registry "
                        "(does not touch any process; refuses live PTY sessions)")
    rm.add_argument("session_id")

    tk = sub.add_parser("token", help="generate or show the bearer token")
    tk.add_argument("action", choices=["generate", "show"])
    tk.add_argument("--keychain", action="store_true",
                    help="store in the macOS Keychain instead of BRIDGE_HOME/token")

    av = sub.add_parser("advise", help="park a question in the advice mailbox "
                        "for the supervising model")
    av.add_argument("question", nargs="?", help="question text; omit with --stdin")
    av.add_argument("--stdin", action="store_true", help="read question from stdin")
    av.add_argument("--id", dest="request_id", help="explicit request id")
    av.add_argument("--wait", action="store_true",
                    help="block until answered, then print the answer")
    av.add_argument("--timeout", type=float, default=300.0,
                    help="seconds to wait with --wait (default 300, max 3600)")

    dr = sub.add_parser("doctor", help="check the whole delivery path: "
                        "token, daemon, auth, tunnel, sessions, mailbox")
    dr.add_argument("--json", action="store_true", dest="as_json",
                    help="machine-readable output")
    dr.add_argument("--port", type=int, default=8788)

    op = sub.add_parser("open", help="print the exact ChatGPT settings URL "
                        "for a task (connectors, developer-mode, "
                        "create-connector)")
    op.add_argument("what", choices=["connectors", "developer-mode",
                                     "create-connector"])

    rw = sub.add_parser("respond", help="watch the mailbox and alert when "
                        "advice requests are pending (macOS)")
    rw.add_argument("--watch", action="store_true", required=True,
                    help="run the watch loop")
    rw.add_argument("--interval", type=float, default=3.0)
    rw.add_argument("--cooldown", type=float, default=120.0,
                    help="seconds between alerts while requests stay pending")
    rw.add_argument("--inject", action="store_true",
                    help="also type the check-advice line into a ChatGPT "
                         "window. The destination cannot be proven, so this "
                         "is opt-in and refuses a non-empty composer. "
                         "Without it, or a scheduled task, or a human, "
                         "nothing answers and callers wait forever.")

    ps = sub.add_parser("pro-setup", help="wire ChatGPT Pro into Codex as a "
                        "model provider (writes ~/.codex/config.toml, with a "
                        "backup)")
    ps.add_argument("--port", type=int, default=8788)
    ps.add_argument("--dry-run", action="store_true",
                    help="print the config blocks without writing")

    tp = sub.add_parser("task-prompt", help="print the ChatGPT scheduled-task "
                        "recipe: answer the mailbox with the app closed")
    tp.add_argument("--every", type=int, default=10, metavar="MINUTES",
                    help="how often the task will run (default 10)")

    an = sub.add_parser("answers", help="read advice answers")
    an.add_argument("--id", dest="request_id", help="one request id")
    an.add_argument("--wait", action="store_true",
                    help="with --id: block until answered")
    an.add_argument("--timeout", type=float, default=300.0,
                    help="seconds to wait with --wait (default 300, max 3600)")

    args = p.parse_args(argv)

    if args.cmd in ("daemon", "serve"):
        from . import server

        if args.stop:
            return server.stop()
        server.run(host=args.host, port=args.port)
        return 0

    if args.cmd == "run":
        cmdline = args.cmdline
        if cmdline and cmdline[0] == "--":
            cmdline = cmdline[1:]
        if not cmdline:
            p.error("run requires a command after --, e.g. "
                    "codex-bridge run --session my-session -- codex")
        from .launcher import run_session

        return run_session(
            args.session,
            cmdline,
            cwd=args.cwd,
            paste_mode=args.paste_mode,
            interactive=not args.headless,
        )

    if args.cmd == "adopt":
        return _print(service.adopt_native(
            args.session, args.thread, pid=args.pid, read_only=args.read_only))

    if args.cmd == "readopt":
        if args.all:
            return _print(service.readopt_all())
        if not args.session_id:
            p.error("provide a session_id or --all")
        return _print(service.readopt(args.session_id))

    if args.cmd == "discover":
        from . import native

        return _print({"ok": True, "processes": native.discover()})

    if args.cmd == "sessions":
        return _print(service.list_sessions())

    if args.cmd == "status":
        return _print(service.get_session(args.session_id))

    if args.cmd == "send":
        if args.stdin:
            message = sys.stdin.read()
        elif args.message is not None:
            message = args.message
        else:
            p.error("provide a message argument or --stdin")
        return _print(service.send_message(
            args.session_id,
            message,
            expected_generation=args.expected_generation,
            idempotency_key=args.idempotency_key,
            source="cli",
        ))

    if args.cmd == "interrupt":
        return _print(service.interrupt(
            args.session_id, expected_generation=args.expected_generation, source="cli"))

    if args.cmd == "read":
        return _print(service.read_recent(
            args.session_id,
            after_cursor=args.after_cursor,
            limit=args.limit,
            plain=not args.raw,
        ))

    if args.cmd == "remove":
        return _print(service.remove_session(args.session_id))

    if args.cmd == "advise":
        from . import advice

        if args.stdin:
            question = sys.stdin.read()
        elif args.question is not None:
            question = args.question
        else:
            p.error("provide a question argument or --stdin")
        res = advice.create(question, request_id=args.request_id, source="cli")
        if not res.get("ok") or not args.wait:
            return _print(res)
        # Never block silently: an agent (or human) must be able to see that
        # this call is waiting, for how long, and how to collect the answer
        # later after interrupting.
        print(f"waiting up to {int(args.timeout)}s for an answer to "
              f"{res['id']}; interrupt and collect it later with: "
              f"codex-remote answers --id {res['id']}", file=sys.stderr, flush=True)
        waited = advice.wait(res["id"], timeout_seconds=args.timeout)
        if waited.get("ok"):
            print(waited["request"]["answer"])
            return 0
        return _print(waited)

    if args.cmd == "answers":
        from . import advice

        if args.request_id:
            if args.wait:
                print(f"waiting up to {int(args.timeout)}s for an answer to "
                      f"{args.request_id}", file=sys.stderr, flush=True)
                res = advice.wait(args.request_id, timeout_seconds=args.timeout)
                if res.get("ok"):
                    print(res["request"]["answer"])
                    return 0
                return _print(res)
            return _print(advice.get(args.request_id))
        return _print(advice.list_pending(include_stale=True))

    if args.cmd == "respond":
        from .nudge import watch

        return watch(interval=args.interval, cooldown=args.cooldown,
                     inject=args.inject)

    if args.cmd == "task-prompt":
        from .prosetup import print_task_prompt

        return print_task_prompt(interval_minutes=args.every)

    if args.cmd == "pro-setup":
        from .prosetup import run_setup

        return run_setup(port=args.port, dry_run=args.dry_run)

    if args.cmd == "doctor":
        from . import doctor

        report = doctor.run_checks(port=args.port)
        if args.as_json:
            return _print(report)
        icons = {"ok": "+", "warning": "!", "error": "x"}
        for c in report["checks"]:
            line = f"[{icons[c['status']]}] {c['id']}: {c['message']}"
            print(line)
            for k in ("detail", "url", "path"):
                if c.get(k):
                    print(f"      {k}: {c[k]}")
            for name, url in (c.get("links") or {}).items():
                print(f"      {name}: {url}")
        print(f"overall: {report['status']}")
        return 0 if report["ok"] else 1

    if args.cmd == "open":
        from .doctor import LINKS

        print(LINKS[args.what])
        return 0

    if args.cmd == "token":
        from .auth import generate_token, load_token

        if args.action == "generate":
            tok = generate_token(use_keychain=args.keychain)
            where = "macOS Keychain (codex-session-bridge)" if args.keychain else "BRIDGE_HOME/token (0600)"
            print(f"token stored in {where}")
            print(tok)
            return 0
        tok = load_token()
        if tok:
            print(tok)
            return 0
        print("no token configured; run: codex-bridge token generate", file=sys.stderr)
        return 1

    p.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
