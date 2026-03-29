#!/usr/bin/env bash
# Capture GIF from the live emotion-hero WebGL visualization.
#
# Requires: chrome/chromium, ffmpeg, node, xvfb-run (for headless environments)
#
# Pipeline:
#   1. Start emotion-hero backend (connects to Bluesky Jetstream)
#   2. Serve the frontend
#   3. Launch Chrome with remote debugging
#   4. Capture frames via CDP Page.captureScreenshot
#   5. Assemble GIF with ffmpeg
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
EMOTION_HERO_DIR="${EMOTION_HERO_DIR:-$(realpath "$ROOT_DIR/../ascii/emotion-hero")}"
OUTPUT_GIF="$ROOT_DIR/hero.gif"
TEMP_DIR="/tmp/emotion-hero-capture"
CHROME="${CHROME:-$(command -v google-chrome-beta || command -v google-chrome-stable || command -v google-chrome || command -v chromium || echo '')}"

FPS="${FPS:-8}"
DURATION="${DURATION:-30}"
TOTAL_FRAMES=$((FPS * DURATION))
WIDTH="${WIDTH:-600}"
HEIGHT="${HEIGHT:-550}"
CDP_PORT=9333
WS_PORT=8090
HTTP_PORT=3099
WARMUP="${WARMUP:-10}"

cleanup() {
  echo "[capture] Cleaning up..."
  kill "$BACKEND_PID" 2>/dev/null || true
  kill "$HTTP_PID" 2>/dev/null || true
  kill "$CHROME_PID" 2>/dev/null || true
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

# Ensure shared JS symlink exists for backend
SHARED_LINK="$EMOTION_HERO_DIR/backend/shared"
if [ ! -e "$SHARED_LINK" ]; then
  ln -s "$EMOTION_HERO_DIR/shared/dist" "$SHARED_LINK"
fi

# Clean temp dir
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"

# 1. Start backend
echo "[capture] Starting emotion-hero backend on port $WS_PORT..."
cd "$EMOTION_HERO_DIR/backend"
WS_PORT=$WS_PORT node dist/index.js &
BACKEND_PID=$!
cd "$ROOT_DIR"
sleep 3

# 2. Serve frontend
echo "[capture] Serving frontend on port $HTTP_PORT..."
cd "$EMOTION_HERO_DIR/frontend"
python3 -m http.server $HTTP_PORT &
HTTP_PID=$!
cd "$ROOT_DIR"
sleep 1

# 3. Launch Chrome with remote debugging
if [ -z "$CHROME" ]; then
  echo "[capture] ERROR: No Chrome/Chromium found. Set CHROME env var."
  exit 1
fi

echo "[capture] Launching Chrome ($CHROME)..."
# Use xvfb-run if no DISPLAY is set (CI/headless environments)
CHROME_ARGS="--remote-debugging-port=$CDP_PORT \
  --no-first-run --no-default-browser-check --disable-extensions \
  --window-size=${WIDTH},${HEIGHT} --ignore-gpu-blocklist \
  --enable-webgl --enable-webgl2-compute-context \
  --user-data-dir=$TEMP_DIR/chrome-profile"

if [ -z "${DISPLAY:-}" ]; then
  xvfb-run -a --server-args="-screen 0 ${WIDTH}x${HEIGHT}x24" \
    "$CHROME" $CHROME_ARGS "http://localhost:$HTTP_PORT" &
else
  "$CHROME" $CHROME_ARGS "http://localhost:$HTTP_PORT" &
fi
CHROME_PID=$!

# Wait for Chrome to be ready
echo "[capture] Waiting for Chrome DevTools on port $CDP_PORT..."
for i in $(seq 1 30); do
  if curl -s "http://127.0.0.1:$CDP_PORT/json" >/dev/null 2>&1; then
    echo "[capture] Chrome ready"
    break
  fi
  sleep 1
done

# 4. Wait for warmup (Jetstream data to flow + WebGL to render)
echo "[capture] Warming up for ${WARMUP}s..."
sleep "$WARMUP"

# Get WebSocket debugger URL for the page
WS_URL=$(curl -s "http://127.0.0.1:$CDP_PORT/json" | python3 -c "
import json, sys
pages = json.load(sys.stdin)
for p in pages:
    if 'localhost:$HTTP_PORT' in p.get('url', ''):
        print(p['webSocketDebuggerUrl'])
        break
")

if [ -z "$WS_URL" ]; then
  echo "[capture] ERROR: Could not find page WebSocket URL"
  exit 1
fi

echo "[capture] Connected to: $WS_URL"

# Hide UI overlays
node -e "
const WebSocket = require('ws');
const ws = new WebSocket('$WS_URL');
ws.on('open', () => {
  // Hide UI
  ws.send(JSON.stringify({
    id: 1, method: 'Runtime.evaluate',
    params: { expression: 'document.getElementById(\"status-panel\").style.display=\"none\"; document.getElementById(\"legend\").style.display=\"none\";' }
  }));
  setTimeout(() => {
    ws.close();
    process.exit(0);
  }, 1000);
});
" 2>/dev/null || true

sleep 1

# 5. Capture frames via CDP
echo "[capture] Capturing $TOTAL_FRAMES frames at ${FPS}fps..."
NODE_PATH="$EMOTION_HERO_DIR/node_modules" node -e "
const WebSocket = require('ws');
const fs = require('fs');
const path = require('path');

const ws = new WebSocket('$WS_URL');
let msgId = 1;
const pending = new Map();

ws.on('message', (data) => {
  const msg = JSON.parse(data.toString());
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
  }
});

function send(method, params = {}) {
  return new Promise((resolve) => {
    const id = msgId++;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

ws.on('open', async () => {
  const total = $TOTAL_FRAMES;
  const interval = 1000 / $FPS;

  for (let i = 0; i < total; i++) {
    const result = await send('Page.captureScreenshot', { format: 'png' });
    const buffer = Buffer.from(result.result.data, 'base64');
    const filename = 'frame-' + String(i).padStart(4, '0') + '.png';
    fs.writeFileSync(path.join('$TEMP_DIR', filename), buffer);
    if ((i + 1) % 10 === 0) process.stdout.write('  ' + (i+1) + '/$TOTAL_FRAMES\n');
    if (i < total - 1) await new Promise(r => setTimeout(r, interval));
  }

  console.log('Captured ' + total + ' frames');
  ws.close();
  process.exit(0);
});
"

# 6. Assemble GIF
echo "[capture] Assembling GIF..."
ffmpeg -y -framerate "$FPS" \
  -i "$TEMP_DIR/frame-%04d.png" \
  -vf "fps=$FPS,scale=${WIDTH}:-1:flags=lanczos,palettegen=max_colors=192:stats_mode=diff" \
  -update 1 "$TEMP_DIR/palette.png" 2>/dev/null

ffmpeg -y -framerate "$FPS" \
  -i "$TEMP_DIR/frame-%04d.png" \
  -i "$TEMP_DIR/palette.png" \
  -lavfi "fps=$FPS,scale=${WIDTH}:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3" \
  "$OUTPUT_GIF" 2>/dev/null

SIZE=$(du -h "$OUTPUT_GIF" | cut -f1)
echo "[capture] Done! $OUTPUT_GIF ($SIZE)"
