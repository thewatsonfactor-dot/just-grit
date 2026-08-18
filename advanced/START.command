#!/bin/zsh
# Just Grit Marketing — double-click this file to start the app.
# It launches the analyzer + Scout dashboard, then opens your browser.
# To stop it: close this Terminal window, or press Control-C in it.

cd "$(dirname "$0")/just_grit" || exit 1

# If something is already running on port 8080, reuse it instead of erroring.
if curl -s http://127.0.0.1:8080/health > /dev/null 2>&1; then
  echo "Just Grit is already running. Opening it..."
  open http://localhost:8080/
  exit 0
fi

echo "Starting Just Grit Marketing..."
open http://localhost:8080/ &

# Runs in the foreground so this window stays open and shows the log.
# Closing the window shuts the server down.
exec ./.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8080
