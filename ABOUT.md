# revelri
[![Python](https://img.shields.io/badge/Python-3-blue)]()
[![GitHub Actions](https://img.shields.io/badge/CI-GitHub_Actions-blue)]()
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()

## How it works

`generate_cards.py` queries the GitHub GraphQL API via `gh`, filters repository-derived panels to public repositories before aggregation, and fills an SVG template. Rate-limit retry with exponential backoff. Falls back to cached mock data when offline.

**What it shows:**
- Public-repository commits and lines changed in the last seven days
- Most recent public-repository activity
- Language distribution across repos pushed in the last 90 days
- Recent public repositories and commit subjects
- Seven-day public-repository lines-of-code trend

## Setup

```bash
gh auth login
cp config.yml.example config.yml   # edit name, tagline, featured repos
python scripts/generate_cards.py
```

Add the GitHub Actions workflow to auto-update on push and every 30 minutes.

**Stack:** Python 3, Playwright (APNG rendering), GitHub GraphQL API, SVG, CSS animations.
