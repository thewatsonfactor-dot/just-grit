#!/bin/zsh
# Nightly: a consistent copy of every workspace database, gzipped, kept 30
# days. Uses sqlite's own .backup so a copy taken mid-write is still whole.
# Also ages out parked (deleted) workspaces after 30 days.
set -u
APP="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${JUST_GRIT_DATA:-$(dirname "$APP")}"
OUT="$APP/backups/nightly"
mkdir -p "$OUT" "$APP/backups/deleted"
STAMP="$(date +%Y%m%d-%H%M)"
n=0
for f in "$DATA"/justgrit*.db; do
  [ -f "$f" ] || continue
  base="$(basename "$f" .db)"
  if sqlite3 "$f" ".backup '$OUT/$base-$STAMP.db'"; then
    gzip -f "$OUT/$base-$STAMP.db" && n=$((n+1))
  else
    echo "backup: FAILED $f" >&2
  fi
done
find "$OUT" -name '*.db.gz' -mtime +30 -delete
find "$APP/backups/deleted" -name '*.db' -mtime +30 -delete
echo "backup: $n database(s) -> $OUT ($STAMP)"
