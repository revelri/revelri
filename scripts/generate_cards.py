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
# Heatmap gradient derived from Zeitgeist emotion palette
HEATMAP_COLORS = ["#333333", "#2a3d35", "#3d6a50", "#6abf7c", "#6aa8c0"]


def _run_gh(cmd, label="gh"):
    """Run a gh CLI command with retry on rate-limit (403/429)."""
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

EXTRA_ORGS = ["Dromoturge"]


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

    for i, (name, pct) in enumerate(languages):
        y = y_start + i * line_height
        bar_width = min(max(4, int(max_bar_width * pct / 100)), 265)
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
    """Generate SVG heatmap cells from contribution calendar."""
    weeks = calendar_data.get("weeks", [])
    cells = []
    cell_size = 11
    gap = 3
    x_offset = 40
    y_offset = 20

    # Find max for color scaling
    max_count = 1
    for week in weeks:
        for day in week.get("contributionDays", []):
            max_count = max(max_count, day["contributionCount"])

    # Take last 52 weeks
    display_weeks = weeks[-52:] if len(weeks) > 52 else weeks

    for wi, week in enumerate(display_weeks):
        for day in week.get("contributionDays", []):
            weekday = day["weekday"]
            count = day["contributionCount"]

            x = x_offset + wi * (cell_size + gap)
            y = y_offset + weekday * (cell_size + gap)

            # Map count to color index (0-4)
            if count == 0:
                ci = 0
            elif count <= max_count * 0.25:
                ci = 1
            elif count <= max_count * 0.5:
                ci = 2
            elif count <= max_count * 0.75:
                ci = 3
            else:
                ci = 4

            color = HEATMAP_COLORS[ci]
            cells.append(
                f'  <rect x="{x}" y="{y}" width="{cell_size}" height="{cell_size}" rx="2" fill="{color}"/>'
            )

    return "\n".join(cells)


# Zeitgeist colors — boosted vibrancy to match CRT card palette
EMOTION_COLORS = [
    "#6aa8c0",  # Serene - vivid teal
    "#c47a9b",  # Vibrant - warm magenta-rose
    "#7c6abf",  # Melancholy - rich purple
    "#b89f5e",  # Curious - amber gold
    "#6abf7c",  # Content - vivid green
]

# Metaball blob centers in normalized [0,1] space
BLOB_CENTERS = [
    (0.25, 0.20),  # serene
    (0.75, 0.15),  # vibrant
    (0.50, 0.50),  # melancholy
    (0.20, 0.75),  # curious
    (0.80, 0.80),  # content
]

from sample_emotions import EMOTIONS as JETSTREAM_EMOTIONS, EMOTION_IDS, sample as sample_emotions

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
    """Compute blended color at (nx, ny) from all 5 metaball fields.

    blob_weights scales each emotion's field strength — driven by live
    Jetstream ratios so dominant emotions claim more visual territory.
    """
    base_radii = [0.35, 0.25, 0.40, 0.30, 0.28]
    if blob_weights is None:
        blob_weights = [1.0] * 5
    fields = []
    for i, (bx, by) in enumerate(BLOB_CENTERS):
        dx, dy = nx - bx, ny - by
        dist_sq = dx * dx + dy * dy + 0.0001
        # Metaball field: r² / d², scaled by emotion weight
        field = blob_weights[i] * (base_radii[i] ** 2) / dist_sq
        fields.append(field)

    total = sum(fields)
    if total < 0.001:
        return EMOTION_COLORS[0]

    r, g, b = 0.0, 0.0, 0.0
    for i in range(5):
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


def render_ascii_hero(emotion_ratios: dict[str, float] | None = None) -> tuple[str, str, int]:
    """Render production ASCII art with metaball-style soft color blending.

    Returns (css_styles, svg_elements, total_height).
    Colors are computed per-character as a weighted blend from all 5 emotion
    blob centers, scaled by live Jetstream emotion ratios so dominant moods
    claim more territory in the color field.
    """
    from html import escape as html_escape

    # Convert ratio dict to ordered weight list matching EMOTION_IDS
    if emotion_ratios:
        raw = [emotion_ratios.get(eid, 0.2) for eid in EMOTION_IDS]
        # Normalize so mean=1.0, then amplify contrast so ratios matter visually
        mean_r = sum(raw) / len(raw) or 1.0
        blob_weights = [(r / mean_r) ** 1.5 for r in raw]
    else:
        blob_weights = [1.0] * 5

    art_lines = _load_production_art()
    first, last, min_col, max_col = _art_bounds(art_lines)
    content_lines = art_lines[first:last + 1]
    total_rows = len(content_lines)
    col_range = max(max_col - min_col, 1)

    card_inner = 800 * 0.67  # 33% smaller than full width
    char_width_at_1px = 0.6
    font_size = min(10, card_inner / (col_range * char_width_at_1px))
    char_width = font_size * char_width_at_1px
    line_height = font_size * 1.15

    total_width = col_range * char_width
    x_offset = (840 - total_width) / 2
    y_start = 385

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

    # Build the 5 shared keyframes
    for i, ec in enumerate(EMOTION_COLORS):
        er, eg, eb = _hex_to_rgb(ec)
        prev_ec = EMOTION_COLORS[(i - 1) % 5]
        next_ec = EMOTION_COLORS[(i + 1) % 5]
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


def fetch_top_repos(repos_data, limit=3):
    """Get top recently-pushed repos with descriptions."""
    nodes = repos_data.get("data", {}).get("viewer", {}).get("repositories", {}).get("nodes", [])
    result = []
    for repo in nodes:
        if repo.get("isPrivate"):
            continue
        name = repo.get("name", "")
        if name.lower() == "revelri":
            continue
        result.append(name)
        if len(result) >= limit:
            break
    return result


def render_projects_panel(repos, config):
    """Generate SVG for the PROJECTS panel."""
    featured = config.get("featured_repos", [])
    if featured:
        items = [(r["name"], r.get("description", "")) for r in featured[:3]]
    else:
        items = [(name, "") for name in repos[:3]]

    lines = []
    y_start = 296
    for i, (name, desc) in enumerate(items):
        y = y_start + i * 20
        label = f"› {name}" if not desc else f"› {name} — {desc}"
        label = label[:48]
        lines.append(
            f'  <text class="zg-primary" x="16" y="{y}" font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="17.3" font-weight="bold">{label}</text>'
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
    top_repos = fetch_top_repos(repos_data)

    lines_added = f"+{additions:,}" if additions else "+--"
    lines_deleted = f"-{deletions:,}" if deletions else "---"

    # Build language summary for accessibility
    lang_summary = ", ".join(f"{name} {pct}%" for name, pct in languages[:3])

    # Sample live Jetstream emotion ratios for the ASCII hero
    print("Sampling Bluesky Jetstream emotions...")
    emotion_ratios = sample_emotions()

    # Render unified card
    print("Rendering card.svg...")
    hero_css, ascii_hero_svg, hero_height = render_ascii_hero(emotion_ratios)
    card_height = 385 + hero_height + 10  # header/panels + hero + bottom pad

    card = render_template(
        "card.svg.template",
        name=config["name"],
        heatmap_cells=render_heatmap(calendar),
        weekly_commits=str(weekly_commits),
        lines_added=lines_added,
        lines_deleted=lines_deleted,
        language_bars=render_language_bars(languages),
        projects_panel=render_projects_panel(top_repos, config),
        stats_panel=render_stats_panel(current_streak, longest_streak, avg_per_day, last_commit_ago),
        ascii_hero=ascii_hero_svg,
        total_contributions=str(total_contributions),
        current_year=str(datetime.now(timezone.utc).year),
        language_summary=lang_summary,
        card_height=str(card_height),
        hero_css=hero_css,
    )
    (ROOT / "card.svg").write_text(card)

    print("Done! Generated card.svg")


if __name__ == "__main__":
    main()
