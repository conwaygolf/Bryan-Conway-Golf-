"""
Polls Bryan's standing in the Kentucky Golf Association's John C. Owens
Player of the Year race (Men's Overall -- every KGA event counts toward it
as of the 2026 scoring changes). Separate from the Tom Musselman Award
(Men's Senior) table already hardcoded in index.html.

Real, verified, curl-scrapable source: the KGA's GolfGenius page
(golfgenius.com/pages/5885301) embeds a season_points_v2 widget for
league 78666 -- same widget shape as the Brewer flight poller
(tools/update_brewer_standing.py), just a different league/page id.

Run daily via Windows Task Scheduler ("ConwayGolf POY Standing Poller").
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent.parent
DATA_PATH = BASE_DIR / "data" / "poy_season_standing.json"

WIDGET_URL = (
    "https://www.golfgenius.com/leagues/78666"
    "/widgets/season_points_v2?page_id=5926400&shared=false"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

ROW_RE = re.compile(
    r"rank_display_value[^>]*>(\d+)</span>.*?player_name\"[^>]*>([^<]+)</a>"
    r".*?</td>\s*<td class='text-center'>\s*(\d+)\s*</td>\s*<td class='text-center'>\s*(\d+)\s*</td>"
    r"\s*<td class='text-right' id='total_points_[0-9]+'>\s*([\d,\.]+)",
    re.S,
)


def fetch_standings():
    r = requests.get(WIDGET_URL, headers=HEADERS, timeout=10)
    r.raise_for_status()
    field = []
    for pos, name, events, wins, points in ROW_RE.findall(r.text):
        field.append({
            "pos": pos,
            "name": name.strip(),
            "events": events,
            "wins": wins,
            "points": points,
        })
    return field


def top7_with_pinned_bryan(field):
    rows = field[:7]
    if rows and not any("Conway" in r["name"] for r in rows):
        bryan = next((r for r in field if "Conway" in r["name"]), None)
        if bryan:
            rows.append({**bryan, "pinned": True})
    return rows


def git_publish(paths, message):
    rel = [str(p.relative_to(BASE_DIR)) for p in paths]
    subprocess.run(["git", "add", *rel], cwd=BASE_DIR, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
    if diff.returncode == 0:
        return False  # nothing changed
    subprocess.run(["git", "commit", "-m", message], cwd=BASE_DIR, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True)
    return True


def main():
    field = fetch_standings()
    if not field:
        print("No rows parsed -- widget markup may have changed.", file=sys.stderr)
        return 1
    data = {
        "league": "Kentucky Golf Association",
        "category": "John C. Owens POY (Men's Overall)",
        "field": field,
        "top7": top7_with_pinned_bryan(field),
    }
    old = json.loads(DATA_PATH.read_text(encoding="utf-8")) if DATA_PATH.exists() else None
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if old != data:
        bryan = next((r for r in field if "Conway" in r["name"]), None)
        note = f"Bryan: pos {bryan['pos']}, {bryan['points']} pts" if bryan else "Bryan not found in field"
        published = git_publish([DATA_PATH], f"Auto-update: John C. Owens POY standings ({note})")
        print("Updated + published:" if published else "Updated (no publish needed):", note)
    else:
        print("No change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
