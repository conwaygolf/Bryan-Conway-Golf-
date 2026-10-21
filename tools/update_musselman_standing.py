"""
Polls Bryan's standing in the Kentucky Golf Association's Tom Musselman
Award (Men's Senior POY race). This table used to be hand-typed directly
into index.html with a note saying it was "confirmed unscrapable" --
that was wrong. Found 2026-10-21: it's the SAME season_points_v2 widget
as the John C. Owens poller (tools/update_poy_standing.py), league 78666,
page_id 5926400 -- the KGA's GolfGenius page just lets a human flip a
"Category" dropdown to switch between POY races, and that dropdown is a
flat point_category_id query param under the hood (11433 = Owens,
11444 = Musselman), not a separate page or an unscrapable JS app.

Run daily via Windows Task Scheduler, same as the Owens poller.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent.parent
DATA_PATH = BASE_DIR / "data" / "musselman_season_standing.json"

WIDGET_URL = (
    "https://www.golfgenius.com/leagues/78666"
    "/widgets/season_points_v2?page_id=5926400&shared=false"
    "&point_category_id=11444"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Same shape as update_poy_standing.py's ROW_RE, plus the City column --
# the Owens poller never needed it since its own site table drops the
# City column, but the existing (now-replaced) hardcoded Musselman table
# had one, so this keeps it.
ROW_RE = re.compile(
    r"rank_display_value[^>]*>(\d+)</span>.*?player_name\"[^>]*>([^<]+)</a>"
    r".*?class='City'>\s*([^<]*?)\s*</td>"
    r".*?</td>\s*<td class='text-center'>\s*(\d+)\s*</td>\s*<td class='text-center'>\s*(\d+)\s*</td>"
    r"\s*<td class='text-right' id='total_points_[0-9]+'>\s*([\d,\.]+)",
    re.S,
)


def fetch_standings():
    r = requests.get(WIDGET_URL, headers=HEADERS, timeout=10)
    r.raise_for_status()
    field = []
    for pos, name, city, events, wins, points in ROW_RE.findall(r.text):
        field.append({
            "pos": pos,
            "name": name.strip(),
            "city": city.strip().title(),
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
    # HEAD:main + try both remotes independently -- see
    # update_live_leaderboard.py's git_publish for the full explanation.
    for remote in ("origin", "bryan"):
        try:
            subprocess.run(["git", "push", remote, "HEAD:main"], cwd=BASE_DIR, check=True)
        except subprocess.CalledProcessError:
            print(f"[update_musselman_standing] push to {remote} rejected (likely diverged history) -- continuing")
    return True


def main():
    field = fetch_standings()
    if not field:
        print("No rows parsed -- widget markup may have changed.", file=sys.stderr)
        return 1
    data = {
        "league": "Kentucky Golf Association",
        "category": "Tom Musselman POY (Men's Senior)",
        "field": field,
        "top7": top7_with_pinned_bryan(field),
    }
    old = json.loads(DATA_PATH.read_text(encoding="utf-8")) if DATA_PATH.exists() else None
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if old != data:
        bryan = next((r for r in field if "Conway" in r["name"]), None)
        note = f"Bryan: pos {bryan['pos']}, {bryan['points']} pts" if bryan else "Bryan not found in field"
        published = git_publish([DATA_PATH], f"Auto-update: Tom Musselman POY standings ({note})")
        print("Updated + published:" if published else "Updated (no publish needed):", note)
    else:
        print("No change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
