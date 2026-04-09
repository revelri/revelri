#!/usr/bin/env bash
# Hot-reload dev server for profile card design iteration.
# Usage: ./dev.sh [--port PORT] [--no-open]
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8080}"
NO_OPEN="${2:-}"

# Ensure mock data exists
if [ ! -f scripts/mock_data.json ]; then
  echo "No mock data found. Fetching from GitHub API..."
  python3 scripts/generate_cards.py --dump-data
fi

# Generate card from mock data
python3 scripts/generate_cards.py --mock

# Copy dev.html to root for serving
cp templates/dev.html dev.html

# Start HTTP server
echo "Starting dev server on http://localhost:$PORT"
python3 -m http.server "$PORT" --bind 127.0.0.1 &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null; rm -f dev.html" EXIT

# Open browser
if [ "$NO_OPEN" != "--no-open" ] && command -v xdg-open &>/dev/null; then
  sleep 0.5
  xdg-open "http://localhost:$PORT/dev.html" &
fi

# Watch for changes and regenerate
echo "Watching for changes... (Ctrl+C to stop)"
if command -v inotifywait &>/dev/null; then
  inotifywait -m -r -e modify,create \
    --include '\.(py|svg|template|yml)$' \
    templates/ scripts/generate_cards.py config.yml 2>/dev/null |
  while read -r; do
    echo "Change detected, regenerating..."
    python3 scripts/generate_cards.py --mock 2>&1 || true
  done
else
  echo "inotifywait not found. Install inotify-tools for auto-reload."
  echo "Falling back to 2s poll..."
  while true; do
    sleep 2
    python3 scripts/generate_cards.py --mock 2>&1 || true
  done
fi
