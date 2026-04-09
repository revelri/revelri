#!/usr/bin/env python3
"""Generate hero.svg from emotion-hero ASCII art with CSS-animated emotion colors.

Connects to the emotion-hero backend via WebSocket to sample live emotion ratios
from Bluesky Jetstream, then renders the ASCII art as an SVG with diagonal color
wave animations driven by those ratios.

Falls back to equal ratios (0.2 each) if the backend is unavailable.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from html import escape
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
EMOTION_HERO_DIR = Path(os.environ.get(
    "EMOTION_HERO_DIR",
    str(ROOT_DIR.parent / "ascii" / "emotion-hero")
))
OUTPUT_SVG = ROOT_DIR / "hero.svg"

# Emotion definitions matching shared/emotions.ts
EMOTIONS = [
    {"id": "serene",     "hex": "#87a99e"},
    {"id": "vibrant",    "hex": "#ad9387"},
    {"id": "melancholy", "hex": "#919baf"},
    {"id": "curious",    "hex": "#a5a091"},
    {"id": "content",    "hex": "#9ba591"},
]

WS_PORT = int(os.environ.get("WS_PORT", "8090"))
SAMPLE_DURATION = int(os.environ.get("SAMPLE_DURATION", "30"))
WARMUP_SECONDS = int(os.environ.get("WARMUP_SECONDS", "5"))

# SVG layout
FONT_SIZE = 7
CHAR_WIDTH = 4.202  # Courier New at 7px
LINE_HEIGHT = 8
NUM_ZONES = 8
CYCLE_DURATION = 12  # seconds per full color cycle
ZONE_STAGGER = 1.5   # seconds between zone animation starts
PULSE_DURATION = 4   # seconds per opacity pulse cycle
BG_COLOR = "#0d0d0d"
PADDING = 10


def find_ascii_art() -> str:
    """Find and read the ASCII art file from the content directory."""
    content_dir = EMOTION_HERO_DIR / "content"

    # Priority: art.txt > any .txt that isn't colors.txt
    art_txt = content_dir / "art.txt"
    if art_txt.exists():
        return art_txt.read_text()

    for f in sorted(content_dir.glob("*.txt")):
        if f.name != "colors.txt":
            return f.read_text()

    raise FileNotFoundError(f"No ASCII art file found in {content_dir}")


def parse_art(raw: str) -> tuple[list[str], int, int, int]:
    """Parse ASCII art, trim blank lines, find content bounds.

    Returns (lines, min_col, max_col, first_row) where lines are the
    non-blank rows and min/max_col define the horizontal content extent.
    """
    all_lines = raw.split("\n")

    # Find first and last non-blank lines
    first = next((i for i, l in enumerate(all_lines) if l.strip()), 0)
    last = next((i for i in range(len(all_lines) - 1, -1, -1) if all_lines[i].strip()), len(all_lines) - 1)
    lines = all_lines[first:last + 1]

    # Find horizontal content bounds
    min_col = float("inf")
    max_col = 0
    for line in lines:
        if not line.strip():
            continue
        stripped = line.rstrip()
        leading = len(stripped) - len(stripped.lstrip())
        min_col = min(min_col, leading)
        max_col = max(max_col, len(stripped))

    if min_col == float("inf"):
        min_col = 0

    return lines, int(min_col), int(max_col), first


import math

# Metaball blob centers — 5 emotion sources with pseudo-random positions
# Positions are in normalized [0,1] space
BLOB_CENTERS = [
    (0.25, 0.20),  # serene — upper left
    (0.75, 0.15),  # vibrant — upper right
    (0.50, 0.50),  # melancholy — center
    (0.20, 0.75),  # curious — lower left
    (0.80, 0.80),  # content — lower right
]


def assign_zone(row_idx: int, line: str, min_col: int, max_col: int, total_rows: int) -> int:
    """Assign a line to the nearest emotion blob (metaball-style)."""
    stripped = line.rstrip()
    if not stripped.strip():
        return 0
    leading = len(stripped) - len(stripped.lstrip())
    col_center = (leading + len(stripped)) / 2 - min_col
    col_range = max(max_col - min_col, 1)
    nx = col_center / col_range
    ny = row_idx / max(total_rows, 1)

    # Find the blob with highest field contribution (inverse square distance)
    best_blob = 0
    best_field = -1.0
    for i, (bx, by) in enumerate(BLOB_CENTERS):
        dx, dy = nx - bx, ny - by
        dist_sq = dx * dx + dy * dy + 0.001
        field = 1.0 / dist_sq
        if field > best_field:
            best_field = field
            best_blob = i
    return best_blob


def generate_keyframes(ratios: dict[str, float]) -> str:
    """Generate CSS @keyframes for metaball-style emotion blobs.

    Each blob (emotion) pulses between its own color and neighboring
    emotion colors, weighted by emotion ratios. The effect is organic
    color bleeding between blob regions.
    """
    css_parts = []

    total = sum(ratios.values()) or 1.0
    normalized = {eid: v / total for eid, v in ratios.items()}

    for i, emotion in enumerate(EMOTIONS):
        hex_color = emotion["hex"]
        ratio = normalized.get(emotion["id"], 0.2)

        # Each blob cycles: own color → blend toward neighbors → back
        # Neighbors are the two adjacent emotions (wrapping)
        prev_e = EMOTIONS[(i - 1) % 5]
        next_e = EMOTIONS[(i + 1) % 5]

        # Dominant emotion gets more time at its own color
        own_pct = max(ratio * 100, 30)
        blend_pct = (100 - own_pct) / 2

        stops = [
            f"  0% {{ fill: {hex_color}; }}",
            f"  {own_pct:.0f}% {{ fill: {hex_color}; }}",
            f"  {own_pct + blend_pct:.0f}% {{ fill: {next_e['hex']}; }}",
            f"  {own_pct + blend_pct * 2:.0f}% {{ fill: {prev_e['hex']}; }}",
            f"  100% {{ fill: {hex_color}; }}",
        ]
        css_parts.append(f"@keyframes blob{i} {{\n" + "\n".join(stops) + "\n}")

    # Pulse for organic breathing
    css_parts.append("@keyframes pulse {\n  0%, 100% { opacity: 0.75; }\n  50% { opacity: 1; }\n}")

    # Blob class definitions with staggered timing for organic feel
    for i, emotion in enumerate(EMOTIONS):
        # Each blob has a different cycle duration for asynchronous drift
        duration = CYCLE_DURATION + i * 2
        delay = -(i * 3)
        pulse_delay = -(i * (PULSE_DURATION / 5))
        css_parts.append(
            f".z{i} {{ fill: {emotion['hex']}; "
            f"animation: blob{i} {duration}s ease-in-out infinite {delay}s, "
            f"pulse {PULSE_DURATION}s ease-in-out infinite {pulse_delay:.1f}s; }}"
        )

    return "\n".join(css_parts)


def assign_char_zone(row_idx: int, col_idx: int, min_col: int, max_col: int, total_rows: int) -> int:
    """Assign a character position to the nearest emotion blob."""
    col_range = max(max_col - min_col, 1)
    nx = (col_idx - min_col) / col_range
    ny = row_idx / max(total_rows, 1)

    best_blob = 0
    best_field = -1.0
    for i, (bx, by) in enumerate(BLOB_CENTERS):
        dx, dy = nx - bx, ny - by
        dist_sq = dx * dx + dy * dy + 0.001
        field = 1.0 / dist_sq
        if field > best_field:
            best_field = field
            best_blob = i
    return best_blob


def generate_svg(art_lines: list[str], min_col: int, max_col: int, ratios: dict[str, float]) -> str:
    """Generate the complete SVG string with per-segment metaball coloring."""
    total_rows = len(art_lines)
    content_width = (max_col - min_col) * CHAR_WIDTH
    content_height = total_rows * LINE_HEIGHT
    vb_w = content_width + PADDING * 2
    vb_h = content_height + PADDING * 2

    keyframes_css = generate_keyframes(ratios)

    text_elements = []
    for row_idx, line in enumerate(art_lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue

        leading = len(stripped) - len(stripped.lstrip())
        content = stripped.lstrip()
        y = row_idx * LINE_HEIGHT + PADDING + FONT_SIZE

        # Group consecutive characters by zone to avoid per-char elements
        segments: list[tuple[int, int, str]] = []  # (start_col, zone, text)
        seg_start = leading
        seg_zone = assign_char_zone(row_idx, leading, min_col, max_col, total_rows)
        seg_chars: list[str] = []

        for ci, ch in enumerate(content):
            col = leading + ci
            zone = assign_char_zone(row_idx, col, min_col, max_col, total_rows)
            if zone != seg_zone:
                segments.append((seg_start, seg_zone, "".join(seg_chars)))
                seg_start = col
                seg_zone = zone
                seg_chars = [ch]
            else:
                seg_chars.append(ch)
        if seg_chars:
            segments.append((seg_start, seg_zone, "".join(seg_chars)))

        for col_start, zone, text in segments:
            x = (col_start - min_col) * CHAR_WIDTH + PADDING
            escaped = escape(text)
            text_elements.append(
                f'<text x="{x:.1f}" y="{y:.1f}" class="z{zone}" xml:space="preserve">{escaped}</text>'
            )

    texts = "\n  ".join(text_elements)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w:.0f} {vb_h:.0f}">
<style>
text {{ font-family: 'Courier New', Courier, monospace; font-size: {FONT_SIZE}px; }}
{keyframes_css}
</style>
<rect width="100%" height="100%" fill="{BG_COLOR}"/>
<g>
  {texts}
</g>
</svg>
"""


async def sample_emotions(port: int, duration: int) -> dict[str, float]:
    """Connect to the emotion-hero backend WS and average emotion ratios."""
    import websockets

    uri = f"ws://localhost:{port}"
    ratios: dict[str, list[float]] = {e["id"]: [] for e in EMOTIONS}

    try:
        async with websockets.connect(uri, open_timeout=10) as ws:
            end_time = time.time() + duration
            while time.time() < end_time:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw)
                    if msg.get("type") == "emotions":
                        for eid, state in msg["emotions"].items():
                            if eid in ratios:
                                ratios[eid].append(state["value"])
                except asyncio.TimeoutError:
                    continue
    except Exception as e:
        print(f"[generate_hero] WS sampling failed: {e}", file=sys.stderr)
        return {}

    # Average collected samples
    averaged = {}
    for eid, values in ratios.items():
        averaged[eid] = sum(values) / len(values) if values else 0.2
    return averaged


def start_backend() -> subprocess.Popen | None:
    """Start the emotion-hero backend as a subprocess."""
    backend_dir = EMOTION_HERO_DIR / "backend"
    index_js = backend_dir / "dist" / "index.js"

    if not index_js.exists():
        print(f"[generate_hero] Backend not built: {index_js}", file=sys.stderr)
        return None

    env = os.environ.copy()
    env["WS_PORT"] = str(WS_PORT)

    try:
        proc = subprocess.Popen(
            ["node", str(index_js)],
            cwd=str(backend_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc
    except Exception as e:
        print(f"[generate_hero] Failed to start backend: {e}", file=sys.stderr)
        return None


def stop_backend(proc: subprocess.Popen | None):
    """Gracefully stop the backend subprocess."""
    if proc is None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except Exception:
        pass


def main():
    print("[generate_hero] Reading ASCII art...")
    raw_art = find_ascii_art()
    art_lines, min_col, max_col, _ = parse_art(raw_art)
    print(f"[generate_hero] Art: {len(art_lines)} lines, cols {min_col}-{max_col}")

    # Try to sample live emotion data
    ratios = {}
    backend_proc = None

    try:
        print(f"[generate_hero] Starting backend (port {WS_PORT})...")
        backend_proc = start_backend()

        if backend_proc:
            print(f"[generate_hero] Warming up ({WARMUP_SECONDS}s)...")
            time.sleep(WARMUP_SECONDS)

            if backend_proc.poll() is not None:
                print("[generate_hero] Backend exited early", file=sys.stderr)
            else:
                print(f"[generate_hero] Sampling emotions ({SAMPLE_DURATION}s)...")
                ratios = asyncio.run(sample_emotions(WS_PORT, SAMPLE_DURATION))
    finally:
        stop_backend(backend_proc)

    if not ratios:
        print("[generate_hero] Using fallback ratios (equal distribution)")
        ratios = {e["id"]: 0.2 for e in EMOTIONS}
    else:
        summary = ", ".join(f"{k}={v:.3f}" for k, v in ratios.items())
        print(f"[generate_hero] Sampled ratios: {summary}")

    print("[generate_hero] Generating SVG...")
    svg = generate_svg(art_lines, min_col, max_col, ratios)

    OUTPUT_SVG.write_text(svg)
    size_kb = OUTPUT_SVG.stat().st_size / 1024
    print(f"[generate_hero] Wrote {OUTPUT_SVG} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
