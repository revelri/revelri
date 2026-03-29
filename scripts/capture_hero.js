#!/usr/bin/env node
/**
 * Capture GIF from the emotion-hero WebGL visualization.
 *
 * Pipeline:
 *   1. Start the emotion-hero backend (Jetstream → EmotionDetector → SignalProcessor → WsServer)
 *   2. Serve the frontend via a local HTTP server
 *   3. Open headless Chrome via Puppeteer, connect to the frontend
 *   4. Wait for WebSocket data to flow, then capture frames
 *   5. Assemble frames into an animated GIF via ffmpeg
 *   6. Shut everything down
 *
 * Usage: node scripts/capture_hero.js [--frames 60] [--fps 10] [--duration 6] [--size 400x300]
 */

const { execSync, spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');
const os = require('os');

// Configuration
const EMOTION_HERO_DIR = path.resolve(__dirname, '../../ascii/emotion-hero');
const OUTPUT_GIF = path.resolve(__dirname, '..', 'hero.gif');
const TEMP_DIR = path.join(os.tmpdir(), 'emotion-hero-capture');

// Parse CLI args
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const DURATION = parseInt(getArg('duration', '6'), 10); // seconds to capture
const FPS = parseInt(getArg('fps', '10'), 10);
const TOTAL_FRAMES = parseInt(getArg('frames', String(DURATION * FPS)), 10);
const SIZE = getArg('size', '420x300');
const [WIDTH, HEIGHT] = SIZE.split('x').map(Number);

// Ensure temp dir exists
if (fs.existsSync(TEMP_DIR)) {
  fs.rmSync(TEMP_DIR, { recursive: true });
}
fs.mkdirSync(TEMP_DIR, { recursive: true });

let backendProcess = null;
let httpServer = null;
let browser = null;

async function cleanup() {
  if (browser) {
    try { await browser.close(); } catch {}
    browser = null;
  }
  if (httpServer) {
    httpServer.close();
    httpServer = null;
  }
  if (backendProcess) {
    backendProcess.kill('SIGTERM');
    backendProcess = null;
  }
  // Clean up temp frames
  if (fs.existsSync(TEMP_DIR)) {
    fs.rmSync(TEMP_DIR, { recursive: true });
  }
}

process.on('SIGINT', async () => { await cleanup(); process.exit(1); });
process.on('SIGTERM', async () => { await cleanup(); process.exit(1); });

async function startBackend() {
  console.log('[capture] Starting emotion-hero backend...');

  const backendEntry = path.join(EMOTION_HERO_DIR, 'backend', 'dist', 'index.js');
  if (!fs.existsSync(backendEntry)) {
    console.error(`Backend not built. Run: cd ${EMOTION_HERO_DIR} && npm run build`);
    process.exit(1);
  }

  // Ensure shared JS files are accessible at the paths the compiled backend expects.
  // The backend dist imports '../shared/emotions.js' which resolves to backend/shared/emotions.js
  // (relative to backend/dist/). Create a symlink directory at backend/shared → ../shared/dist
  const backendSharedLink = path.join(EMOTION_HERO_DIR, 'backend', 'shared');
  const sharedDistDir = path.join(EMOTION_HERO_DIR, 'shared', 'dist');
  if (fs.existsSync(sharedDistDir) && !fs.existsSync(backendSharedLink)) {
    fs.symlinkSync(sharedDistDir, backendSharedLink);
  }

  return new Promise((resolve, reject) => {
    backendProcess = spawn('node', [backendEntry], {
      cwd: path.join(EMOTION_HERO_DIR, 'backend'),
      env: { ...process.env, WS_PORT: '8090' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let started = false;
    const timeout = setTimeout(() => {
      if (!started) {
        // Give it a chance even without the log message
        started = true;
        resolve();
      }
    }, 8000);

    backendProcess.stdout.on('data', (data) => {
      const msg = data.toString();
      process.stdout.write(`[backend] ${msg}`);
      if (msg.includes('Connected') || msg.includes('started') || msg.includes('listening')) {
        if (!started) {
          started = true;
          clearTimeout(timeout);
          // Wait a bit for Jetstream data to start flowing
          setTimeout(resolve, 3000);
        }
      }
    });

    backendProcess.stderr.on('data', (data) => {
      process.stderr.write(`[backend:err] ${data.toString()}`);
    });

    backendProcess.on('error', (err) => {
      if (!started) {
        clearTimeout(timeout);
        reject(err);
      }
    });

    backendProcess.on('exit', (code) => {
      if (!started) {
        clearTimeout(timeout);
        reject(new Error(`Backend exited with code ${code}`));
      }
    });
  });
}

function startFrontendServer() {
  console.log('[capture] Starting frontend HTTP server...');

  const frontendDir = path.join(EMOTION_HERO_DIR, 'frontend');
  return new Promise((resolve) => {
    httpServer = http.createServer((req, res) => {
      let filePath = path.join(frontendDir, req.url === '/' ? 'index.html' : req.url);
      // Security: prevent directory traversal
      if (!filePath.startsWith(frontendDir)) {
        res.writeHead(403);
        res.end();
        return;
      }
      if (!fs.existsSync(filePath)) {
        res.writeHead(404);
        res.end('Not found');
        return;
      }
      const ext = path.extname(filePath);
      const mimeTypes = {
        '.html': 'text/html',
        '.js': 'application/javascript',
        '.css': 'text/css',
        '.map': 'application/json',
        '.png': 'image/png',
        '.woff2': 'font/woff2',
      };
      res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'application/octet-stream' });
      fs.createReadStream(filePath).pipe(res);
    });

    httpServer.listen(3099, () => {
      console.log('[capture] Frontend serving on http://localhost:3099');
      resolve();
    });
  });
}

async function captureFrames() {
  console.log(`[capture] Capturing ${TOTAL_FRAMES} frames at ${FPS}fps (${DURATION}s)...`);

  // Dynamic import for puppeteer (ESM-compatible)
  let puppeteer;
  try {
    puppeteer = require('puppeteer');
  } catch {
    console.error('[capture] Puppeteer not found. Installing...');
    execSync('npm install puppeteer', { cwd: path.resolve(__dirname, '..'), stdio: 'inherit' });
    puppeteer = require('puppeteer');
  }

  // Use headed mode with virtual display (xvfb) for WebGL2 support.
  // Headless Chromium doesn't support WebGL2 reliably.
  // When DISPLAY is set (real or xvfb), headed mode works with GPU.
  const isHeadless = !process.env.DISPLAY;
  browser = await puppeteer.launch({
    headless: isHeadless,
    args: [
      `--window-size=${WIDTH},${HEIGHT}`,
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--enable-webgl',
      '--enable-webgl2-compute-context',
      '--ignore-gpu-blocklist',
      '--disable-software-rasterizer',
    ],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: WIDTH, height: HEIGHT });

  // Override WebSocket URL to connect to our backend on port 8090
  await page.evaluateOnNewDocument(() => {
    // Intercept the WebSocket constructor to force our port
    const OrigWebSocket = window.WebSocket;
    window.WebSocket = function(url, protocols) {
      // Replace whatever port with 8090
      const newUrl = url.replace(/:\d+$/, ':8090');
      return new OrigWebSocket(newUrl, protocols);
    };
    window.WebSocket.prototype = OrigWebSocket.prototype;
    Object.assign(window.WebSocket, OrigWebSocket);
  });

  // Navigate to the frontend
  await page.goto('http://localhost:3099', { waitUntil: 'networkidle0', timeout: 30000 });

  // Wait for canvas to be ready and WebGL to initialize
  await page.waitForSelector('canvas', { timeout: 10000 });

  // Wait for the loading overlay to dismiss
  await page.waitForFunction(() => {
    const overlay = document.getElementById('loading-overlay');
    return !overlay || overlay.classList.contains('hidden') || !document.contains(overlay);
  }, { timeout: 15000 });

  // Wait a bit for emotion data to start flowing and the visualization to warm up
  console.log('[capture] Waiting for visualization to warm up...');
  await new Promise(r => setTimeout(r, 5000));

  // Hide UI overlays for clean capture
  await page.evaluate(() => {
    const panel = document.getElementById('status-panel');
    if (panel) panel.style.display = 'none';
    const legend = document.getElementById('legend');
    if (legend) legend.style.display = 'none';
  });

  // Capture frames
  const frameInterval = 1000 / FPS;
  for (let i = 0; i < TOTAL_FRAMES; i++) {
    const framePath = path.join(TEMP_DIR, `frame-${String(i).padStart(4, '0')}.png`);
    await page.screenshot({ path: framePath, type: 'png' });
    if (i < TOTAL_FRAMES - 1) {
      await new Promise(r => setTimeout(r, frameInterval));
    }
    if ((i + 1) % 10 === 0) {
      process.stdout.write(`[capture] ${i + 1}/${TOTAL_FRAMES} frames\n`);
    }
  }

  console.log(`[capture] Captured ${TOTAL_FRAMES} frames`);
}

function assembleGif() {
  console.log('[capture] Assembling GIF with ffmpeg...');

  const inputPattern = path.join(TEMP_DIR, 'frame-%04d.png');

  // Two-pass approach for better quality:
  // 1. Generate optimal palette from the frames
  // 2. Use palette to create high-quality GIF
  const palettePath = path.join(TEMP_DIR, 'palette.png');

  execSync(
    `ffmpeg -y -framerate ${FPS} -i "${inputPattern}" -vf "fps=${FPS},scale=${WIDTH}:${HEIGHT}:flags=lanczos,palettegen=max_colors=128:stats_mode=diff" "${palettePath}"`,
    { stdio: 'inherit' }
  );

  execSync(
    `ffmpeg -y -framerate ${FPS} -i "${inputPattern}" -i "${palettePath}" -lavfi "fps=${FPS},scale=${WIDTH}:${HEIGHT}:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3" "${OUTPUT_GIF}"`,
    { stdio: 'inherit' }
  );

  const stats = fs.statSync(OUTPUT_GIF);
  console.log(`[capture] GIF saved: ${OUTPUT_GIF} (${(stats.size / 1024).toFixed(0)}KB)`);
}

async function main() {
  try {
    await startBackend();
    await startFrontendServer();
    await captureFrames();
    assembleGif();
    console.log('[capture] Done!');
  } catch (err) {
    console.error('[capture] Error:', err);
    process.exitCode = 1;
  } finally {
    await cleanup();
  }
}

main();
