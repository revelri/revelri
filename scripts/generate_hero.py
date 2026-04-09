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
CYCLE_DURATION = 24  # seconds per full color cycle
ZONE_STAGGER = 3     # seconds between zone animation starts
PULSE_DURATION = 8   # seconds per opacity pulse cycle
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


def assign_zone(row_idx: int, line: str, min_col: int, max_col: int, total_rows: int) -> int:
    """Assign a line to a diagonal color zone (0 to NUM_ZONES-1)."""
    stripped = line.rstrip()
    if not stripped.strip():
        return 0
    leading = len(stripped) - len(stripped.lstrip())
    col_center = (leading + len(stripped)) / 2 - min_col
    col_range = max(max_col - min_col, 1)
    diagonal = (row_idx / max(total_rows, 1) + col_center / col_range) / 2
    return int(diagonal * NUM_ZONES) % NUM_ZONES


def generate_keyframes(ratios: dict[str, float]) -> str:
    """Generate CSS @keyframes rules from emotion ratios.

    Each zone cycles through all 5 emotion colors, spending time
    proportional to each emotion's ratio. Zones use the same keyframes
    but different animation-delays for the wave effect.
    """
    css_parts = []

    # Normalize ratios to sum to 1.0
    total = sum(ratios.values()) or 1.0
    normalized = {eid: v / total for eid, v in ratios.items()}

    # Build color sequence ordered by ratio (dominant first)
    ordered = sorted(EMOTIONS, key=lambda e: normalized.get(e["id"], 0.2), reverse=True)

    # Track each zone's starting color for initial fill
    zone_start_colors = []

    for zone in range(NUM_ZONES):
        # Rotate the color order per zone for variety
        rotated = ordered[zone % len(ordered):] + ordered[:zone % len(ordered)]
        zone_start_colors.append(rotated[0]["hex"])
        stops = []
        pct = 0.0

        for i, emotion in enumerate(rotated):
            ratio = normalized.get(emotion["id"], 0.2)
            duration_pct = ratio * 100
            hex_color = emotion["hex"]

            stops.append(f"  {pct:.1f}% {{ fill: {hex_color}; }}")
            pct += duration_pct
            if i < len(rotated) - 1:
                stops.append(f"  {min(pct, 100):.1f}% {{ fill: {hex_color}; }}")

        stops.append(f"  100% {{ fill: {rotated[0]['hex']}; }}")

        css_parts.append(f"@keyframes z{zone} {{\n" + "\n".join(stops) + "\n}")

    # Pulse animation for opacity
    css_parts.append("@keyframes pulse {\n  0%, 100% { opacity: 0.7; }\n  50% { opacity: 1; }\n}")

    # Zone class definitions with staggered delays
    # Use negative delays so animations start mid-cycle (immediate visible motion)
    for zone in range(NUM_ZONES):
        # Negative delay = animation starts as if it began N seconds ago
        delay = -(zone * ZONE_STAGGER)
        pulse_delay = -(zone * (PULSE_DURATION / NUM_ZONES))
        css_parts.append(
            f".z{zone} {{ fill: {zone_start_colors[zone]}; "
            f"animation: z{zone} {CYCLE_DURATION}s linear infinite {delay}s, "
            f"pulse {PULSE_DURATION}s ease-in-out infinite {pulse_delay:.1f}s; }}"
        )

    return "\n".join(css_parts)


def generate_svg(art_lines: list[str], min_col: int, max_col: int, ratios: dict[str, float]) -> str:
    """Generate the complete SVG string."""
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

        zone = assign_zone(row_idx, line, min_col, max_col, total_rows)
        # x offset: shift content left by min_col, add padding
        leading = len(stripped) - len(stripped.lstrip())
        x = (leading - min_col) * CHAR_WIDTH + PADDING
        y = row_idx * LINE_HEIGHT + PADDING + FONT_SIZE  # baseline offset

        # Escape special XML characters
        escaped = escape(stripped.lstrip())

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
