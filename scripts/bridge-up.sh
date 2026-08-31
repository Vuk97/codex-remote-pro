#!/bin/bash
# Bring everything up: daemon + tunnel, heal rebooted sessions, publish the
# MCP URL to ~/.codex-session-bridge/current-url.txt.
#
# Idempotent - safe to run at login and on a timer; it only starts what is
# not already running. Optional config in ~/.codex-session-bridge/tunnel.conf:
#   BRIDGE_GATEWAY_URL=https://<your-project>.vercel.app
# When set (and vercel-proxy/ is deployed), the published URL is the
# permanent gateway endpoint and this script re-aims the gateway whenever the
# local tunnel origin changes. Without it, the published URL is the raw
# tunnel URL and changes on every tunnel restart.
set -uo pipefail
cd "$(dirname "$0")/.."
HOME_DIR="${BRIDGE_HOME:-$HOME/.codex-session-bridge}"
mkdir -p "$HOME_DIR"
LOG="$HOME_DIR/bridge-up.log"
URL_FILE="$HOME_DIR/current-url.txt"
CONF="$HOME_DIR/tunnel.conf"
[ -f "$CONF" ] && . "$CONF"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" >> "$LOG"; }

# --- daemon ---------------------------------------------------------------
if ! pgrep -f "codex-remote daemon" >/dev/null 2>&1; then
  log "starting daemon"
  BRIDGE_ALLOW_URL_TOKEN=1 nohup .venv/bin/codex-remote daemon >> "$HOME_DIR/daemon.log" 2>&1 &
  sleep 3
fi

# Heal native sessions whose codex process was replaced (reboot/restart):
# same session_id + thread, new pid, generation + 1. No-op when pids live.
.venv/bin/codex-remote readopt --all >> "$LOG" 2>&1 || true

TOKEN="$(.venv/bin/codex-remote token show 2>/dev/null)"
[ -z "$TOKEN" ] && { log "FATAL: no token; run 'codex-remote token generate'"; exit 1; }

# --- gateway sync (optional permanent URL) --------------------------------
sync_gateway() {  # $1 = current tunnel origin
  local origin="$1" state="$HOME_DIR/gateway-target.txt"
  [ -n "${BRIDGE_GATEWAY_URL:-}" ] || return 0
  [ -d vercel-proxy ] && command -v vercel >/dev/null 2>&1 || return 0
  [ -f "$state" ] && [ "$(cat "$state")" = "$origin" ] && return 0
  log "re-aiming gateway at $origin"
  ( cd vercel-proxy \
    && vercel env rm BRIDGE_TARGET production --yes >/dev/null 2>&1 \
    ;  printf '%s' "$origin" | vercel env add BRIDGE_TARGET production >/dev/null 2>&1 \
    && vercel deploy --prod --yes >/dev/null 2>&1 ) \
    && { printf '%s' "$origin" > "$state"; log "gateway re-aimed OK"; } \
    || log "WARNING: gateway re-aim failed; connector URL will be stale"
}

publish() {  # $1 = tunnel origin
  sync_gateway "$1"
  local secret_file="$HOME_DIR/proxy-secret"
  if [ -n "${BRIDGE_GATEWAY_URL:-}" ] && [ -s "$secret_file" ]; then
    printf '%s/p/%s/mcp\n' "$BRIDGE_GATEWAY_URL" "$(cat "$secret_file")" > "$URL_FILE"
  else
    printf '%s/t/%s/mcp\n' "$1" "$TOKEN" > "$URL_FILE"
  fi
  chmod 600 "$URL_FILE"
  log "MCP URL published (tunnel origin $1)"
}

# --- tunnel ---------------------------------------------------------------
if pgrep -f "cloudflared tunnel --url" >/dev/null 2>&1 && [ -s "$URL_FILE" ]; then
  log "tunnel already running"
  exit 0
fi
: > "$HOME_DIR/tunnel.log"
log "starting cloudflared quick tunnel"
nohup cloudflared tunnel --url http://127.0.0.1:8788 >> "$HOME_DIR/tunnel.log" 2>&1 &
for _ in $(seq 1 30); do
  ORIGIN=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$HOME_DIR/tunnel.log" | head -1)
  [ -n "$ORIGIN" ] && break
  sleep 1
done
[ -z "${ORIGIN:-}" ] && { log "FATAL: tunnel produced no URL"; exit 1; }
publish "$ORIGIN"
