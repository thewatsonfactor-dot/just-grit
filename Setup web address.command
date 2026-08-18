#!/bin/zsh
# ─────────────────────────────────────────────────────────────
#  One-time: put Just Grit at justgrit.thewatsonfactor.dev
#
#  Run this once. After that, "JUST GRIT.command" publishes
#  automatically every time you start it.
# ─────────────────────────────────────────────────────────────
cd "$(dirname "$0")" || exit 1

NAME="justgrit"
HOSTNAME="justgrit.thewatsonfactor.dev"
CONF="$HOME/.cloudflared/$NAME.yml"

echo ""
echo "  ┌──────────────────────────────────────────────────┐"
echo "  │  Setting up  $HOSTNAME"
echo "  └──────────────────────────────────────────────────┘"
echo ""

if ! command -v cloudflared > /dev/null 2>&1; then
  echo "  Installing the Cloudflare connector…"
  brew install cloudflared || { echo "  Install failed."; read -k1 -s; exit 1; }
fi

# ── 1. sign in to Cloudflare ────────────────────────────────
if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
  echo "  STEP 1 — A browser window will open."
  echo "  Sign in to Cloudflare and pick the thewatsonfactor.dev domain."
  echo ""
  echo -n "  Press Enter to open it… "
  read
  cloudflared tunnel login || { echo "  Sign-in failed."; read -k1 -s; exit 1; }
  echo ""
fi
echo "  ✓ Signed in to Cloudflare"

# ── 2. create the tunnel ────────────────────────────────────
if cloudflared tunnel list 2>/dev/null | grep -q " $NAME "; then
  echo "  ✓ Tunnel '$NAME' already exists"
else
  cloudflared tunnel create "$NAME" || { echo "  Could not create tunnel."; read -k1 -s; exit 1; }
  echo "  ✓ Tunnel created"
fi

UUID=$(cloudflared tunnel list 2>/dev/null | awk -v n="$NAME" '$2==n {print $1}')
if [ -z "$UUID" ]; then
  echo "  ✗ Couldn't find the tunnel ID. Run: cloudflared tunnel list"
  read -k1 -s; exit 1
fi

# ── 3. point the web address at it ──────────────────────────
cloudflared tunnel route dns "$NAME" "$HOSTNAME" 2>&1 | grep -vi "already exists" || true
echo "  ✓ $HOSTNAME points here"

# ── 4. write the config ─────────────────────────────────────
mkdir -p "$HOME/.cloudflared"
cat > "$CONF" <<EOF
tunnel: $UUID
credentials-file: $HOME/.cloudflared/$UUID.json

ingress:
  - hostname: $HOSTNAME
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF
echo "  ✓ Config written"

echo ""
echo "  ┌──────────────────────────────────────────────────┐"
echo "  │  ALMOST DONE — one more step, in your browser    │"
echo "  └──────────────────────────────────────────────────┘"
echo ""
echo "  Right now anyone with the link could open your app."
echo "  Lock it to your Google account:"
echo ""
echo "   1. Go to  one.dash.cloudflare.com"
echo "   2. Access  →  Applications  →  Add an application  →  Self-hosted"
echo "   3. Name it 'Just Grit', domain: $HOSTNAME"
echo "   4. Add a policy:  Action = Allow,  Include = Emails,"
echo "      and type your email address"
echo "   5. Save"
echo ""
echo "  After that, opening the link asks you to sign in with Google first."
echo ""
echo -n "  Open the Cloudflare dashboard now? [Y/n]: "
read ans
[[ "$ans" != "n" && "$ans" != "N" ]] && open "https://one.dash.cloudflare.com/"

echo ""
echo "  Done. From now on just double-click 'JUST GRIT.command'."
echo "  Press any key to close."
read -k1 -s
