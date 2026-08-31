#!/bin/bash
# Print the current MCP endpoint to paste into the ChatGPT connector, and
# verify it actually answers before printing.
set -uo pipefail
cd "$(dirname "$0")/.."
HOME_DIR="${BRIDGE_HOME:-$HOME/.codex-session-bridge}"
URL_FILE="$HOME_DIR/current-url.txt"
[ -s "$URL_FILE" ] || { echo "no URL published yet - run scripts/bridge-up.sh" >&2; exit 1; }
URL="$(cat "$URL_FILE")"
CODE=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -X POST "$URL" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
if [ "$CODE" = "200" ]; then
  echo "$URL"
else
  echo "endpoint not answering (HTTP $CODE): $URL" >&2
  exit 1
fi
