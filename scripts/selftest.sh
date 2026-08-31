#!/bin/bash
# One-command end-to-end selftest: starts a fake interactive PTY session in a
# throwaway BRIDGE_HOME, sends "bridge-test" through the bridge, and verifies
# it arrived exactly once. Exits 0 on success. Touches nothing outside /tmp.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
export BRIDGE_HOME="$(mktemp -d /tmp/cbr-selftest-XXXXXX)"
trap 'kill "$LAUNCHER_PID" 2>/dev/null || true; rm -rf "$BRIDGE_HOME"' EXIT

"$PY" -m codex_bridge.cli run --session selftest --headless --paste-mode plain \
  -- "$PY" -u tests/fake_repl.py >/dev/null 2>&1 &
LAUNCHER_PID=$!

for _ in $(seq 1 100); do
  [ -S "$BRIDGE_HOME/sockets/selftest.sock" ] && break
  sleep 0.1
done
[ -S "$BRIDGE_HOME/sockets/selftest.sock" ] || { echo "FAIL: session did not start"; exit 1; }

"$PY" -m codex_bridge.cli send selftest "bridge-test" \
  --expected-generation 1 --idempotency-key selftest-key >/dev/null
# A retry with the same key must dedup, not double-send.
"$PY" -m codex_bridge.cli send selftest "bridge-test" \
  --expected-generation 1 --idempotency-key selftest-key >/dev/null

for _ in $(seq 1 100); do
  OUT="$("$PY" -m codex_bridge.cli read selftest --after-cursor 0 --limit 65536 | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["text"])')"
  case "$OUT" in *"echo:bridge-test"*) break;; esac
  sleep 0.1
done

COUNT=$(printf '%s' "$OUT" | grep -c "echo:bridge-test" || true)
if [ "$COUNT" = "1" ]; then
  echo "PASS: bridge-test delivered exactly once to the fake PTY session"
  exit 0
fi
echo "FAIL: expected exactly one delivery, saw $COUNT"
printf '%s\n' "$OUT" | tail -20
exit 1
