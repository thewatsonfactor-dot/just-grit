#!/bin/zsh
# Just Grit — build today's call sheet.
# Double-click. Pick a list. It scans every site and opens the sheet.

cd "$(dirname "$0")" || exit 1

# make sure the analyzer is running
if ! curl -s -m 3 http://127.0.0.1:8080/health > /dev/null 2>&1; then
  echo "Starting the analyzer..."
  (cd just_grit && nohup ./.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8080 \
     > ../server.log 2>&1 &)
  sleep 6
fi

echo ""
echo "  Which list?"
echo ""
ls -1 prospects_*.csv 2>/dev/null | nl -w3 -s'. '
echo ""
echo -n "  Number (or Enter for the first one): "
read choice
FILE=$(ls -1 prospects_*.csv | sed -n "${choice:-1}p")
[ -z "$FILE" ] && { echo "No list found. Make a prospects_something.csv first."; exit 1; }

echo ""
echo "  Vertical? [1] restaurant  [2] construction  [3] appointment  [4] multilocation  [5] generic"
echo -n "  Number (Enter = restaurant): "
read v
case "${v:-1}" in
  1) VERT=restaurant ;; 2) VERT=construction ;; 3) VERT=appointment ;;
  4) VERT=multilocation ;; *) VERT=generic ;;
esac

echo ""
echo -n "  How many dials today? (Enter = 8): "
read n

OUT="callsheet_$(date +%Y-%m-%d).html"
./just_grit/.venv/bin/python callsheet.py "$FILE" --vertical "$VERT" --calls "${n:-8}" --out "$OUT"
open "$OUT"

echo ""
echo "  Done. Sheet is open in your browser — Cmd-P to print it."
echo "  Press any key to close this window."
read -k1 -s
