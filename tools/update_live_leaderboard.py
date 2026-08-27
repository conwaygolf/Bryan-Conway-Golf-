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
  5. Anything else (i.e. a plain typed name, no GolfGenius URL/ID at all) --
     matched against Golf House Kentucky's own "Full Tournament Schedule"
     GolfGenius directory (customer_directory 3045 on golfgenius.com/pages/
     4492492 -- found by inspecting THAT page's embedded iframe rather than
     any of the category-specific sub-schedule pages it links to; this one
     directory covers every KGA event category -- Amateur Series, Women's,
     Senior, Men's Am, Qualifiers -- in one paginated list, so it's the
     right single source to search). Matching tries, in order: exact
     case-insensitive name match, substring either direction, then a
     shared-word-count fallback (>=2 words) -- e.g. an admin can type just
     "KGA Amateur Series #5" or "Kentucky Senior Open" and this resolves it
     to the real league_id with no GolfGenius link/ID ever needed. THIS IS
     THE INTENDED NORMAL PATH now -- an admin should just type the
     tournament's name into the LeaderBoard card and never touch a
     GolfGenius URL at all (confirmed 2026-08-27: don't ask for Bryan's own
     tournament code, resolving from the name alone works and is simpler).
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
import time
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

# Golf House Kentucky's own "Full Tournament Schedule" GolfGenius directory --
# one paginated list covering every KGA event category. See resolve_widget_url's
# name-match fallback (point 5 in the module docstring) for why this is the
# right single source to search against a plain typed tournament name.
KGA_SCHEDULE_DIRECTORY_URL = ("https://www.golfgenius.com/leagues/27340/v2_customer_directories/3045/"
                               "fetch_initial_data_for_directories?page_id=4492492&page={page}")
_schedule_cache = {"leagues": None, "fetched_at": 0}
SCHEDULE_CACHE_TTL = 3600  # the season schedule barely changes hour to hour


def fetch_kga_schedule_leagues():
    """{league_id: name} for every event on Golf House Kentucky's GolfGenius
    schedule, across all categories. Paginated -- keep fetching until
    noMoreData or a sane page cap (a full season fits in a handful of pages)."""
    cached = _schedule_cache["leagues"]
    if cached and time.time() - _schedule_cache["fetched_at"] < SCHEDULE_CACHE_TTL:
        return cached
    leagues = {}
    for page in range(1, 8):
        r = requests.get(KGA_SCHEDULE_DIRECTORY_URL.format(page=page), headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        for lid, league in data.get("leagues", {}).items():
            leagues[lid] = league.get("name", "").strip()
        if data.get("misc", {}).get("noMoreData"):
            break
    _schedule_cache["leagues"] = leagues
    _schedule_cache["fetched_at"] = time.time()
    return leagues


def find_league_by_name(name):
    """Best-effort match of a plain typed tournament name against the KGA
    schedule directory -- exact match, then substring either direction, then
    a shared-word-count fallback (>=2 meaningful words) to survive an admin
    typing e.g. "Senior Open" instead of "27th Kentucky Senior Open
    Championship". Returns (league_id, matched_name) or (None, None)."""
    leagues = fetch_kga_schedule_leagues()
    needle = name.strip().lower()
    if not needle:
        return None, None
    for lid, league_name in leagues.items():
        if league_name.lower() == needle:
            return lid, league_name
    for lid, league_name in leagues.items():
        lname = league_name.lower()
        if needle in lname or lname in needle:
            return lid, league_name
    needle_words = set(re.findall(r"[a-z0-9]+", needle))
    best_id, best_name, best_overlap = None, None, 0
    for lid, league_name in leagues.items():
        words = set(re.findall(r"[a-z0-9]+", league_name.lower()))
        overlap = len(needle_words & words)
        if overlap > best_overlap:
            best_id, best_name, best_overlap = lid, league_name, overlap
    if best_overlap >= 2:
        return best_id, best_name
    return None, None


def resolve_widget_url(code):
    """Returns (widget_url, err, matched_name). matched_name is only set
    when resolution went through the name-search fallback, so callers can
    use the real official tournament name as the event_label when an admin
    didn't also type a separate description."""
    code = code.strip()
    if not code:
        return None, "No tournament code set.", None

    if "golfgenius.com" in code and "/widgets/" in code:
        return code, None, None

    m = SHARE_LINK_RE.search(code)
    if m:
        league_id, round_id = m.groups()
        return (f"https://www.golfgenius.com/leagues/{league_id}/widgets/"
                f"tournament_results?no_header=true&round={round_id}&shared=false"), None, None

    if "golfgenius.com" in code:
        try:
            r = requests.get(code, headers=HEADERS, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            return None, f"Couldn't fetch the tournament code URL: {e}", None
        found = re.search(r"data-custom_src='([^']*golfgenius\.com/leagues/\d+/widgets/[^']+)'", r.text)
        if found:
            return found.group(1).replace("&amp;", "&"), None, None
        m2 = LEAGUE_ID_RE.search(code)
        if m2:
            return f"https://www.golfgenius.com/leagues/{m2.group(1)}/widgets/tournament_results?shared=false", None, None
        return None, "Fetched the tournament code page but couldn't find an embedded widget URL in it.", None

    if code.isdigit():
        return f"https://www.golfgenius.com/leagues/{code}/widgets/tournament_results?shared=false", None, None

    try:
        league_id, matched_name = find_league_by_name(code)
    except requests.RequestException as e:
        return None, f"Couldn't search the KGA schedule for '{code}': {e}", None
    if league_id:
        return (f"https://www.golfgenius.com/leagues/{league_id}/widgets/tournament_results?shared=false",
                None, matched_name)

    return None, (f"Couldn't find '{code}' in the KGA schedule, and it's not a golfgenius.com URL or "
                   f"league ID either -- check the spelling against kygolf.org's schedule."), None


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
            "aggregate_id": row.get("data-aggregate-id", ""),
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

    widget_url, err, matched_name = resolve_widget_url(config.get("tournament_code", ""))
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    # If the admin only typed a tournament name (no separate description),
    # use the real official name the schedule search matched against.
    label = config.get("description") or matched_name or None
    if err:
        write_result({"event_label": label, "venue": None,
                      "rows": [], "updated": now, "note": err})
        return 0

    try:
        event_ids = find_event_ids(widget_url)
    except requests.RequestException as e:
        write_result({"event_label": label, "venue": None,
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
            write_result({"event_label": label, "venue": None,
                          "rows": [], "updated": now,
                          "note": ("Found Bryan Conway's division but it's not a flat stroke-play "
                                   "leaderboard (likely a match-play bracket) -- this poller doesn't "
                                   "parse that shape yet, see live_match_tracker.py and hand-wire it "
                                   "the same way, or ask Claude to.")})
            return 0
        bryan = next((r for r in field if PLAYER_NAME in r["name"]), None)
        write_result({
            "event_label": label,
            "venue": bryan["city"] if bryan else None,
            "rows": top7_with_pinned_bryan(field),
            "updated": now,
            "note": None,
        })
        return 0

    write_result({"event_label": label, "venue": None,
                  "rows": [], "updated": now,
                  "note": "Couldn't find Bryan Conway in any division of this tournament code right now."})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
