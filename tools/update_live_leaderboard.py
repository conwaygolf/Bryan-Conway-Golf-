"""
Admin-driven live tournament leaderboard poller.

Unlike the season-standing pollers (update_brewer_standing.py,
update_poy_standing.py -- fixed widget URLs, run weekly) this one is
config-driven: an admin turns it on and enters a tournament_code (and
optional description) via the "LeaderBoard" card in /admin, and this script
turns that into a real GolfGenius URL each run. See data/leaderboard_config.json
(written by admin_leaderboard_save() in app.py) for the current settings.

Self-gates immediately if disabled or no code is set, so it's cheap to run
this often (every 15 min via Task Scheduler, "ConwayGolf Live Leaderboard
Poller") even when nothing is live.

--- Turning tournament_code into a widget URL ---
Accepts, in order of preference:
  1. A full golfgenius.com URL already pointing at a /widgets/... endpoint
     -- used as-is.
  2. A golfgenius.com "share link" of the form leagues/<league_id>/lb.<round_id>
     (what share-cdn.golfgenius.com/live links to) -- rewritten to
     leagues/<league_id>/widgets/tournament_results?round=<round_id>&shared=false.
  3. Any other golfgenius.com page (e.g. a kygolf.org-linked pages/XXXXX page)
     -- fetched and scanned for an embedded iframe's data-custom_src widget URL.
  4. A bare numeric ID -- treated as a league_id:
     leagues/<id>/widgets/tournament_results?shared=false.
If none of these apply, the code can't be resolved; a note is written to
live_leaderboard.json explaining that instead of crashing, so it shows up
next time an admin/Claude checks in.

--- Finding Bryan Conway's division and scores ---
Same discovery steps as tools/live_senior_open_tracker.py: pull every
data-tournament-event-id out of the widget HTML, then curl
v2tournaments/<event_id>?player_stats_for_portal=true for each until one
contains "Bryan Conway". THIS SCRIPT ONLY HANDLES STROKE-PLAY LEADERBOARDS
(tr.aggregate-row rows -- pos/name/score/thru, a flat table). Match-play
brackets are a structurally different page (see live_match_tracker.py) and
are NOT auto-parsed here -- if the found division looks like a bracket
instead of a flat leaderboard, a note is written asking for that to be
hand-wired the way live_match_tracker.py was, rather than guessing.

--- Bryan's GolfGenius member ID ---
If Jimmy ever gives us Bryan's personal GolfGenius member/player ID (distinct
from a tournament code -- it's a cross-event ID, e.g. seen as
data-member-card-id="2247513" on the John C. Owens POY widget), prefer
matching rows by that id over the "Bryan Conway" name string: it's immune to
name collisions and works even if GolfGenius ever renders his name
differently. Wire it as an optional BRYAN_MEMBER_ID env var / config value
and check `row.get("member_id") == BRYAN_MEMBER_ID` first, falling back to
the name match below when not set. Not implemented yet since we don't have
the id on hand -- this paragraph is the reminder to do it when we do.

Run via Windows Task Scheduler ("ConwayGolf Live Leaderboard Poller"),
every 15 min, always Enabled (it no-ops fast when turned off in /admin --
never disable the task itself to pause this, same rule as every other
poller in this project).
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "data" / "leaderboard_config.json"
DATA_PATH = BASE_DIR / "data" / "live_leaderboard.json"

PLAYER_NAME = "Bryan Conway"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

SHARE_LINK_RE = re.compile(r"leagues/(\d+)/lb\.(\d+)")
LEAGUE_ID_RE = re.compile(r"leagues/(\d+)")


def resolve_widget_url(code):
    code = code.strip()
    if not code:
        return None, "No tournament code set."

    if "golfgenius.com" in code and "/widgets/" in code:
        return code, None

    m = SHARE_LINK_RE.search(code)
    if m:
        league_id, round_id = m.groups()
        return (f"https://www.golfgenius.com/leagues/{league_id}/widgets/"
                f"tournament_results?no_header=true&round={round_id}&shared=false"), None

    if "golfgenius.com" in code:
        try:
            r = requests.get(code, headers=HEADERS, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            return None, f"Couldn't fetch the tournament code URL: {e}"
        found = re.search(r"data-custom_src='([^']*golfgenius\.com/leagues/\d+/widgets/[^']+)'", r.text)
        if found:
            return found.group(1).replace("&amp;", "&"), None
        m2 = LEAGUE_ID_RE.search(code)
        if m2:
            return f"https://www.golfgenius.com/leagues/{m2.group(1)}/widgets/tournament_results?shared=false", None
        return None, "Fetched the tournament code page but couldn't find an embedded widget URL in it."

    if code.isdigit():
        return f"https://www.golfgenius.com/leagues/{code}/widgets/tournament_results?shared=false", None

    return None, f"Couldn't make sense of tournament code '{code}' -- expected a golfgenius.com URL or a bare league ID."


def find_event_ids(widget_url):
    r = requests.get(widget_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return sorted(set(re.findall(r'data-tournament-event-id="(\d+)"', r.text)))


def fetch_event_html(event_id):
    url = f"https://www.golfgenius.com/v2tournaments/{event_id}?player_stats_for_portal=true"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text


def parse_stroke_play_field(html):
    """Every row of a flat stroke-play leaderboard table, or [] if this
    page isn't that shape (e.g. it's a match-play bracket instead)."""
    soup = BeautifulSoup(html, "html.parser")
    field = []
    for row in soup.find_all("tr", class_="aggregate-row"):
        name_link = row.find("a", class_="open-aggregate-details")
        if not name_link:
            continue
        pos = row.find("td", class_="pos")
        score = row.find("td", class_="score")
        thru = row.find("td", class_="past_round_thru") or row.find("td", class_="thru")
        affiliation = row.find("div", class_="affiliation")
        field.append({
            "pos": pos.get_text(strip=True) if pos else "",
            "name": name_link.get_text(strip=True),
            "score": score.get_text(strip=True) if score else "",
            "thru": (thru.get_text(" ", strip=True) if thru else "").replace("*", "").strip(),
            "city": affiliation.get_text(strip=True) if affiliation else "",
        })
    return field


def top7_with_pinned_bryan(field):
    rows = field[:7]
    if rows and not any(PLAYER_NAME in r["name"] for r in rows):
        bryan = next((r for r in field if PLAYER_NAME in r["name"]), None)
        if bryan:
            rows.append({**bryan, "pinned": True})
    return rows


def git_publish(paths, message):
    rel = [str(p.relative_to(BASE_DIR)) for p in paths]
    subprocess.run(["git", "add", *rel], cwd=BASE_DIR, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
    if diff.returncode == 0:
        return False
    subprocess.run(["git", "commit", "-m", message], cwd=BASE_DIR, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True)
    subprocess.run(["git", "push", "bryan", "main"], cwd=BASE_DIR, check=False)
    return True


def write_result(data):
    old = json.loads(DATA_PATH.read_text(encoding="utf-8")) if DATA_PATH.exists() else None
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if old == data:
        print("No change.")
        return
    published = git_publish([DATA_PATH], "Auto-update: live leaderboard")
    print("Updated + published:" if published else "Updated (no publish needed):", data.get("note") or data.get("event_label"))


def main():
    if not CONFIG_PATH.exists():
        print("No leaderboard_config.json yet -- nothing to do.")
        return 0
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not config.get("enabled"):
        print("Leaderboard disabled in /admin. Skipping.")
        return 0

    widget_url, err = resolve_widget_url(config.get("tournament_code", ""))
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    if err:
        write_result({"event_label": config.get("description") or None, "venue": None,
                      "rows": [], "updated": now, "note": err})
        return 0

    try:
        event_ids = find_event_ids(widget_url)
    except requests.RequestException as e:
        write_result({"event_label": config.get("description") or None, "venue": None,
                       "rows": [], "updated": now, "note": f"Couldn't fetch widget: {e}"})
        return 0

    for event_id in event_ids:
        try:
            html = fetch_event_html(event_id)
        except requests.RequestException:
            continue
        if PLAYER_NAME not in html:
            continue
        field = parse_stroke_play_field(html)
        if not field:
            write_result({"event_label": config.get("description") or None, "venue": None,
                          "rows": [], "updated": now,
                          "note": ("Found Bryan Conway's division but it's not a flat stroke-play "
                                   "leaderboard (likely a match-play bracket) -- this poller doesn't "
                                   "parse that shape yet, see live_match_tracker.py and hand-wire it "
                                   "the same way, or ask Claude to.")})
            return 0
        bryan = next((r for r in field if PLAYER_NAME in r["name"]), None)
        write_result({
            "event_label": config.get("description") or None,
            "venue": bryan["city"] if bryan else None,
            "rows": top7_with_pinned_bryan(field),
            "updated": now,
            "note": None,
        })
        return 0

    write_result({"event_label": config.get("description") or None, "venue": None,
                  "rows": [], "updated": now,
                  "note": "Couldn't find Bryan Conway in any division of this tournament code right now."})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
