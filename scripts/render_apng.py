#!/usr/bin/env python3
"""Render card.svg animation frames and assemble into APNG.

Pauses CSS animations and seeks them deterministically to each frame
target so playback speed doesn't drift with screenshot wall time.

Dependencies: playwright, Pillow (9.1+ for APNG support)
"""

import sys
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = ROOT / "card.svg"
APNG_PATH = ROOT / "card.png"  # .png — APNG is backwards-compatible

FPS = 10
DURATION_S = 6  # longest keyframe cycle
FRAME_COUNT = FPS * DURATION_S  # 60 frames
FRAME_DELAY_MS = 1000 // FPS  # 100ms per frame
WIDTH = 840  # native SVG width


def main():
    if not SVG_PATH.exists():
        print("card.svg not found — run generate_cards.py first", file=sys.stderr)
        sys.exit(1)

    from playwright.sync_api import sync_playwright
    from PIL import Image

    svg_uri = SVG_PATH.as_uri()

    print(f"Capturing {FRAME_COUNT} frames at {FPS}fps...")

    with tempfile.TemporaryDirectory() as tmp:
        frame_paths = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(svg_uri, wait_until="networkidle")
            page.wait_for_timeout(2000)  # let fonts render

            dims = page.evaluate("""() => {
                const svg = document.querySelector('svg');
                const r = svg.getBoundingClientRect();
                return { width: r.width, height: r.height };
            }""")
            height = int(dims["height"])
            page.set_viewport_size({"width": WIDTH, "height": height})
            page.wait_for_timeout(500)

            page.evaluate("() => { for (const a of document.getAnimations()) a.pause(); }")

            for i in range(FRAME_COUNT):
                target_ms = i * FRAME_DELAY_MS
                page.evaluate(
                    "(t) => { for (const a of document.getAnimations()) a.currentTime = t; }",
                    target_ms,
                )
                frame_path = os.path.join(tmp, f"frame_{i:04d}.png")
                page.screenshot(
                    path=frame_path,
                    type="png",
                    clip={"x": 0, "y": 0, "width": WIDTH, "height": height},
                    timeout=120000,
                )
                frame_paths.append(frame_path)
                if (i + 1) % 10 == 0:
                    print(f"  frame {i + 1}/{FRAME_COUNT}")

            browser.close()

        # Ping-pong: append reversed frames (skip first and last to avoid doubling)
        pingpong_paths = frame_paths + frame_paths[-2:0:-1]

        print(f"Assembling APNG ({len(pingpong_paths)} frames, ping-pong)...")

        # Load frames for APNG assembly after browser has closed and freed its memory
        first = Image.open(pingpong_paths[0]).convert("RGBA")
        rest = [Image.open(p).convert("RGBA") for p in pingpong_paths[1:]]
        first.save(
            APNG_PATH,
            save_all=True,
            append_images=rest,
            duration=FRAME_DELAY_MS,
            loop=0,  # infinite loop
        )
        for img in rest:
            img.close()
        first.close()

    size_kb = APNG_PATH.stat().st_size / 1024
    cycle_s = len(pingpong_paths) / FPS
    print(f"Done! {APNG_PATH.name}: {size_kb:.0f} KB ({len(pingpong_paths)} frames, {FPS}fps, {cycle_s:.1f}s cycle)")


if __name__ == "__main__":
    main()
