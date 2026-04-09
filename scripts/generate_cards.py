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
HEATMAP_COLORS = ["#333333", "#2d1b4e", "#4c2882", "#7c3aed", "#c084fc"]


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
    with open(CONFIG_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key in ("name", "tagline"):
                config[key] = val
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


def fetch_repos():
    """Fetch repository data via GraphQL."""
    query = """
    query {
      viewer {
        repositories(first: 100, ownerAffiliations: OWNER, orderBy: {field: PUSHED_AT, direction: DESC}) {
          nodes {
            name
            pushedAt
            isPrivate
            languages(first: 5, orderBy: {field: SIZE, direction: DESC}) {
              edges {
                size
                node { name }
              }
            }
          }
        }
      }
    }
    """
    return gh_graphql(query)


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
        result = gh_graphql(query, owner=username, name=repo["name"], since=week_ago_str)
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
LANG_COLOR_FALLBACKS = ["#c084fc", "#f1e05a", "#3178c6", "#dea584", "#00ADD8"]


def lang_color(name, index):
    """Get a vibrant color for a language."""
    return LANG_COLORS.get(name, LANG_COLOR_FALLBACKS[index % len(LANG_COLOR_FALLBACKS)])


def render_language_bars(languages):
    """Generate SVG elements for language bar chart.

    Layout fits inside the LANGUAGES box (x=425..815, y=88..258).
    Names right-justified at x=530, bars from 535 to 805 (270px max).
    """
    lines = []
    max_bar_width = 370  # doubled from original 200 → fills most of the box
    label_x = 520   # right edge for right-justified names
    bar_x = 528
    pct_x = 805     # right-aligned percentage
    y_start = 130
    line_height = 22

    for i, (name, pct) in enumerate(languages):
        y = y_start + i * line_height
        bar_width = min(max(4, int(max_bar_width * pct / 100)), 265)
        color = lang_color(name, i)
        # Right-justified language name
        lines.append(
            f'  <text x="{label_x}" y="{y + 2}" text-anchor="end" '
            f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="13" fill="{color}" font-weight="bold">{name}</text>'
        )
        # Bar
        lines.append(
            f'  <rect x="{bar_x}" y="{y - 7}" width="{bar_width}" height="12" rx="2" fill="{color}" opacity="0.85"/>'
        )
        # Percentage right-aligned
        lines.append(
            f'  <text x="{pct_x}" y="{y + 2}" text-anchor="end" '
            f'font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="13" fill="{color}" font-weight="bold">{pct}%</text>'
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


# Emotion-hero color palette
EMOTION_COLORS = [
    "#87a99e",  # Serene - muted teal-green
    "#ad9387",  # Vibrant - soft terracotta
    "#919baf",  # Melancholy - muted slate blue
    "#a5a091",  # Curious - soft warm gray
    "#9ba591",  # Content - muted olive green
]

# Metaball blob centers in normalized [0,1] space
BLOB_CENTERS = [
    (0.25, 0.20),  # serene
    (0.75, 0.15),  # vibrant
    (0.50, 0.50),  # melancholy
    (0.20, 0.75),  # curious
    (0.80, 0.80),  # content
]

EMOTION_HERO_DIR = Path(os.environ.get(
    "EMOTION_HERO_DIR",
    str(ROOT.parent / "ascii" / "emotion-hero")
))


def _load_production_art() -> list[str]:
    """Load production ASCII art. Checks local content/ first, then emotion-hero."""
    # Local copy bundled in this repo
    local_art = ROOT / "content" / "art.txt"
    if local_art.exists():
        return local_art.read_text().rstrip("\n").split("\n")

    # Fall back to emotion-hero directories
    for search_dir in [EMOTION_HERO_DIR / "backend" / "content", EMOTION_HERO_DIR / "content"]:
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


def _metaball_color(nx: float, ny: float) -> str:
    """Compute blended color at (nx, ny) from all 5 metaball fields."""
    # Blob radii — different sizes create curved boundaries
    radii = [0.35, 0.25, 0.40, 0.30, 0.28]
    fields = []
    for i, (bx, by) in enumerate(BLOB_CENTERS):
        dx, dy = nx - bx, ny - by
        dist_sq = dx * dx + dy * dy + 0.0001
        # Metaball field: r² / d²
        field = (radii[i] ** 2) / dist_sq
        fields.append(field)

    total = sum(fields)
    if total < 0.001:
        return EMOTION_COLORS[0]

    # Weighted blend of all 5 colors
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


def render_ascii_hero() -> tuple[str, str, int]:
    """Render production ASCII art with metaball-style soft color blending.

    Returns (css_styles, svg_elements, total_height).
    Colors are computed per-character as a weighted blend from all 5 emotion
    blob centers, creating organic rounded regions with smooth transitions.
    """
    from html import escape as html_escape

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
    y_start = 355

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
        seg_color = _quantize_color(_metaball_color((leading - min_col) / col_range, ny))
        seg_chars: list[str] = []

        for ci, ch in enumerate(content):
            col = leading + ci
            nx = (col - min_col) / col_range
            qc = _quantize_color(_metaball_color(nx, ny))
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

    # Generate CSS: one class per quantized color + shared pulse animation
    css_parts = [
        "@keyframes hpulse {\n  0%, 100% { opacity: 0.75; }\n  50% { opacity: 1; }\n}"
    ]
    for qc in sorted(used_colors):
        cls = "c" + qc.lstrip("#")
        css_parts.append(
            f".{cls} {{ fill: {qc}; animation: hpulse 8s ease-in-out infinite; }}"
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
    y_start = 270
    for i, (name, desc) in enumerate(items):
        y = y_start + i * 20
        label = f"› {name}" if not desc else f"› {name} — {desc}"
        label = label[:48]
        lines.append(
            f'  <text x="16" y="{y}" font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="14.4" fill="#c084fc" font-weight="bold">{label}</text>'
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
    y_start = 270
    for i, (label, value) in enumerate(stats):
        y = y_start + i * 20
        lines.append(
            f'  <text x="815" y="{y}" text-anchor="end" font-family="\'TX-02\', \'Courier New\', Courier, monospace" '
            f'font-size="14.4" font-weight="bold">'
            f'<tspan fill="#7c3aed">{label}</tspan>'
            f'<tspan fill="#c084fc">{value}</tspan>'
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

    # Render unified card
    print("Rendering card.svg...")
    hero_css, ascii_hero_svg, hero_height = render_ascii_hero()
    card_height = 355 + hero_height + 30  # header/panels + hero + footer
    footer_y = card_height - 13

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
        footer_y=str(footer_y),
        hero_css=hero_css,
    )
    (ROOT / "card.svg").write_text(card)

    print("Done! Generated card.svg")


if __name__ == "__main__":
    main()
