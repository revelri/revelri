#!/usr/bin/env python3
"""Sample live emotion ratios from the Zeitgeist backend WebSocket.

Connects to the always-on Zeitgeist service that taps Bluesky Jetstream,
collects emotion ratio snapshots over a configurable window, and returns
averaged ratios. Falls back to equal distribution if unavailable.
"""

import asyncio
import json
import os
import sys
import time

EMOTIONS = [
    {"id": "serene",     "hex": "#87a99e"},
    {"id": "vibrant",    "hex": "#ad9387"},
    {"id": "melancholy", "hex": "#919baf"},
    {"id": "curious",    "hex": "#a5a091"},
    {"id": "content",    "hex": "#9ba591"},
]

EMOTION_IDS = [e["id"] for e in EMOTIONS]

WS_PORT = int(os.environ.get("WS_PORT", "8090"))
SAMPLE_DURATION = int(os.environ.get("SAMPLE_DURATION", "30"))

FALLBACK_RATIOS = {e["id"]: 0.2 for e in EMOTIONS}


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
        averaged[eid] = sum(values) / len(values) if values else 0.2
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
