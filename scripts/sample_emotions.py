#!/usr/bin/env python3
"""Sample live emotion ratios from the Zeitgeist backend WebSocket.

Connects to the always-on Zeitgeist service that taps Bluesky Jetstream,
collects emotion ratio snapshots over a configurable window, and returns
averaged ratios. Falls back to equal distribution if unavailable.
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

_FALLBACK_EMOTIONS = [
    {"id": "serene",     "hex": "#87a99e"},
    {"id": "vibrant",    "hex": "#ad9387"},
    {"id": "melancholy", "hex": "#919baf"},
    {"id": "curious",    "hex": "#a5a091"},
    {"id": "content",    "hex": "#9ba591"},
]


def _load_emotions_from_zeitgeist() -> list[dict]:
    """Parse `name=#hex` lines from zeitgeist's colors.txt.

    Source of truth: ${ZEITGEIST_DIR}/backend/content/colors.txt.
    Order is preserved. Returns [] if file missing or empty so caller can fall back.
    """
    zeit_dir = Path(os.environ.get(
        "ZEITGEIST_DIR",
        str(Path(__file__).resolve().parent.parent.parent / "zeitgeist"),
    ))
    colors_path = zeit_dir / "backend" / "content" / "colors.txt"
    if not colors_path.exists():
        return []

    emotions = []
    pat = re.compile(r"^\s*([a-zA-Z_][\w-]*)\s*=\s*(#[0-9a-fA-F]{6})\s*$")
    for raw in colors_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = pat.match(line)
        if m:
            emotions.append({"id": m.group(1).lower(), "hex": m.group(2).lower()})
    return emotions


_loaded = _load_emotions_from_zeitgeist()
if _loaded:
    EMOTIONS = _loaded
else:
    print("[sample_emotions] zeitgeist colors.txt missing/empty — using fallback emotions", file=sys.stderr)
    EMOTIONS = _FALLBACK_EMOTIONS

EMOTION_IDS = [e["id"] for e in EMOTIONS]

WS_PORT = int(os.environ.get("WS_PORT", "8090"))
SAMPLE_DURATION = int(os.environ.get("SAMPLE_DURATION", "30"))

FALLBACK_RATIOS = {e["id"]: 1.0 / len(EMOTIONS) for e in EMOTIONS}


async def _sample_ws(port: int, duration: int) -> dict[str, float]:
    """Connect to Zeitgeist WS and average emotion ratios over duration."""
    import websockets

    uri = f"ws://localhost:{port}"
    ratios: dict[str, list[float]] = {eid: [] for eid in EMOTION_IDS}

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
        print(f"[sample_emotions] WS sampling failed: {e}", file=sys.stderr)
        return {}

    averaged = {}
    for eid, values in ratios.items():
        averaged[eid] = sum(values) / len(values) if values else 1.0 / len(EMOTION_IDS)
    return averaged


def sample(port: int | None = None, duration: int | None = None) -> dict[str, float]:
    """Sample live emotion ratios. Returns fallback on failure."""
    port = port or WS_PORT
    duration = duration or SAMPLE_DURATION

    try:
        print(f"[sample_emotions] Connecting to Zeitgeist (port {port})...")
        print(f"[sample_emotions] Sampling emotions ({duration}s)...")
        ratios = asyncio.run(_sample_ws(port, duration))
    except Exception as e:
        print(f"[sample_emotions] Failed: {e}", file=sys.stderr)
        ratios = {}

    if not ratios:
        print("[sample_emotions] Using fallback ratios (equal distribution)")
        return dict(FALLBACK_RATIOS)

    summary = ", ".join(f"{k}={v:.3f}" for k, v in ratios.items())
    print(f"[sample_emotions] Sampled ratios: {summary}")
    return ratios
