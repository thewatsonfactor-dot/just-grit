#!/bin/zsh
# Just Grit — find prospects from Google Maps.
# Double-click. Type what you're looking for. Get a call-ready CSV.

cd "$(dirname "$0")" || exit 1

# ── first run: get the API key ──────────────────────────────────────────────
if [ ! -f .google_api_key ] && [ -z "$GOOGLE_MAPS_API_KEY" ]; then
  echo ""
  echo "  ┌──────────────────────────────────────────────────────────────┐"
  echo "  │  One-time setup: Google Maps API key                         │"
  echo "  └──────────────────────────────────────────────────────────────┘"
  echo ""
  echo "  1. Go to  console.cloud.google.com"
  echo "  2. Create a project (any name)"
  echo "  3. APIs & Services → Library → search 'Places API (New)' → Enable"
  echo "  4. APIs & Services → Credentials → Create credentials → API key"
  echo "  5. Click the key → Restrict it to 'Places API (New)'"
  echo ""
  echo "  Billing must be enabled on the project, but there's a free monthly"
  echo "  allowance. Each search page = 1 request, up to 20 businesses."
  echo ""
  echo -n "  Paste your API key here (or press Enter to quit): "
  read -r key
  if [ -z "$key" ]; then echo "  Nothing saved. Run this again when you have a key."; exit 0; fi
  echo "$key" > .google_api_key
  chmod 600 .google_api_key
  echo "  Saved to .google_api_key (readable only by you)."
  echo ""
fi

echo ""
echo -n "  What are you looking for? (e.g. barber shop, roofing contractor): "
read -r what
[ -z "$what" ] && { echo "  Nothing to search for."; exit 0; }

echo -n "  Which city? (Enter = San Antonio, TX): "
read -r city
city="${city:-San Antonio, TX}"

echo -n "  How many pages? 1-3, each = 20 businesses (Enter = 2): "
read -r pages

echo ""
./just_grit/.venv/bin/python findprospects.py "$what" \
    --city "$city" --pages "${pages:-2}" --max-reviews 3000

echo ""
echo -n "  Build the call sheet from this now? [y/N]: "
read -r go
if [[ "$go" == "y" || "$go" == "Y" ]]; then
  slug=$(echo "$what" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\{1,\}/-/g; s/^-//; s/-$//')
  echo ""
  echo "  Vertical? [1] restaurant [2] construction [3] appointment [4] multilocation [5] generic"
  echo -n "  Number (Enter = generic): "
  read -r v
  case "${v:-5}" in
    1) VERT=restaurant ;; 2) VERT=construction ;; 3) VERT=appointment ;;
    4) VERT=multilocation ;; *) VERT=generic ;;
  esac
  if ! curl -s -m 3 http://127.0.0.1:8080/health > /dev/null 2>&1; then
    (cd just_grit && nohup ./.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8080 \
       > ../server.log 2>&1 &)
    sleep 6
  fi
  OUT="callsheet_$(date +%Y-%m-%d).html"
  ./just_grit/.venv/bin/python callsheet.py "prospects_${slug}.csv" \
      --vertical "$VERT" --calls 8 --out "$OUT" && open "$OUT"
fi

echo ""
echo "  Press any key to close."
read -k1 -s
