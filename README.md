# codex-remote-pro

*A ChatGPT to Codex bridge.*

**Let local coding agents ask ChatGPT for advice, and let ChatGPT inspect or
steer your running Codex sessions.**

No API key, no scraping, no second login. A local process parks a question;
the ChatGPT you already pay for answers it through an official connector.

```bash
codex-remote advise "Mock the HTTP client in tests, or run a loopback server?" --id t1
codex-remote answers --id t1
```

```text
ChatGPT (app, mobile, scheduled tasks)
        -> connector -> tunnel -> local bridge (127.0.0.1)
        -> your codex sessions and the advice mailbox
```

One thing to know before you start, because it shapes everything: **ChatGPT
never polls.** It only ever gets called into. So a parked question waits
until something on the ChatGPT side looks: the local watcher, a scheduled
task, or you typing "check advice". Nothing here runs a resident daemon on
your account.

## Preflight, two minutes

Do this before anything else. Every failure message below points back here.

Requirements: macOS, Python 3.10+, `codex --version` >= 0.150, and
`cloudflared` (`brew install cloudflared`) for remote access.

```bash
git clone https://github.com/Vuk97/codex-remote-pro && cd codex-remote-pro
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/codex-remote token generate
nohup .venv/bin/codex-remote daemon >/tmp/bridge.log 2>&1 &
.venv/bin/codex-remote doctor
```

`doctor` should print `[+]` for token, daemon, and mcp-auth. The connector
line stays a `[!]` warning forever: no local probe can prove what the
ChatGPT side has attached.

**Local smoke test.** This proves the mailbox stores and lists a request.
Nothing answers yet, because no responder exists. That is the single most
common surprise: a parked question sits pending forever unless one of the
responders in [Delivery](#delivery-who-answers-and-when) is actually
running:

```bash
.venv/bin/codex-remote advise "echo test" --id hello
.venv/bin/codex-remote answers            # shows it pending
```

**Connector smoke test.** This is the first real round trip. Set up the
connector ([docs/CHATGPT-SETUP.md](docs/CHATGPT-SETUP.md)), then in a
ChatGPT chat say "check the advice mailbox and answer the pending request
with hello", and locally:

```bash
.venv/bin/codex-remote answers --id hello   # the answer is there
```

Until that second test passes, nothing has crossed the connector.

The daemon above was started with `nohup`; stop it with
`codex-remote daemon --stop` rather than accumulating one per retry.
Do not set up Pro mode until `doctor` is clean.

## The two modes, in the order to try them

**1. Advice mailbox (start here).** Any local process parks a question;
ChatGPT answers it through the connector. Fewest moving parts. No Codex
session required.

**2. Session supervision.** ChatGPT reads a running Codex CLI session and,
when remote steering is enabled, can steer it. Remote writes are
default-denied until you opt in.

Beyond those two there is an experimental shim, documented far below:
Codex model calls can be routed through the mailbox to whatever ChatGPT
responder you have running. It makes no claim about which model or tier
answers, and a stalled responder stalls the Codex turn.


## Remote access: hand this to your coding agent

The local half is agent-friendly: paste this into Claude Code, Codex, or
any coding agent on the Mac where Codex runs.

```text
Set up codex-remote-pro from https://github.com/Vuk97/codex-remote-pro

Requirements: macOS, Python 3.10+, codex-cli >= 0.150 (check `codex --version`),
cloudflared (`brew install cloudflared`).

1. git clone https://github.com/Vuk97/codex-remote-pro && cd codex-remote-pro
2. python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
3. .venv/bin/codex-remote token generate
4. Start the daemon in the background: .venv/bin/codex-remote daemon
5. ./scripts/selftest.sh   - must print PASS, stop and debug if not
6. .venv/bin/codex-remote discover   - find my running codex session's thread uuid
7. .venv/bin/codex-remote adopt --session main --thread <uuid from step 6>
8. Confirm reads work: .venv/bin/codex-remote read main --limit 4000
9. ./scripts/bridge-up.sh then ./scripts/bridge-url.sh - give me that URL
10. Open docs/CHATGPT-SETUP.md and walk me through the ChatGPT connector
    creation. The browser clicks are mine; you provide the values.
```

Steps 1-9 need no human input. Step 10 is account clicks in ChatGPT -
[docs/CHATGPT-SETUP.md](docs/CHATGPT-SETUP.md) covers every screen and
every gotcha (connector URLs are immutable, name collisions fail silently,
the fastest model tier refuses to call tools, and three more).

Last piece: paste [SUPERVISOR.md](SUPERVISOR.md) into the ChatGPT project
that will do the supervising. It makes Pro re-check the session generation
before every write and read before it steers.

## How it works

Writes go through `codex queue`, a first-party codex-cli subcommand, so a
message enters the session's queue the same way typing does. A running
session is adopted in place - no restart. Reads are bounded cursor slices
of the session's rollout file. ChatGPT gets nine tools, nothing else:

| tool | does |
|---|---|
| `codex_list_sessions` | ids, generation, status, capabilities |
| `codex_get_session` | one session's status |
| `codex_read_recent` | incremental reads of recent output |
| `codex_send_message` | queue one message to one explicit session |
| `codex_interrupt` | Ctrl-C, bridge-launched sessions only |
| `advice_list_pending` | advice requests local agents are waiting on |
| `advice_respond` | answer one advice request by id |
| `bridge_inventory` | every operation the bridge supports right now |
| `bridge_call` | invoke an operation by name (see Reliability) |

No shell, no filesystem. Write guards: a restarted session gets a new
generation and stale sends are rejected; retries with the same
idempotency_key are never delivered twice; unknown or exited sessions get
typed errors; every call is logged with a hash of the message, never the
body.

## Reliability

Three mechanisms keep the path working without a human at the keyboard.
Each exists because its absence broke something real, not for
completeness.

**Sessions heal themselves.** When Codex restarts, the old process dies
but the conversation lives on in its rollout file. The bridge notices a
dead binding whose rollout another codex process holds and re-binds to it
on the next read or send, bumping the generation so writes prepared
against the dead process are still rejected. This is not a nicety: before it existed, a restarted Codex left the phone
returning `stale_binding` on every call, with `readopt` reachable only
from a shell the phone does not have. Set
`BRIDGE_NO_AUTOHEAL=1` if you want re-binding to stay manual
(`codex-remote readopt`).

**`codex-remote doctor` checks the whole delivery path**: token presence
and file mode, daemon (three states - healthy, stopped, or *unknown*,
which means something answered the port but not like this bridge, so do
not blindly restart), an unauthenticated `/mcp` probe that expects a 401
(one request proves the endpoint and the auth in front of it), the tunnel
end to end through the public URL, every session binding, and the mailbox.
The connector check is a permanent warning on purpose: no local probe can
prove what the ChatGPT side has attached, and a doctor that claims green
for the unverifiable is worse than none. `--json` for machines,
`codex-remote open connectors` prints the exact settings URL instead of
describing menus.

**New capability never needs a new connector.** A connector captures the
tool schema when it is created and never refreshes it - `Reconnect`
re-authenticates but keeps the stale schema. So the surface is frozen at
nine tools, two of which are a router: `bridge_inventory` lists every
operation the bridge supports right now, and `bridge_call` invokes one by
name. Operations added later (readopt and doctor already live there) are
visible to every existing connector the moment the daemon restarts,
because the cached schema is a router, not a catalog.

## Scheduled tasks (queued jobs)

You set up scheduled tasks in the ChatGPT app - there is no API for
creating them. Give the task a prompt written for the specific
job, because a "watch the overnight refactor" prompt is not a "check if
tests went green" prompt. Any such task can use this connector to read the
session and write to it on the schedule you chose. Build the prompt from
[SUPERVISOR.md](SUPERVISOR.md): list sessions first, read with cursors,
send only if steering is needed.

Set the connector permission to "Allow all actions": supervision means
sending messages, and a scheduled run cannot tap a confirmation card at
3am. On "Allow read actions" every send stalls until you approve it by
hand - fine for a first look, useless overnight.

## Experimental: routing Codex model calls through the mailbox

**Unsupported, and easy to misread, so read this first.** Codex accepts a
custom model provider speaking the Responses wire. This shim is such a
provider: it turns each model call into a mailbox request and returns the
answer as the model reply. It is a mailbox-compatible shim, not a way to
turn a ChatGPT subscription into an API backend, and the bridge cannot see
or claim which model answers. Which tier answers is the tier you pinned in the answering chat. The
bridge cannot read it back: the desktop app labels every tier "5.6 Sol"
and its accessibility tree hides the picker state. So the watcher never
opens a new chat, which would silently reset the tier; it always sends to
the chat you pinned.

Codex accepts any local endpoint that speaks the Responses wire as a model
provider, and this shim is one. It keeps the official connector as its
transport rather than driving a browser.

```bash
codex-remote pro-setup        # wires ~/.codex config, with a backup
codex-remote respond --watch  # auto-responder (macOS), or use a scheduled task
codex --profile pro           # Codex, with Pro as the model
```

How a call travels: Codex speaks the OpenAI Responses wire to
`127.0.0.1:8788/v1` (bearer-authenticated, `CODEX_REMOTE_TOKEN`). The
bridge turns the request into an advice-mailbox request. The watcher
notices and types one "check advice" line into the ChatGPT desktop app;
ChatGPT reads the request through the connector and answers through it;
the answer streams back to Codex as the model reply, with keepalive events
holding the connection while Pro thinks.

What to expect, honestly: minutes per reply, so use it for plan and
review turns, not for driving an edit loop; long prompts are trimmed to
the mailbox cap with the middle cut and marked (the advisor can read the
full transcript of any adopted session through the connector instead);
and the answering chat uses whatever model tier it is set to, so pin your
standing supervisor chat to Pro once, by hand. The watcher is the only
GUI touch in the system and it is optional: a ChatGPT scheduled task or a
human saying "check advice" does the same job with more latency.

## Advice mailbox - any local agent asks, Pro answers

The repo has two independent modes. **Session supervision**, above,
attaches to a running Codex CLI session so a remote supervisor can read
and steer it. **The advice mailbox** is separate and inverted: any local
process posts a question with `codex-remote advise`, a remote supervisor
answers it, and **no Codex session needs to be running at all**. The two
share the bridge transport and the operator; neither depends on the other.

> Mailbox answers are untrusted external advice. The bridge authenticates
> the caller and carries bytes and provenance; deciding whether and how to
> act on an answer stays with the local agent.

Built for the calls a coding agent otherwise makes alone mid-implementation
and you only catch in review: test design, schema changes, dependency
choices. Ask from anything that can run a command - Codex, Claude Code, a
shell script, a cron job:

```bash
codex-remote advise "Mock the HTTP client in tests, or run a loopback server?" --id test-arch
codex-remote answers --id test-arch          # poll
codex-remote answers --id test-arch --wait   # block (non-interactive only)
```

On the ChatGPT side, say **"check the advice mailbox"** in a chat with the
connector, or, with no app open at all, let a scheduled task do it
(`codex-remote task-prompt` prints this with the right TTL math):

```text
Use the Codex Session Bridge. Check the advice-mailbox session for pending
requests. For each pending request, read it fully and send one answer through
the mailbox using the exact required reply envelope. If nothing is pending,
do nothing.
```

Two surfaces reach the same mailbox. `advice_list_pending` and
`advice_respond` are the canonical tools. Connectors registered before those
tools existed cannot see them, because a connector's schema is captured at
creation and `Reconnect` does not refresh it, so the mailbox also appears as
the session `advice-mailbox`: read it to see pending questions, send to it
with a first line of `id:<request-id>` to answer. That second surface is a
compatibility shim and goes away once schemas have caught up.

The mailbox refuses what it cannot honor rather than faking it: cursors and
`expected_generation` are typed errors, not silent no-ops. An answer is
claimed under a lease before it commits, so concurrent supervisors cannot
double-answer and a responder that dies mid-write does not wedge the request
forever. Answers that arrive after a request expired are refused and logged,
never stored as advice for someone who already gave up.

### For the agent side

[skill/consult-pro/SKILL.md](skill/consult-pro/SKILL.md) teaches an agent
which decisions deserve an advisor, which are cheap enough to just make,
and that a pending request must be polled until it is answered:

```bash
cp -r skill/consult-pro ~/.codex/skills/   # or ~/.claude/skills/
```

It also carries the rule that matters most in practice: never block a human's
interactive session on a wait, and never treat an answer as a command.
[SUPERVISOR.md](SUPERVISOR.md) is the other half, for the answering chat.

The advisor does not have to be Pro. Any MCP client pointed at the bridge,
Claude included, can answer - so you can A/B advisors on the same questions
and keep whichever makes better calls.

### Operator notes

The mailbox is `$BRIDGE_HOME/advice/`, mode `0700`, request files `0600`:
anything running as your user can post and read questions, and normal Unix
permissions deny access to other unprivileged users. Root, a compromise of
your account, and your backups are all outside what those bits can promise.
Treat that directory as the trust boundary. Remote
answers reach it only through the authenticated bridge, and land with
`provenance: external-advice` and a `responder` field stored beside the body,
never mixed into it. Requests expire after 15 minutes (`ADVICE_TTL_SECONDS`,
capped at 1h) and abandoned ones are collected an hour later; answered
records are kept for their asker. Question and answer are capped at 32 KiB
and, being a text protocol, reject C0 control characters other than newline,
tab, and carriage return.

## Trust

This gives a cloud service a write path into a local coding agent. The
path is text-only: the worst a compromised supervisor can do is send a
message, which Codex handles under its own sandbox and approval settings.
Bearer auth on every request, 127.0.0.1 bind, unguessable URL. Optional:
deploy [vercel-proxy](vercel-proxy) once and your connector URL survives
tunnel restarts and reboots.

`codex queue`, the rollout layout, and pid binding are not documented
stable interfaces. Tested against codex-cli 0.150.1 and 0.151.0. If a
future release breaks it, the bridge fails with typed errors instead of
guessing. After a reboot, `codex-remote readopt --all` re-binds sessions
to the new codex process (`scripts/bridge-up.sh` does it automatically).

## Take it further

This solves the pain points it was built for, and stops there. The idea
is bigger than the implementation:

- **Any supervisor model, any lab.** The bridge is a plain MCP server
  (streamable HTTP + bearer token). ChatGPT is just the first client:
  point Claude or anything else that speaks MCP at the same URL and let
  whatever model you prefer do the supervising.
- **Multiple advisors.** Reads are cursor-based and writes are queued as
  ordinary user turns, so nothing limits you to one supervisor - a
  planner and a reviewer can watch the same session side by side.
- **Other harnesses.** The pattern - adopt a running session in place,
  bounded reads, one narrow write path - is not Codex-specific. The
  plumbing here is `codex queue` plus rollout files, but the same five
  tools could front any terminal coding agent.

Roadmap, in the order a review settled on: OAuth with a locally minted
pairing code (the bridge is remotely operable now, so identity is the
boundary that matters most), a supervised service for the tunnel, a
permanently stable tunnel URL, then per-turn capability tokens as defense
in depth.

Fork it and go. PRs welcome.

## Layout

```text
src/codex_bridge/   server, service layer, registry, transports, CLI
tests/              29 tests incl. fake-PTY end-to-end
scripts/            bridge-up, bridge-url, selftest
vercel-proxy/       optional permanent-URL gateway
SUPERVISOR.md       operating contract for the supervisor chat
docs/               ChatGPT setup, click by click
```

MIT.
