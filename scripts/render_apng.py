#!/usr/bin/env python3
"""Render card.svg animation frames and assemble into APNG.

Pauses CSS animations and seeks them deterministically to each frame
target so playback speed doesn't drift with screenshot wall time.

Dependencies: playwright, Pillow (9.1+ for APNG support)
"""

import sys
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
    from io import BytesIO

    svg_uri = SVG_PATH.as_uri()

    print(f"Capturing {FRAME_COUNT} frames at {FPS}fps...")
    frames: list[Image.Image] = []

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
            png_bytes = page.screenshot(
                type="png",
                clip={"x": 0, "y": 0, "width": WIDTH, "height": height},
                timeout=120000,
            )
            frames.append(Image.open(BytesIO(png_bytes)).convert("RGBA"))
            if (i + 1) % 10 == 0:
                print(f"  frame {i + 1}/{FRAME_COUNT}")

        browser.close()

    print(f"Assembling APNG ({len(frames)} frames)...")
    frames[0].save(
        APNG_PATH,
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_DELAY_MS,
        loop=0,  # infinite loop
    )

    size_kb = APNG_PATH.stat().st_size / 1024
    print(f"Done! {APNG_PATH.name}: {size_kb:.0f} KB ({len(frames)} frames, {FPS}fps, {DURATION_S}s cycle)")


if __name__ == "__main__":
    main()
