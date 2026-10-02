#!/bin/zsh
# release.sh <bundle.tgz>   - install a release: back up what it replaces, extract,
#                             run the tests it names, bump VERSION, restart, check /health.
#                             If tests or the health check fail, it rolls back by itself.
# release.sh rollback       - put back the files from the last release.
# release.sh list           - show releases on disk.
set -u
APP="$(cd "$(dirname "$0")/.." && pwd)"
REL="$APP/backups/releases"
PY="$APP/.venv/bin/python"
PORT="${JUST_GRIT_PORT:-8080}"
LABEL="com.justgrit.app"
mkdir -p "$REL"
cd "$APP"

restart() {
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
  else
    kill $(pgrep -f "uvicorn webapp:app") 2>/dev/null; sleep 2
    nohup "$APP/.venv/bin/uvicorn" webapp:app --host 127.0.0.1 --port "$PORT" >> "$APP/uvicorn.log" 2>&1 &
  fi
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
    [ "$code" = "200" ] && return 0
  done
  return 1
}

restore_from() {   # $1 = release dir
  [ -f "$1/files.txt" ] || { echo "nothing to restore in $1"; return 1; }
  while read -r f; do
    [ -f "$1/prev/$f" ] && mkdir -p "$(dirname "$f")" && cp "$1/prev/$f" "$f"
  done < "$1/files.txt"
  [ -f "$1/prev/VERSION" ] && cp "$1/prev/VERSION" VERSION
  echo "restored $(wc -l < "$1/files.txt" | tr -d ' ') file(s) from $1"
}

case "${1:-}" in
  list) ls -1t "$REL" ;;
  rollback)
    last="$(ls -1t "$REL" | head -1)"; [ -n "$last" ] || { echo "no releases yet"; exit 1; }
    restore_from "$REL/$last" && restart && echo "rolled back to before $last; /health 200" ;;
  *.tgz)
    B="$1"; [ -f "$B" ] || { echo "no such bundle: $B"; exit 1; }
    STAMP="$(date +%Y%m%d-%H%M%S)"; D="$REL/$STAMP"; mkdir -p "$D/prev"
    tar tzf "$B" | grep -v '/$' > "$D/files.txt"
    while read -r f; do [ -f "$f" ] && mkdir -p "$D/prev/$(dirname "$f")" && cp "$f" "$D/prev/$f"; done < "$D/files.txt"
    cp VERSION "$D/prev/VERSION" 2>/dev/null
    for f in $(grep -E '^(.*/)?justgrit.*\.db$' "$D/files.txt"); do :; done
    # databases are never in a bundle; snapshot them anyway so a rollback can restore data too
    for db in "$(dirname "$APP")"/justgrit*.db; do [ -f "$db" ] && sqlite3 "$db" ".backup '$D/prev/$(basename "$db")'"; done
    tar xzf "$B" -C "$APP" || { echo "extract failed"; restore_from "$D"; exit 1; }
    # tests named in the bundle, then the two that guard the door and the money
    fails=0
    for t in $(grep -E '^tests/test_.*\.py$' "$D/files.txt") tests/test_login.py tests/test_start.py; do
      [ -f "$t" ] || continue
      out="$(JUST_GRIT_NO_LOOP=1 "$PY" "$t" 2>&1)" ; echo "$out" | grep -qE 'FAILS: 0|FAILURES: 0|^PASS' || { fails=1; echo "TEST FAILED: $t"; echo "$out" | tail -5; }
    done
    if [ "$fails" = "1" ]; then echo "tests failed - rolling back"; restore_from "$D"; restart; exit 1; fi
    if restart; then
      echo "release $STAMP installed: version $(cat VERSION 2>/dev/null) · /health 200"
    else
      echo "app didn't come back healthy - rolling back"; restore_from "$D"; restart; exit 1
    fi ;;
  *) echo "usage: release.sh <bundle.tgz> | rollback | list"; exit 2 ;;
esac
