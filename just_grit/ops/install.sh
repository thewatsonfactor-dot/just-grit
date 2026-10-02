#!/bin/zsh
# One-time: put the three launchd jobs in place and start them. Safe to re-run.
set -u
APP="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"; mkdir -p "$LA" "$APP/backups/nightly"
for j in app backup watchdog; do
  cp "$APP/ops/com.justgrit.$j.plist" "$LA/"
  launchctl bootout "gui/$(id -u)/com.justgrit.$j" 2>/dev/null
done
# hand the app over from nohup to launchd
kill $(pgrep -f "uvicorn webapp:app") 2>/dev/null; sleep 2
for j in app backup watchdog; do launchctl bootstrap "gui/$(id -u)" "$LA/com.justgrit.$j.plist"; done
sleep 6; curl -s -o /dev/null -w "app /health %{http_code}\n" http://127.0.0.1:8080/health
zsh "$APP/ops/backup.sh"
launchctl print "gui/$(id -u)/com.justgrit.app" | grep -E "state|pid" | head -3
