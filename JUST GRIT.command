#!/bin/zsh
# ─────────────────────────────────────────────────────────────
#  JUST GRIT — double-click this. That's it.
#
#  Starts the app, publishes it at justgrit.thewatsonfactor.dev
#  (if the tunnel is set up), and opens it in your browser.
#
#  Leave this window open while you work. Closing it stops the app.
# ─────────────────────────────────────────────────────────────
cd "$(dirname "$0")" || exit 1

APP_DIR="just_grit"
PORT=8080
TUNNEL_CONF="$HOME/.cloudflared/justgrit.yml"

cleanup() {
  echo ""
  echo "  Shutting down…"
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null
  [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null
  exit 0
}
trap cleanup INT TERM

echo ""
echo "  ┌────────────────────────────────────┐"
echo "  │   JUST GRIT                        │"
echo "  └────────────────────────────────────┘"
echo ""

# ── start the app ───────────────────────────────────────────
if curl -s -m 2 "http://127.0.0.1:$PORT/healthz" > /dev/null 2>&1; then
  echo "  ✓ App already running"
else
  echo "  Starting app…"
  ( cd "$APP_DIR" && ./.venv/bin/uvicorn webapp:app --host 127.0.0.1 --port $PORT \
      > ../app.log 2>&1 ) &
  APP_PID=$!
  for i in {1..20}; do
    sleep 1
    curl -s -m 2 "http://127.0.0.1:$PORT/healthz" > /dev/null 2>&1 && break
  done
  if ! curl -s -m 2 "http://127.0.0.1:$PORT/healthz" > /dev/null 2>&1; then
    echo ""
    echo "  ✗ The app didn't start. Last few lines of the log:"
    echo ""
    tail -15 app.log | sed 's/^/     /'
    echo ""
    echo "  Press any key to close."
    read -k1 -s
    exit 1
  fi
  echo "  ✓ App running"
fi

# ── publish it, if the tunnel has been set up ───────────────
if [ -f "$TUNNEL_CONF" ]; then
  echo "  Publishing to justgrit.thewatsonfactor.dev…"
  cloudflared tunnel --config "$TUNNEL_CONF" run > tunnel.log 2>&1 &
  TUNNEL_PID=$!
  sleep 5
  if kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "  ✓ Live at https://justgrit.thewatsonfactor.dev"
  else
    echo "  ✗ Tunnel failed — see tunnel.log. The app still works on this Mac."
  fi
else
  echo "  · Not published yet (run 'Setup web address.command' once)"
fi

echo ""
echo "  Opening…"
open "http://127.0.0.1:$PORT/"
echo ""
echo "  ────────────────────────────────────────────"
echo "   Keep this window open while you work."
echo "   Close it (or press Control-C) to stop."
echo "  ────────────────────────────────────────────"
echo ""

wait
