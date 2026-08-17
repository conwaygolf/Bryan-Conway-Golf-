"""
Polls the real live GolfGenius leaderboard for the 27th Kentucky Senior
Open (stroke play, Country Club of Paducah) and updates the site's
live-banner when Bryan Conway's position/score changes.

Different tournament format than tools/live_match_tracker.py (that one
parses a match-play bracket) -- this is a stroke-play leaderboard, a
different HTML shape entirely. Reverse-engineered 2026-08-17:

1. The tournament's live leaderboard hub is
   https://share-cdn.golfgenius.com/live (Golf Genius's own public
   directory of every live event it's currently hosting) -- found this
   event's row there via its `data-link` attribute:
   golfgenius.com/leagues/511281/lb.1575855
2. That page embeds an iframe at
   golfgenius.com/leagues/511281/widgets/tournament_results?no_header=true&round=1575855&shared=false
   -- curl that directly (works fine, unlike the match-play widget this
   does NOT need a real browser). It embeds one
   data-tournament-event-id="..." per division (8 for this event: Senior
   Division, Super-Senior Overall, Senior/Super-Senior Amateurs and
   Professionals split out, plus two KPGA POY-points divisions).
3. For each event-id, curl
   golfgenius.com/v2tournaments/<event-id>?player_stats_for_portal=true
   -- real server-rendered HTML (no JS needed, unlike v2 tournament
   pages for some other event types). Whichever event-id's page
   contains "Bryan Conway" is the right one -- don't hardcode it, ids
   are per-tournament.
4. Parse with BeautifulSoup. Each player is one
   `<tr class="aggregate-row ...">` with real, simple structure:
     td.pos                  -> position, e.g. "T1"
     td.name .player a       -> name; .affiliation -> hometown
     td.colored.score        -> current total to-par, e.g. "E", "+2", "-3"
     td.past_round_thru      -> holes played so far today (a bare number,
                                 or a number + "*" meaning still in progress)
   No THRU/bracket weirdness like match play -- this is a flat
   leaderboard table, much simpler to parse.

Run this on a schedule (Windows Task Scheduler, every 10 min while the
tournament is live -- Aug 17-18, 2026). Update PLAYER_NAME / VENUE /
STOP_DATE below for future stroke-play events; the league/round ids
will be different every time, rediscover them via share-cdn.golfgenius.com/live
rather than assuming these same numbers still work.
"""
import json
import re
import subprocess
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent.parent
STATE_FILE = HERE / ".tmp" / "live_senior_open_state.json"
TEMPLATE_FILE = HERE / "templates" / "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# --- per-tournament config ---------------------------------------------
WIDGET_URL = "https://www.golfgenius.com/leagues/511281/widgets/tournament_results?no_header=true&round=1575855&shared=false"
PLAYER_NAME = "Bryan Conway"
VENUE = "Country Club of Paducah"
EVENT_LABEL = "27th Kentucky Senior Open"
STOP_DATE = date(2026, 8, 21)  # script no-ops (and should be disabled) after this date -- kept up a few extra days past the Aug 17-18 event per Jimmy's request, since it "looks great"
# -------------------------------------------------------------------------


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def find_event_ids():
    r = requests.get(WIDGET_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return sorted(set(re.findall(r'data-tournament-event-id="(\d+)"', r.text)))


def fetch_leaderboard(event_id):
    url = f"https://www.golfgenius.com/v2tournaments/{event_id}?player_stats_for_portal=true"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text


def find_player_row(player_name):
    """Returns (event_id, row_dict) for the first division containing the player."""
    for event_id in find_event_ids():
        try:
            html = fetch_leaderboard(event_id)
        except requests.RequestException:
            continue
        if player_name not in html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        name_link = soup.find("a", class_="open-aggregate-details", string=lambda s: s and player_name in s)
        if not name_link:
            continue
        row = name_link.find_parent("tr")
        if not row:
            continue
        pos = row.find("td", class_="pos")
        score = row.find("td", class_="score")
        thru = row.find("td", class_="past_round_thru")
        affiliation = row.find("div", class_="affiliation")
        return event_id, {
            "pos": pos.get_text(strip=True) if pos else "",
            "score": score.get_text(strip=True) if score else "",
            "thru": thru.get_text(" ", strip=True) if thru else "",
            "affiliation": affiliation.get_text(strip=True) if affiliation else "",
        }
    return None, None


def build_banner_html(info):
    pos = info["pos"]
    score = info["score"]
    thru = info["thru"].replace("*", "").strip()

    if not thru or thru == "-":
        return None  # hasn't teed off yet, nothing worth showing

    score_word = "Even" if score == "E" else score
    detail = (f'{EVENT_LABEL} &mdash; <strong>{PLAYER_NAME}</strong> {pos}, '
              f'{score_word}, Thru {thru} &middot; {VENUE}')
    return '<span class="live-dot"></span>\n  <span class="live-label">Live</span>\n  ' \
           f'<span class="live-detail">{detail}</span>'


def update_banner(inner_html):
    text = TEMPLATE_FILE.read_text(encoding="utf-8")
    new_text = re.sub(
        r'(<div class="live-banner">\s*)(.*?)(\s*</div>)',
        lambda m: m.group(1) + inner_html + m.group(3),
        text, count=1, flags=re.S,
    )
    if new_text == text:
        return False
    TEMPLATE_FILE.write_text(new_text, encoding="utf-8")
    return True


def git(*args, cwd=HERE):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def main():
    if date.today() > STOP_DATE:
        print(f"Past stop date {STOP_DATE}, doing nothing. Disable/remove this task.")
        return

    event_id, info = find_player_row(PLAYER_NAME)
    if not info:
        print(f"Could not find {PLAYER_NAME} in any division leaderboard right now.")
        return

    state = load_state()
    fingerprint = json.dumps(info, sort_keys=True)
    if state.get("fingerprint") == fingerprint:
        print(f"No change since last check ({info}).")
        return

    banner = build_banner_html(info)
    if not banner:
        print(f"Found player (event {event_id}) but not teed off yet: {info}")
        state["fingerprint"] = fingerprint
        save_state(state)
        return

    changed = update_banner(banner)
    state["fingerprint"] = fingerprint
    save_state(state)

    if not changed:
        print("Computed banner matches current file content, nothing to commit.")
        return

    git("add", "templates/index.html")
    git("commit", "-m", "Auto-update live Senior Open leaderboard banner")
    git("push", "origin", "main")
    git("push", "bryan", "main")
    print("Banner updated and pushed to both remotes:", info)


if __name__ == "__main__":
    main()
