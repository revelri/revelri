#!/usr/bin/env python3
"""Generate GitHub profile SVG cards from API data."""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
CONFIG_PATH = ROOT / "config.yml"
MOCK_DATA_PATH = ROOT / "scripts" / "mock_data.json"

# CRT color palette
# Heatmap gradient derived from Zeitgeist emotion palette.
# 11 stops: empty (#333333) + 10 stops interpolated from deep green-grey -> teal.
def _lerp_hex(a: str, b: str, t: float) -> str:
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    b = int(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


_HEATMAP_RAMP_START = "#2a3d35"
_HEATMAP_RAMP_END = "#6aa8c0"
HEATMAP_COLORS = ["#333333"] + [
    _lerp_hex(_HEATMAP_RAMP_START, _HEATMAP_RAMP_END, i / 9) for i in range(10)
]


def _run_gh(cmd, label="gh"):
    """Run a gh CLI command with retry on rate-limit and transient network errors."""
    for attempt in range(3):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return json.loads(result.stdout)
        stderr = result.stderr.lower()
        if "rate limit" in stderr or "403" in stderr or "429" in stderr:
            wait = 2 ** attempt * 5
            print(f"Rate limited on {label}, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        # Transient network errors during long fetches drop responses;
        # retry briefly so a single blip doesn't truncate the repo set.
        if any(s in stderr for s in ("error connecting", "timeout", "tls handshake", "eof", "connection reset")):
            wait = 2 + attempt * 3
            print(f"Network error on {label}, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        print(f"Error calling {label}: {result.stderr}", file=sys.stderr)
        return None
    print(f"Failed after 3 attempts: {label}", file=sys.stderr)
    return None


def gh_api(endpoint, method="GET"):
    """Call GitHub API via gh CLI."""
    cmd = ["gh", "api", endpoint]
    if method != "GET":
        cmd.extend(["--method", method])
    return _run_gh(cmd, label=endpoint)


def gh_graphql(query, **variables):
    """Call GitHub GraphQL API via gh CLI."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, val in variables.items():
        cmd.extend(["-f", f"{key}={val}"])
    return _run_gh(cmd, label="graphql")


def load_config():
    """Load config.yml (simple key: value parsing, no yaml dep)."""
    config = {"name": "revelri", "tagline": ""}
    if not CONFIG_PATH.exists():
        return config
    current_list = None
    current_item = {}
    with open(CONFIG_PATH) as f:
        for line in f:
            raw = line.rstrip()
            stripped = raw.strip()
            if stripped.startswith("#") or not stripped:
                continue
            # Detect list items (  - name: value)
            if raw.startswith("    - ") or raw.startswith("  - "):
                if current_list is not None:
                    if current_item:
                        config.setdefault(current_list, []).append(current_item)
                    current_item = {}
                k, _, v = stripped.lstrip("- ").partition(":")
                current_item[k.strip()] = v.strip().strip('"').strip("'")
                continue
            # Detect continuation of list item (    key: value)
            if current_list and (raw.startswith("      ") or raw.startswith("    ")) and ":" in stripped and not stripped.startswith("- "):
                k, _, v = stripped.partition(":")
                current_item[k.strip()] = v.strip().strip('"').strip("'")
                continue
            # Flush pending list item
            if current_item and current_list:
                config.setdefault(current_list, []).append(current_item)
                current_item = {}
            # Top-level key
            if ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not val:
                # Start of a list section
                current_list = key
            elif key in ("name", "tagline"):
                config[key] = val
                current_list = None
        # Flush final item
        if current_item and current_list:
            config.setdefault(current_list, []).append(current_item)
    return config


def fetch_contributions():
    """Fetch contribution data via GraphQL."""
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    year_start = f"{now.year}-01-01T00:00:00Z"

    query = """
    query($from: DateTime!, $yearStart: DateTime!) {
      viewer {
        contributionsCollection(from: $from) {
          totalCommitContributions
          restrictedContributionsCount
          totalPullRequestContributions
          totalIssueContributions
        }
        yearCollection: contributionsCollection(from: $yearStart) {
          totalCommitContributions
          restrictedContributionsCount
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                contributionCount
                date
                weekday
              }
            }
          }
        }
      }
    }
    """
    return gh_graphql(query, **{"from": week_ago, "yearStart": year_start})


REPO_FIELDS = """
  nodes {
    name
    pushedAt
    isPrivate
    owner { login }
    languages(first: 5, orderBy: {field: SIZE, direction: DESC}) {
      edges {
        size
        node { name }
      }
    }
  }
"""

EXTRA_ORGS = ["Chorosyne"]


def fetch_repos():
    """Fetch repos viewer owns plus repos under EXTRA_ORGS, merged + sorted by pushedAt desc."""
    viewer_query = """
    query {
      viewer {
        repositories(first: 100, ownerAffiliations: OWNER, orderBy: {field: PUSHED_AT, direction: DESC}) {
          REPO_FIELDS_PLACEHOLDER
        }
      }
    }
    """.replace("REPO_FIELDS_PLACEHOLDER", REPO_FIELDS)

    org_query = """
    query($login: String!) {
      organization(login: $login) {
        repositories(first: 100, orderBy: {field: PUSHED_AT, direction: DESC}) {
          REPO_FIELDS_PLACEHOLDER
        }
      }
    }
    """.replace("REPO_FIELDS_PLACEHOLDER", REPO_FIELDS)

    merged = []
    viewer = gh_graphql(viewer_query) or {}
    merged.extend(viewer.get("data", {}).get("viewer", {}).get("repositories", {}).get("nodes", []) or [])

    for org in EXTRA_ORGS:
        result = gh_graphql(org_query, login=org) or {}
        nodes = (result.get("data", {}).get("organization") or {}).get("repositories", {}).get("nodes", []) or []
        merged.extend(nodes)

    merged.sort(key=lambda r: r.get("pushedAt") or "", reverse=True)
    return {"data": {"viewer": {"repositories": {"nodes": merged}}}}


def fetch_lines_changed(repos_data, username):
    """Fetch lines changed this week using GraphQL bulk queries."""
    nodes = repos_data.get("data", {}).get("viewer", {}).get("repositories", {}).get("nodes", [])
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    week_ago_str = week_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    additions = 0
    deletions = 0

    for repo in nodes:
        pushed = repo.get("pushedAt")
        if not pushed:
            continue
        dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
        if dt < week_ago:
            break  # Sorted by pushedAt desc

        owner = (repo.get("owner") or {}).get("login") or username

        query = """
        query($owner: String!, $name: String!, $since: GitTimestamp!) {
          repository(owner: $owner, name: $name) {
            defaultBranchRef {
              target {
                ... on Commit {
                  history(since: $since, first: 100) {
                    nodes {
                      additions
                      deletions
                      author { user { login } }
                    }
                  }
                }
              }
            }
          }
        }
        """
        result = gh_graphql(query, owner=owner, name=repo["name"], since=week_ago_str)
        if not result:
            continue

        history = (result.get("data", {}).get("repository") or {})
        branch = (history.get("defaultBranchRef") or {}).get("target", {})
        commits = (branch.get("history") or {}).get("nodes", [])
        for c in commits:
            author = (c.get("author") or {}).get("user") or {}
            if author.get("login", "").lower() == username.lower():
                additions += c.get("additions", 0)
                deletions += c.get("deletions", 0)

    return additions, deletions


def calc_streak(calendar_data):
    """Calculate current and longest streak from contribution calendar."""
    all_days = []
    for week in calendar_data.get("weeks", []):
        for day in week.get("contributionDays", []):
            all_days.append(day)

    # Sort by date
    all_days.sort(key=lambda d: d["date"])

    # Current streak: walk backwards from today
    # Treat today and yesterday as "in progress" to handle API lag
    current_streak = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    for day in reversed(all_days):
        if day["date"] > today:
            continue
        if day["contributionCount"] > 0:
            current_streak += 1
        elif day["date"] in (today, yesterday):
            continue
        else:
            break

    # Longest streak
    longest = 0
    current = 0
    for day in all_days:
        if day["contributionCount"] > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0

    return current_streak, longest


def calc_last_commit_ago(repos_data):
    """Calculate time since most recent push across all repos."""
    nodes = repos_data.get("data", {}).get("viewer", {}).get("repositories", {}).get("nodes", [])
    if not nodes:
        return "unknown"

    latest = None
    for repo in nodes:
        pushed = repo.get("pushedAt")
        if pushed:
            dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
            if latest is None or dt > latest:
                latest = dt

    if latest is None:
        return "unknown"

    delta = datetime.now(timezone.utc) - latest
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours:02d}h")
    parts.append(f"{minutes:02d}m ago")
    return " ".join(parts)


def aggregate_languages(repos_data):
    """Aggregate language sizes across recently pushed repos."""
    nodes = repos_data.get("data", {}).get("viewer", {}).get("repositories", {}).get("nodes", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    lang_sizes = {}

    for repo in nodes:
        pushed = repo.get("pushedAt")
        if not pushed:
            continue
        dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
        if dt < cutoff:
            continue
        for edge in repo.get("languages", {}).get("edges", []):
            name = edge["node"]["name"]
            size = edge["size"]
            lang_sizes[name] = lang_sizes.get(name, 0) + size

    # Sort by size, show individually if ≥3%, lump the rest as "Other"
    sorted_langs = sorted(lang_sizes.items(), key=lambda x: x[1], reverse=True)
    total = sum(s for _, s in sorted_langs)
    if total == 0:
        return [("None", 100)]

    result = []
    other = 0
    for name, size in sorted_langs:
        pct = round(size / total * 100)
        if pct >= 3 and len(result) < 5:
            result.append((name, pct))
        else:
            other += pct

    if other > 0:
        result.append(("Other", other))

    # Ensure percentages sum to 100
    diff = 100 - sum(p for _, p in result)
    if result and diff != 0:
        name, pct = result[0]
        result[0] = (name, pct + diff)

    return result




LANG_COLORS = {
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "Python": "#3572A5",
    "Rust": "#dea584",
    "Go": "#00ADD8",
    "Java": "#b07219",
    "C": "#555555",
    "C++": "#f34b7d",
    "C#": "#178600",
    "Ruby": "#701516",
    "PHP": "#4F5D95",
    "Shell": "#89e051",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "Lua": "#000080",
    "Zig": "#ec915c",
    "Kotlin": "#A97BFF",
    "Swift": "#F05138",
    "Dart": "#00B4AB",
    "Svelte": "#ff3e00",
    "Vue": "#41b883",
    "Nix": "#7e7eff",
    "SCSS": "#c6538c",
    "Haskell": "#5e5086",
    "Elixir": "#6e4a7e",
    "Clojure": "#db5855",
    "Scala": "#c22d40",
    "Perl": "#0298c3",
    "R": "#198ce7",
    "Julia": "#a270ba",
    "Erlang": "#B83998",
    "OCaml": "#3be133",
    "F#": "#b845fc",
    "Dockerfile": "#384d54",
    "Makefile": "#427819",
    "GLSL": "#5686a5",
    "HLSL": "#aace60",
    "Vim Script": "#199f4b",
    "Emacs Lisp": "#c065db",
    "PowerShell": "#012456",
    "Objective-C": "#438eff",
    "Assembly": "#6E4C13",
    "Groovy": "#4298b8",
    "Terraform": "#5c4ee5",
    "HCL": "#844FBA",
    "YAML": "#cb171e",
    "TOML": "#9c4221",
    "Jsonnet": "#0064ce",
    "Solidity": "#AA6746",
    "Other": "#8b949e",
    "Move": "#4a137a",
    "V": "#4f87c4",
    "Nim": "#ffc200",
    "Crystal": "#000100",
    "D": "#ba595e",
}
LANG_COLOR_FALLBACKS = ["#6aa8c0", "#6abf7c", "#b89f5e", "#c47a9b", "#7c6abf"]


def lang_color(name, index):
    """Get a vibrant color for a language."""
    return LANG_COLORS.get(name, LANG_COLOR_FALLBACKS[index % len(LANG_COLOR_FALLBACKS)])


def render_language_bars(languages):
    """Generate SVG elements for language bar chart.

    Layout fits inside the LANGUAGES box (x=425..815, y=88..258).
    No header — vertically centered with enlarged fonts.
    """
    lines = []
    max_bar_width = 320
    label_x = 518   # right edge for right-justified names
    bar_x = 528
    pct_x = 805     # right-aligned percentage
    line_height = 30
    font_size = 15
    bar_height = 14

    # Vertically center in box (y=88..258, height=170) with inset padding
    padding = 6
    box_top = 88 + padding
    box_height = 170 - 2 * padding
    n = len(languages)
    content_height = n * line_height
    y_start = box_top + (box_height - content_height) // 2 + line_height // 2

    # Reserve room for the right-aligned percentage so the bar never
    # overdraws the text (was overflowing on 90%+ entries).
    pct_text_room = 50
    max_drawable = pct_x - bar_x - pct_text_room
    for i, (name, pct) in enumerate(languages):
        y = y_start + i * line_height
        bar_width = min(max(4, int(max_bar_width * pct / 100)), max_drawable)
        color = lang_color(name, i)
        # Right-justified language name
        lines.append(
            f'  <text x="{label_x}" y="{y + 2}" text-anchor="end" '
            f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="{font_size}" fill="{color}" font-weight="bold">{name}</text>'
        )
        # Bar
        lines.append(
            f'  <rect x="{bar_x}" y="{y - 8}" width="{bar_width}" height="{bar_height}" rx="2" fill="{color}" opacity="0.85"/>'
        )
        # Percentage right-aligned
        lines.append(
            f'  <text x="{pct_x}" y="{y + 2}" text-anchor="end" '
            f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="{font_size}" fill="{color}" font-weight="bold">{pct}%</text>'
        )

    return "\n".join(lines)




def render_heatmap(calendar_data):
    """Generate SVG heatmap cells from contribution calendar.

    Returns (cells_svg, raw_width, raw_height) so the caller can compute
    a transform that fits the heatmap inside a target box.
    """
    weeks = calendar_data.get("weeks", [])
    cells = []
    cell_size = 11
    gap = 3

    # Find max for color scaling
    max_count = 1
    for week in weeks:
        for day in week.get("contributionDays", []):
            max_count = max(max_count, day["contributionCount"])

    # Take last 52 weeks
    display_weeks = weeks[-52:] if len(weeks) > 52 else weeks
    num_weeks = len(display_weeks)

    # Log-scaled bucketing across 10 non-zero stops (indices 1..10)
    log_max = math.log1p(max_count)

    for wi, week in enumerate(display_weeks):
        for day in week.get("contributionDays", []):
            weekday = day["weekday"]
            count = day["contributionCount"]

            x = wi * (cell_size + gap)
            y = weekday * (cell_size + gap)

            if count == 0:
                ci = 0
            else:
                ratio = math.log1p(count) / log_max if log_max > 0 else 0
                ci = 1 + min(9, int(ratio * 9 + 0.5))

            color = HEATMAP_COLORS[ci]
            cells.append(
                f'  <rect x="{x}" y="{y}" width="{cell_size}" height="{cell_size}" rx="2" fill="{color}"/>'
            )

    raw_width = num_weeks * (cell_size + gap) - gap if num_weeks else 0
    raw_height = 7 * (cell_size + gap) - gap
    return "\n".join(cells), raw_width, raw_height


from sample_emotions import EMOTIONS as JETSTREAM_EMOTIONS, EMOTION_IDS, sample as sample_emotions

# Emotion colors auto-loaded from zeitgeist's colors.txt (via sample_emotions).
EMOTION_COLORS = [e["hex"] for e in JETSTREAM_EMOTIONS]


def _default_blob_centers(n: int) -> list[tuple[float, float]]:
    """Evenly distribute n blob centers around the canvas."""
    if n <= 0:
        return [(0.5, 0.5)]
    if n == 1:
        return [(0.5, 0.5)]
    # Spread on a ring around center, with a slight inset so blobs cover edges too.
    centers = []
    for i in range(n):
        theta = (i / n) * 2 * math.pi - math.pi / 2
        cx = 0.5 + 0.32 * math.cos(theta)
        cy = 0.5 + 0.32 * math.sin(theta)
        centers.append((cx, cy))
    return centers


BLOB_CENTERS = _default_blob_centers(len(EMOTION_COLORS))

ZEITGEIST_DIR = Path(os.environ.get(
    "ZEITGEIST_DIR",
    str(ROOT.parent / "ascii" / "zeitgeist")
))


def _load_production_art() -> list[str]:
    """Load production ASCII art. Checks local content/ first, then Zeitgeist."""
    # Local copy bundled in this repo
    local_art = ROOT / "content" / "art.txt"
    if local_art.exists():
        return local_art.read_text().rstrip("\n").split("\n")

    # Fall back to Zeitgeist directories
    for search_dir in [ZEITGEIST_DIR / "backend" / "content", ZEITGEIST_DIR / "content"]:
        art_txt = search_dir / "art.txt"
        if art_txt.exists():
            return art_txt.read_text().rstrip("\n").split("\n")
        for f in sorted(search_dir.glob("*.txt")):
            if f.name != "colors.txt":
                return f.read_text().rstrip("\n").split("\n")
    return ["* no art found *"]


def _art_bounds(art_lines: list[str]) -> tuple[int, int, int, int]:
    """Find content bounds: (first_row, last_row, min_col, max_col)."""
    first = next((i for i, l in enumerate(art_lines) if l.strip()), 0)
    last = next((i for i in range(len(art_lines) - 1, -1, -1) if art_lines[i].strip()), len(art_lines) - 1)
    min_col = min((len(l) - len(l.lstrip()) for l in art_lines[first:last + 1] if l.strip()), default=0)
    max_col = max((len(l.rstrip()) for l in art_lines[first:last + 1] if l.strip()), default=1)
    return first, last, min_col, max_col


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _metaball_color(nx: float, ny: float, blob_weights: list[float] | None = None) -> str:
    """Compute blended color at (nx, ny) from all metaball fields.

    blob_weights scales each emotion's field strength — driven by live
    Jetstream ratios so dominant emotions claim more visual territory.
    """
    n = len(EMOTION_COLORS)
    base_radius = 0.32
    if blob_weights is None:
        blob_weights = [1.0] * n
    fields = []
    for i, (bx, by) in enumerate(BLOB_CENTERS):
        dx, dy = nx - bx, ny - by
        dist_sq = dx * dx + dy * dy + 0.0001
        field = blob_weights[i] * (base_radius ** 2) / dist_sq
        fields.append(field)

    total = sum(fields)
    if total < 0.001:
        return EMOTION_COLORS[0]

    r, g, b = 0.0, 0.0, 0.0
    for i in range(n):
        w = fields[i] / total
        cr, cg, cb = _hex_to_rgb(EMOTION_COLORS[i])
        r += cr * w
        g += cg * w
        b += cb * w

    return _rgb_to_hex(int(r), int(g), int(b))


def _quantize_color(hex_color: str, steps: int = 24) -> str:
    """Quantize a hex color to reduce unique class count."""
    r, g, b = _hex_to_rgb(hex_color)
    q = 256 // steps
    r = (r // q) * q + q // 2
    g = (g // q) * q + q // 2
    b = (b // q) * q + q // 2
    return _rgb_to_hex(min(r, 255), min(g, 255), min(b, 255))


def render_ascii_hero(emotion_ratios: dict[str, float] | None = None, y_start: float = 385) -> tuple[str, str, int]:
    """Render production ASCII art with metaball-style soft color blending.

    Returns (css_styles, svg_elements, total_height).
    Colors are computed per-character as a weighted blend from all 5 emotion
    blob centers, scaled by live Jetstream emotion ratios so dominant moods
    claim more territory in the color field.
    """
    from html import escape as html_escape

    # Convert ratio dict to ordered weight list matching EMOTION_IDS
    n_emotions = len(EMOTION_COLORS)
    default_ratio = 1.0 / n_emotions if n_emotions else 0.2
    if emotion_ratios:
        raw = [emotion_ratios.get(eid, default_ratio) for eid in EMOTION_IDS]
        mean_r = sum(raw) / len(raw) or 1.0
        blob_weights = [(r / mean_r) ** 1.5 for r in raw]
    else:
        blob_weights = [1.0] * n_emotions

    art_lines = _load_production_art()
    first, last, min_col, max_col = _art_bounds(art_lines)
    content_lines = art_lines[first:last + 1]
    total_rows = len(content_lines)
    col_range = max(max_col - min_col, 1)

    card_inner = 800 * 0.50  # shrunk to leave room for the legend captions
    char_width_at_1px = 0.6
    font_size = min(8.0, card_inner / (col_range * char_width_at_1px))
    char_width = font_size * char_width_at_1px
    line_height = font_size * 1.15

    total_width = col_range * char_width
    x_offset = (840 - total_width) / 2

    # Collect all unique quantized colors used
    used_colors: set[str] = set()
    elements = []

    for row_idx, line in enumerate(content_lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue

        leading = len(stripped) - len(stripped.lstrip())
        content = stripped.lstrip()
        y = y_start + row_idx * line_height
        ny = row_idx / max(total_rows, 1)

        # Group adjacent characters by quantized metaball color
        segments: list[tuple[int, str, str]] = []  # (start_col, qcolor, chars)
        seg_start = leading
        seg_color = _quantize_color(_metaball_color((leading - min_col) / col_range, ny, blob_weights))
        seg_chars: list[str] = []

        for ci, ch in enumerate(content):
            col = leading + ci
            nx = (col - min_col) / col_range
            qc = _quantize_color(_metaball_color(nx, ny, blob_weights))
            if qc != seg_color:
                segments.append((seg_start, seg_color, "".join(seg_chars)))
                seg_start = col
                seg_color = qc
                seg_chars = [ch]
            else:
                seg_chars.append(ch)
        if seg_chars:
            segments.append((seg_start, seg_color, "".join(seg_chars)))

        for col_start, qcolor, text in segments:
            x = x_offset + (col_start - min_col) * char_width
            escaped = html_escape(text)
            css_class = "c" + qcolor.lstrip("#")
            used_colors.add(qcolor)
            elements.append(
                f'  <text x="{x:.1f}" y="{y:.1f}" class="{css_class}" '
                f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
                f'font-size="{font_size:.1f}" xml:space="preserve">{escaped}</text>'
            )

    # Generate CSS: 5 shared emotion keyframes + per-color class assignments
    # Each emotion keyframe drifts 30% toward its two adjacent neighbors.
    # Each quantized color class is assigned to the nearest emotion's keyframe
    # with a staggered delay for organic variety. This keeps visual fidelity
    # while cutting unique @keyframes from ~80 to 5 (massive GPU savings).
    css_parts = []

    # Build one shared keyframe per emotion
    n_emo = len(EMOTION_COLORS)
    for i, ec in enumerate(EMOTION_COLORS):
        er, eg, eb = _hex_to_rgb(ec)
        prev_ec = EMOTION_COLORS[(i - 1) % n_emo]
        next_ec = EMOTION_COLORS[(i + 1) % n_emo]
        pr, pg, pb = _hex_to_rgb(prev_ec)
        nr, ng, nb = _hex_to_rgb(next_ec)
        shift1 = _rgb_to_hex(int(er * 0.7 + pr * 0.3), int(eg * 0.7 + pg * 0.3), int(eb * 0.7 + pb * 0.3))
        shift2 = _rgb_to_hex(int(er * 0.7 + nr * 0.3), int(eg * 0.7 + ng * 0.3), int(eb * 0.7 + nb * 0.3))
        css_parts.append(
            f"@keyframes emo{i} {{\n"
            f"  0%, 100% {{ fill: {ec}; opacity: 0.8; }}\n"
            f"  33% {{ fill: {shift1}; opacity: 1; }}\n"
            f"  66% {{ fill: {shift2}; opacity: 0.9; }}\n"
            f"}}"
        )

    # Assign each quantized color to nearest emotion keyframe
    for qc in sorted(used_colors):
        cls = "c" + qc.lstrip("#")
        r, g, b = _hex_to_rgb(qc)

        # Find nearest emotion
        best_i, best_dist = 0, float("inf")
        for i, ec in enumerate(EMOTION_COLORS):
            er, eg, eb = _hex_to_rgb(ec)
            dist = (r - er) ** 2 + (g - eg) ** 2 + (b - eb) ** 2
            if dist < best_dist:
                best_i, best_dist = i, dist

        # Stagger duration and delay by color hash for organic feel
        h = hash(qc) & 0xFFFFFFFF
        dur = 4 + h % 5
        delay = -((h >> 8) % 7)
        css_parts.append(
            f".{cls} {{ fill: {qc}; animation: emo{best_i} {dur}s ease-in-out infinite {delay}s; }}"
        )

    hero_height = int(total_rows * line_height + 40)
    return "\n".join(css_parts), "\n".join(elements), hero_height


def render_template(template_name, **kwargs):
    """Load a template and fill in placeholders."""
    template_path = TEMPLATES / template_name
    with open(template_path) as f:
        content = f.read()

    for key, val in kwargs.items():
        content = content.replace(f"{{{key}}}", str(val))

    return content


def get_username():
    """Get the authenticated GitHub username."""
    result = subprocess.run(
        ["gh", "api", "/user", "--jq", ".login"],
        capture_output=True, text=True
    )
    return result.stdout.strip()


def _relative_time(iso_ts: str) -> str:
    """Compact human delta: 2h, 3d, 2w."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return ""
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 3600:
        return f"{max(s // 60, 1)}m"
    if s < 86400:
        return f"{s // 3600}h"
    if s < 86400 * 14:
        return f"{s // 86400}d"
    if s < 86400 * 60:
        return f"{s // (86400 * 7)}w"
    return f"{s // (86400 * 30)}mo"


def fetch_active_repos(repos_data, username, limit=5, window_days=30, exclude=None):
    """Find user's most active repos in the last `window_days`.

    Returns list of dicts: {owner, name, count, commits: [{sha7, msg, ago}, ...]}.
    Only counts commits authored by `username` on the default branch.

    `exclude` is a set of normalized identifiers matched case-insensitively
    against both bare repo names and `owner/name` forms.
    """
    nodes = repos_data.get("data", {}).get("viewer", {}).get("repositories", {}).get("nodes", [])
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    candidates = []
    exclude = {e.lower() for e in (exclude or set())}

    # Resolve viewer's node ID once; GraphQL's history(author:{id:...}) filter
    # gives us a proper totalCount instead of capping at the fetched node count.
    viewer_id_resp = gh_graphql("query{viewer{id}}") or {}
    viewer_id = (viewer_id_resp.get("data", {}).get("viewer") or {}).get("id")

    # CommitAuthor is an input object; gh_graphql passes -f flat strings so
    # we inline the author filter directly. viewer_id is an opaque base64
    # node ID safe to substitute.
    author_clause = f', author: {{id: "{viewer_id}"}}' if viewer_id else ""
    query = f"""
    query($owner: String!, $name: String!, $since: GitTimestamp!) {{
      repository(owner: $owner, name: $name) {{
        defaultBranchRef {{
          target {{
            ... on Commit {{
              history(since: $since, first: 3{author_clause}) {{
                totalCount
                nodes {{
                  oid
                  messageHeadline
                  committedDate
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    """

    for repo in nodes:
        name = repo.get("name", "")
        pushed = repo.get("pushedAt")
        if not pushed:
            continue
        dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
        if dt < since:
            break  # sorted desc by pushedAt — nothing after this point qualifies

        owner = (repo.get("owner") or {}).get("login") or username
        if name.lower() in exclude or f"{owner}/{name}".lower() in exclude:
            continue
        result = gh_graphql(query, owner=owner, name=name, since=since_str)
        if not result:
            continue
        target = ((result.get("data", {}).get("repository") or {}).get("defaultBranchRef") or {}).get("target") or {}
        history = (target.get("history") or {})
        total = history.get("totalCount") or 0
        if total == 0:
            continue
        commits = history.get("nodes", []) or []
        candidates.append({
            "owner": owner,
            "name": name,
            "count": total,
            "commits": [
                {
                    "sha7": (c.get("oid") or "")[:7],
                    "msg": c.get("messageHeadline") or "",
                    "ago": _relative_time(c.get("committedDate") or ""),
                }
                for c in commits[:3]
            ],
        })

    candidates.sort(key=lambda r: r["count"], reverse=True)
    return candidates[:limit]


def render_active_repos_panel(active_repos, y_start=296):
    """Render top-5 active repos with last 3 commits each indented underneath.

    Returns (svg_string, bottom_y) so the caller can place the next panel.
    """
    lines = []
    y = y_start
    repo_font = 14
    commit_font = 11
    repo_line_h = 17
    commit_line_h = 14
    gap = 6

    if not active_repos:
        lines.append(
            f'  <text class="zg-primary" x="16" y="{y}" font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="{repo_font}">(no recent activity)</text>'
        )
        return "\n".join(lines), y + repo_line_h

    for repo in active_repos:
        head = f"› {repo['owner']}/{repo['name']} ({repo['count']})"
        head = head[:46]
        lines.append(
            f'  <text class="zg-primary" x="16" y="{y}" font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="{repo_font}" font-weight="bold">{head}</text>'
        )
        y += repo_line_h
        for c in repo["commits"]:
            msg = c["msg"]
            label = f"  • {c['sha7']} {msg} · {c['ago']}"
            # Truncate to fit the panel column; 50% wider than the original
            # 54-char budget so longer commit headlines remain legible.
            label = label[:81]
            from html import escape as _esc
            lines.append(
                f'  <text class="zg-secondary" x="22" y="{y}" font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
                f'font-size="{commit_font}">{_esc(label)}</text>'
            )
            y += commit_line_h
        y += gap

    return "\n".join(lines), y


def render_emotion_legend(emotions, x, y_start, hero_height, ingest_ts=None):
    """Vertical legend next to the ASCII hero.

    Each row: a 12x12 swatch pulsing on the same `emo{i}` keyframe as the hero
    chars, plus the emotion id label. Header `EMOTIONS` above, `Generated by
    zeitgeist` footer below.
    """
    from html import escape as _esc

    lines = []
    # Header
    lines.append(
        f'  <text class="zg-secondary" x="{x}" y="{y_start}" font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
        f'font-size="12" font-weight="bold" letter-spacing="2" filter="url(#glow)">EMOTIONS</text>'
    )

    row_h = 18
    swatch = 11
    label_dx = 18
    for i, e in enumerate(emotions):
        ry = y_start + 14 + i * row_h
        lines.append(
            f'  <rect x="{x}" y="{ry - swatch + 2}" width="{swatch}" height="{swatch}" rx="2" '
            f'fill="{e["hex"]}" style="animation: emo{i} 6s ease-in-out infinite;"/>'
        )
        lines.append(
            f'  <text class="zg-primary" x="{x + label_dx}" y="{ry}" '
            f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" font-size="12">{_esc(e["id"])}</text>'
        )

    footer_y = y_start + 14 + len(emotions) * row_h + 12
    sub_y = footer_y + 12
    # Clamp so both lines stay inside hero area
    max_sub_y = y_start + hero_height - 6
    if sub_y > max_sub_y:
        offset = sub_y - max_sub_y
        footer_y -= offset
        sub_y -= offset
    lines.append(
        f'  <text class="zg-primary" x="{x}" y="{footer_y}" '
        f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" font-size="12" '
        f'font-weight="bold" font-style="italic">generated by zeitgeist</text>'
    )
    if ingest_ts:
        lines.append(
            f'  <text class="zg-secondary" x="{x}" y="{sub_y + 2}" '
            f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" font-size="10" '
            f'font-style="italic">based on Bluesky jetstream</text>'
        )
        lines.append(
            f'  <text class="zg-secondary" x="{x}" y="{sub_y + 14}" '
            f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" font-size="10" '
            f'font-style="italic">ingest, {ingest_ts}</text>'
        )

    return "\n".join(lines)


def render_stats_panel(current_streak, longest_streak, avg_per_day, last_commit_ago):
    """Generate SVG for the STATS panel."""
    stats = [
        ("streak: ", f"{current_streak}d (best {longest_streak}d)"),
        ("avg: ", f"{avg_per_day}/day"),
        ("last: ", f"{last_commit_ago}"),
    ]
    lines = []
    y_start = 296
    for i, (label, value) in enumerate(stats):
        y = y_start + i * 20
        lines.append(
            f'  <text x="815" y="{y}" text-anchor="end" font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="17.3" font-weight="bold">'
            f'<tspan class="zg-secondary">{label}</tspan>'
            f'<tspan class="zg-primary">{value}</tspan>'
            f'</text>'
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate GitHub profile SVG cards")
    parser.add_argument("--mock", action="store_true", help="Use cached mock data instead of API")
    parser.add_argument("--dump-data", action="store_true", help="Save API responses to mock_data.json")
    args = parser.parse_args()

    config = load_config()

    if args.mock:
        if not MOCK_DATA_PATH.exists():
            print("No mock data found. Run with --dump-data first.", file=sys.stderr)
            sys.exit(1)
        print("Using mock data...")
        mock = json.loads(MOCK_DATA_PATH.read_text())
        contrib_data = mock["contributions"]
        repos_data = mock["repos"]
        username = mock["username"]
        additions = mock.get("additions", 0)
        deletions = mock.get("deletions", 0)
    else:
        # Validate environment
        auth_check = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if auth_check.returncode != 0:
            print("gh is not authenticated. Run 'gh auth login' or set GH_TOKEN.", file=sys.stderr)
            sys.exit(1)

        username = get_username()
        print(f"Generating cards for {username}...")

        print("Fetching contributions...")
        contrib_data = fetch_contributions()
        if not contrib_data:
            print("Failed to fetch contributions", file=sys.stderr)
            sys.exit(1)

        print("Fetching repos...")
        repos_data = fetch_repos()
        if not repos_data:
            print("Failed to fetch repos", file=sys.stderr)
            sys.exit(1)

        print("Fetching lines changed...")
        try:
            additions, deletions = fetch_lines_changed(repos_data, username)
        except Exception as e:
            print(f"Warning: couldn't fetch lines changed: {e}", file=sys.stderr)
            additions, deletions = 0, 0

        if args.dump_data:
            mock = {
                "contributions": contrib_data,
                "repos": repos_data,
                "username": username,
                "additions": additions,
                "deletions": deletions,
            }
            MOCK_DATA_PATH.write_text(json.dumps(mock, indent=2))
            print(f"Mock data saved to {MOCK_DATA_PATH}")

    # Extract contribution stats
    viewer = contrib_data["data"]["viewer"]
    weekly = viewer["contributionsCollection"]
    yearly = viewer["yearCollection"]
    calendar = yearly["contributionCalendar"]

    weekly_commits = weekly["totalCommitContributions"] + weekly["restrictedContributionsCount"]
    total_contributions = calendar["totalContributions"]

    current_streak, longest_streak = calc_streak(calendar)
    avg_per_day = round(weekly_commits / 7, 1)
    last_commit_ago = calc_last_commit_ago(repos_data)
    languages = aggregate_languages(repos_data)
    print("Computing top active repos (last 30d)...")
    exclude_set = {r["name"] for r in config.get("exclude_repos", []) if r.get("name")}
    if exclude_set:
        print(f"Excluding repos from ACTIVE REPOS: {sorted(exclude_set)}")
    active_repos = fetch_active_repos(
        repos_data, username, limit=5, window_days=30, exclude=exclude_set
    )

    lines_added = f"+{additions:,}" if additions else "+--"
    lines_deleted = f"-{deletions:,}" if deletions else "---"

    # Build language summary for accessibility
    lang_summary = ", ".join(f"{name} {pct}%" for name, pct in languages[:3])

    # Sample live Jetstream emotion ratios for the ASCII hero
    print(f"Zeitgeist emotions ({len(JETSTREAM_EMOTIONS)}): {[e['id'] for e in JETSTREAM_EMOTIONS]}")
    print("Sampling Bluesky Jetstream emotions...")
    emotion_ratios = sample_emotions()
    ingest_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")

    # Render unified card
    print("Rendering card.svg...")

    # Fit heatmap into LANGUAGES box (x=425..815, y=88..258 -> width 390, height 170).
    heatmap_cells, hm_raw_w, hm_raw_h = render_heatmap(calendar)
    HM_BOX_X, HM_BOX_Y, HM_BOX_W, HM_BOX_H = 425, 4, 390, 80
    if hm_raw_w > 0 and hm_raw_h > 0:
        hm_scale = min(HM_BOX_W / hm_raw_w, HM_BOX_H / hm_raw_h)
        hm_drawn_w = hm_raw_w * hm_scale
        hm_drawn_h = hm_raw_h * hm_scale
        hm_tx = HM_BOX_X + (HM_BOX_W - hm_drawn_w)  # right-align in box
        hm_ty = HM_BOX_Y + (HM_BOX_H - hm_drawn_h) / 2
        heatmap_transform = f"matrix({hm_scale:.4f}, 0, 0, {hm_scale:.4f}, {hm_tx:.2f}, {hm_ty:.2f})"
    else:
        heatmap_transform = "translate(425, 4)"

    # Auto-fit title font to THIS WEEK box (x=16..406; inset to 30..394 -> ~364px usable).
    title_text = config["name"]
    title_max_width = 364
    title_max_font = 31.2
    title_min_font = 18
    if title_text:
        # TX-02 monospace advance is ~0.6 * font_size per glyph.
        ideal = title_max_width / (len(title_text) * 0.6)
        name_font_size = max(title_min_font, min(title_max_font, ideal))
    else:
        name_font_size = title_max_font

    # Layout the dynamic ACTIVE REPOS panel; let it push the hero down as needed.
    active_panel_svg, active_panel_bottom = render_active_repos_panel(active_repos, y_start=296)
    hero_top = max(385, active_panel_bottom + 14)
    hero_css, ascii_hero_svg, hero_height = render_ascii_hero(emotion_ratios, y_start=hero_top)
    emotion_legend_svg = render_emotion_legend(
        JETSTREAM_EMOTIONS, x=635, y_start=hero_top - 6,
        hero_height=hero_height, ingest_ts=ingest_ts,
    )
    card_height = hero_top + hero_height + 10

    card = render_template(
        "card.svg.template",
        name=title_text,
        name_font_size=f"{name_font_size:.1f}",
        heatmap_cells=heatmap_cells,
        heatmap_transform=heatmap_transform,
        weekly_commits=str(weekly_commits),
        lines_added=lines_added,
        lines_deleted=lines_deleted,
        language_bars=render_language_bars(languages),
        projects_panel=active_panel_svg,
        stats_panel=render_stats_panel(current_streak, longest_streak, avg_per_day, last_commit_ago),
        ascii_hero=ascii_hero_svg,
        emotion_legend=emotion_legend_svg,
        total_contributions=str(total_contributions),
        current_year=str(datetime.now(timezone.utc).year),
        language_summary=lang_summary,
        card_height=str(card_height),
        divider_y=f"{hero_top - 20:.1f}",
        hero_css=hero_css,
    )
    (ROOT / "card.svg").write_text(card)

    print("Done! Generated card.svg")


if __name__ == "__main__":
    main()
