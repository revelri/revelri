#!/usr/bin/env python3
"""Generate GitHub profile SVG cards from API data."""

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
CONFIG_PATH = ROOT / "config.yml"

# CRT color palette
HEATMAP_COLORS = ["#333333", "#1a4a2e", "#2d7a4a", "#4a9e7a", "#6bf1b6"]


def gh_api(endpoint, method="GET"):
    """Call GitHub API via gh CLI."""
    cmd = ["gh", "api", endpoint]
    if method != "GET":
        cmd.extend(["--method", method])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error calling {endpoint}: {result.stderr}", file=sys.stderr)
        return None
    return json.loads(result.stdout)


def gh_graphql(query, **variables):
    """Call GitHub GraphQL API via gh CLI."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, val in variables.items():
        cmd.extend(["-f", f"{key}={val}"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"GraphQL error: {result.stderr}", file=sys.stderr)
        return None
    return json.loads(result.stdout)


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
    """Fetch lines changed this week from active repos."""
    nodes = repos_data.get("data", {}).get("viewer", {}).get("repositories", {}).get("nodes", [])
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    week_ago_str = week_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    additions = 0
    deletions = 0

    # Only check repos pushed in the last week
    for repo in nodes:
        pushed = repo.get("pushedAt")
        if not pushed:
            continue
        dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
        if dt < week_ago:
            break  # Sorted by pushedAt desc

        repo_name = f"revelri/{repo['name']}"
        # Get recent commits by the user
        commits = gh_api(
            f"/repos/{repo_name}/commits?author={username}&since={week_ago_str}&per_page=100"
        )
        if not commits or not isinstance(commits, list):
            continue

        for commit in commits[:20]:  # Cap at 20 per repo to limit API calls
            sha = commit.get("sha")
            if sha:
                detail = gh_api(f"/repos/{repo_name}/commits/{sha}")
                if detail and "stats" in detail:
                    additions += detail["stats"].get("additions", 0)
                    deletions += detail["stats"].get("deletions", 0)

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
    current_streak = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for day in reversed(all_days):
        if day["date"] > today:
            continue
        if day["contributionCount"] > 0:
            current_streak += 1
        else:
            # Allow today to be zero (day isn't over yet)
            if day["date"] == today:
                continue
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

    # Sort by size, top 4 + other
    sorted_langs = sorted(lang_sizes.items(), key=lambda x: x[1], reverse=True)
    total = sum(s for _, s in sorted_langs)
    if total == 0:
        return [("None", 100)]

    result = []
    other = 0
    for i, (name, size) in enumerate(sorted_langs):
        pct = round(size / total * 100)
        if i < 3:
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
}
LANG_COLOR_FALLBACKS = ["#6bf1b6", "#f1e05a", "#3178c6", "#dea584", "#00ADD8"]


def lang_color(name, index):
    """Get a vibrant color for a language."""
    return LANG_COLORS.get(name, LANG_COLOR_FALLBACKS[index % len(LANG_COLOR_FALLBACKS)])


def render_language_bars(languages):
    """Generate SVG elements for language bar chart."""
    lines = []
    max_bar_width = 200
    bar_x = 434
    text_x = 644
    y_start = 108
    line_height = 22

    for i, (name, pct) in enumerate(languages):
        y = y_start + i * line_height
        bar_width = max(2, int(max_bar_width * pct / 100))
        color = lang_color(name, i)
        lines.append(
            f'  <rect x="{bar_x}" y="{y - 8}" width="{bar_width}" height="12" rx="2" fill="{color}" opacity="0.85"/>'
        )
        lines.append(
            f'  <text x="{text_x}" y="{y + 2}" font-family="\'Courier New\', Courier, monospace" font-size="12" fill="{color}">{name} {pct}%</text>'
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


# Emotion-hero heart ASCII art
HEART_ASCII = [
    "      ******       ******",
    "   **********   **********",
    "  ************ ************",
    " ***************************",
    "  *************************",
    "   ***********************",
    "     *******************",
    "       ***************",
    "         ***********",
    "           *******",
    "             ***",
    "              *",
]

# Emotion-hero color palette (adapted for CRT aesthetic)
EMOTION_COLORS = [
    "#87a99e",  # Serene - muted teal-green
    "#ad9387",  # Vibrant - soft terracotta
    "#919baf",  # Melancholy - muted slate blue
    "#a5a091",  # Curious - soft warm gray
    "#9ba591",  # Content - muted olive green
]


def render_ascii_hero():
    """Generate SVG elements for the ASCII heart hero with animated colors."""
    lines = []
    char_width = 9.6  # Courier New approximate character width at 16px
    font_size = 16
    line_height = 18
    # Center the heart horizontally in the 840px card
    max_line_len = max(len(row) for row in HEART_ASCII)
    total_width = max_line_len * char_width
    x_offset = (840 - total_width) / 2
    y_start = 280  # Below the info panes, within card bounds

    num_colors = len(EMOTION_COLORS)
    num_bands = 8  # Number of diagonal color bands across the heart

    for row_idx, row in enumerate(HEART_ASCII):
        y = y_start + row_idx * line_height

        # Group consecutive '*' characters by their diagonal band color
        i = 0
        while i < len(row):
            if row[i] == '*':
                # Find the run of '*' characters
                start = i
                while i < len(row) and row[i] == '*':
                    i += 1
                run_text = row[start:i]
                x = x_offset + start * char_width

                # Calculate diagonal band index for color assignment
                mid_col = (start + i) / 2
                band = int((mid_col + row_idx * 2) / max_line_len * num_bands) % num_colors
                base_color = EMOTION_COLORS[band]
                next_color = EMOTION_COLORS[(band + 1) % num_colors]
                third_color = EMOTION_COLORS[(band + 2) % num_colors]

                # Stagger animation timing based on position for wave effect
                delay = round((mid_col + row_idx * 3) / (max_line_len + len(HEART_ASCII) * 3) * 12, 1)

                lines.append(
                    f'  <text x="{x:.1f}" y="{y}" '
                    f'font-family="\'Courier New\', Courier, monospace" font-size="{font_size}" '
                    f'fill="{base_color}" opacity="0.85">'
                    f'{run_text}'
                    f'<animate attributeName="fill" '
                    f'values="{base_color};{next_color};{third_color};{base_color}" '
                    f'dur="12s" begin="{delay}s" repeatCount="indefinite"/>'
                    f'<animate attributeName="opacity" '
                    f'values="0.7;1;0.8;1;0.7" '
                    f'dur="8s" begin="{delay * 0.7:.1f}s" repeatCount="indefinite"/>'
                    f'</text>'
                )
            else:
                i += 1

    return "\n".join(lines)


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


def main():
    config = load_config()
    username = get_username()
    print(f"Generating cards for {username}...")

    # Fetch all data
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

    # Extract contribution stats
    viewer = contrib_data["data"]["viewer"]
    weekly = viewer["contributionsCollection"]
    yearly = viewer["yearCollection"]
    calendar = yearly["contributionCalendar"]

    weekly_commits = weekly["totalCommitContributions"] + weekly["restrictedContributionsCount"]
    weekly_prs = weekly["totalPullRequestContributions"]
    weekly_issues = weekly["totalIssueContributions"]
    total_contributions = calendar["totalContributions"]

    # Calculate derived stats
    current_streak, longest_streak = calc_streak(calendar)
    avg_per_day = round(weekly_commits / 7, 1)
    last_commit_ago = calc_last_commit_ago(repos_data)
    languages = aggregate_languages(repos_data)


    # Fetch lines changed (slower due to per-commit API calls)
    print("Fetching lines changed...")
    try:
        additions, deletions = fetch_lines_changed(repos_data, username)
        lines_added = f"+{additions:,}"
        lines_deleted = f"-{deletions:,}"
    except Exception as e:
        print(f"Warning: couldn't fetch lines changed: {e}", file=sys.stderr)
        lines_added = "+--"
        lines_deleted = "---"

    # Render unified card
    print("Rendering card.svg...")
    card = render_template(
        "card.svg.template",
        name=config["name"],
        heatmap_cells=render_heatmap(calendar),
        weekly_commits=str(weekly_commits),
        lines_added=lines_added,
        lines_deleted=lines_deleted,
        language_bars=render_language_bars(languages),
        ascii_hero=render_ascii_hero(),
        total_contributions=str(total_contributions),
        current_year=str(datetime.now(timezone.utc).year),
    )
    (ROOT / "card.svg").write_text(card)

    print("Done! Generated card.svg")


if __name__ == "__main__":
    main()
