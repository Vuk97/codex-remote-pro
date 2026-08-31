# ChatGPT setup, click by click

Every step below comes from wiring this up for real, including the
failure modes. UI positions current as of ChatGPT web, August 2026.

## Prerequisites

- ChatGPT Pro (or any plan with Developer mode connectors)
- the bridge daemon running locally (`.venv/bin/codex-remote daemon` or
  `./scripts/bridge-up.sh`, which also starts the tunnel)
- a public HTTPS endpoint for it (Path A or B below)

## 1. Enable Developer mode

chatgpt.com -> Settings -> **Security** -> "Developer mode" toggle ON.
It is labeled "ELEVATED RISK"; that is the toggle for unverified
connectors, which is exactly what a self-hosted one is.

## 2. Get your endpoint URL

**Path A - quick tunnel (fastest, URL rotates on restart):**

```bash
./scripts/bridge-up.sh && ./scripts/bridge-url.sh
```

`bridge-url.sh` prints `https://<random>.trycloudflare.com/t/<token>/mcp`
only after verifying the endpoint answers an MCP tools/list with HTTP 200.
Caveat: every tunnel restart mints a new URL, and a ChatGPT connector URL is
immutable (see gotchas), so you will be deleting and recreating the
connector each time. Fine for trying it out; annoying for daily use.

**Path B - permanent URL via the Vercel gateway (recommended):**

```bash
cd vercel-proxy
vercel deploy --prod --yes --name codex-remote-gateway
# set three env vars: BRIDGE_TARGET (current tunnel origin),
# BRIDGE_TOKEN (your bearer), PROXY_SECRET (openssl rand -hex 24)
printf '%s' "<value>" | vercel env add <NAME> production
vercel deploy --prod --yes
```

Store the proxy secret at `~/.codex-session-bridge/proxy-secret` (0600).
From then on `./scripts/bridge-up.sh` re-aims BRIDGE_TARGET automatically
whenever the local tunnel URL changes, and your connector URL is permanently:

```text
https://<project>.vercel.app/p/<PROXY_SECRET>/mcp
```

The real bearer token never appears in the public URL; the gateway injects it
server-side from an encrypted env var. Wrong secret gets 401. The gateway
only forwards to the one configured origin - it is not an open proxy.

## 3. Create the connector

chatgpt.com/plugins -> "+" (Create app) -> New Plugin:

- Name: `Codex Session Bridge` (any name; see gotcha 2)
- Connection: **Server URL**, paste your endpoint URL
- Authentication: **No Auth** (auth rides in the URL path / gateway)
- tick "I understand and want to continue"
- Create -> Connect

On success the detail page lists all five `codex_*` actions.

## 4. Set permissions

Connector detail -> Permissions -> **Allow all actions**. Supervision is a
write workflow: steering happens through `codex_send_message`, and a
scheduled run cannot tap a confirmation card while you sleep. "Allow read
actions" demotes the supervisor to a viewer - reads run, every send waits
for a manual tap. Acceptable for a cautious first session; switch to all
actions for real use.

## 5. Use it

- New chat -> "+" -> pick Codex Session Bridge -> ask it to list sessions.
- Paste [SUPERVISOR.md](../SUPERVISOR.md) into the project instructions of a
  dedicated supervisor conversation.
- Mobile: nothing extra - the connector is account-level and shows up in the
  ChatGPT phone app's plugin picker too.
- Scheduled: ChatGPT's scheduled prompts can use the connector for
  overnight check-ins. Steering from a scheduled run needs the "Allow all
  actions" permission from step 4. There is no one-size prompt - write one
  per job, starting from the supervisor contract (list first, read with
  cursors, steer only if needed).

## Gotchas (each one cost real time)

1. **The connector URL is immutable.** The plugin menu offers edit name /
   edit description / disconnect / delete - no URL edit. A changed endpoint
   means delete + recreate. This is why Path B exists.
2. **Name collisions fail silently.** Creating a connector while another
   with the same name exists does nothing: the Create button appears to work
   and no error is shown. Delete the old one first.
3. **Delete + recreate breaks existing conversations.** The recreated
   connector has a new app id; conversations bound to the old id show the
   schema but every invocation is "blocked as disabled", permanently. Start
   a fresh conversation after recreating (re-attaching via the picker does
   not heal an already-poisoned one).
4. **The fastest model setting does not call tools.** On the effort slider,
   "Instant" refuses connector calls with "tool unavailable". Use a thinking
   tier for supervisor chats.
5. **localhost URLs cannot work.** Connectors execute from OpenAI's servers,
   not from your machine; `http://127.0.0.1:...` never validates. A tunnel
   or gateway is mandatory.
6. **First tool call asks for permission per conversation.** The
   Allow/Deny card can render below the fold - scroll down if a call seems
   stuck.
