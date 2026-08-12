"""
Polls Golf House Kentucky's GolfGenius bracket for Bryan Conway's current
match and updates the site's live-banner when something real changes.

How this actually works (reverse-engineered 2026-08-12 -- read before touching):

1. Find the tournament's GolfGenius "Tournament Results" widget page on
   kygolf.org (a page like golfgenius.com/pages/XXXXXXX). curl it directly
   with a normal browser User-Agent -- Playwright/headless browsers get a
   flat 403 from GolfGenius's bot detection, plain curl/requests does not.
2. That widget page embeds an iframe pointing at
   golfgenius.com/leagues/<league_id>/widgets/tournament_results -- fetch
   THAT directly. It's still mostly a JS-rendered shell, but it embeds
   `data-tournament-event-id="..."` attributes (usually one per
   division/flight) that are NOT JS-rendered -- grep those out of the raw
   HTML.
3. For each event-id, fetch golfgenius.com/v2tournaments/<event-id>
   ?player_stats_for_portal=true -- THIS is real, mostly-server-rendered
   bracket HTML. Whichever event-id's page actually contains the player's
   name is the right division; don't hardcode the id, tournaments/divisions
   get new ids every event.
4. Parse with BeautifulSoup. Each player's name is inside
   `<a class="aggregate_bracket_match" data-disable-with="Full Name">`.
   Its `div.match` ancestor contains both combatants (`div.top`, `div.bottom`
   -- these "winner"/"loser" CSS classes are just bracket-position labels,
   NOT the actual live result, ignore them). Each combatant's own
   `div.status_or_affiliation` text is their result from the PRECEDING
   round (e.g. the Quarterfinals column shows each player's Round-of-16
   margin) -- NOT that round's own outcome. `div.match` also has an
   `in-match-spacing-text` with the tee time.
5. The one genuinely live signal is the literal substring "THRU" (e.g.
   "1 up THRU 6") -- match-play notation only ever uses THRU for a match
   still being played; a finished match reads "3 & 2" (clinched early) or
   a bare "1 up"/"2 up" (finished all holes) -- both have no THRU.
   Bonus/quirk: GolfGenius previews an in-progress semifinal's live score
   inside the *next* round's bracket slot (i.e. the Finals column can
   show a live "THRU" score for a semifinal that hasn't finished yet).
   So: don't trust which column a THRU score sits in -- just scan every
   match_div containing the tracked player's name, in every column, for
   any THRU text anywhere in that match_div, and treat that as the live
   score if found.
6. Advancement/elimination is structural, not textual: find the highest
   round-column index where the player's name appears. If their name also
   appears in the next column, they won and advanced (regardless of what
   text is showing). If the next column exists but has different names in
   that bracket slot, they lost. If there's no next column, the round
   they're in is the final.

Run this on a schedule (Windows Task Scheduler, every 10 min while a
tournament the player is in is live). Update WIDGET_URL / PLAYER_NAME /
STOP_DATE below per-tournament -- there will be more of these.
"""
import json
import re
import sys
import subprocess
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent.parent
STATE_FILE = HERE / ".tmp" / "live_match_state.json"
TEMPLATE_FILE = HERE / "templates" / "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# --- per-tournament config: update these for each new event/code ----------
WIDGET_URL = "https://www.golfgenius.com/leagues/510355/widgets/tournament_results?shared=false"
PLAYER_NAME = "Bryan Conway"
VENUE = "Owensboro Country Club"
STOP_DATE = date(2026, 8, 12)  # script no-ops (and should be disabled) after this date
# ---------------------------------------------------------------------------


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


def fetch_bracket(event_id):
    url = f"https://www.golfgenius.com/v2tournaments/{event_id}?player_stats_for_portal=true"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text


def find_player_bracket_html(player_name):
    for event_id in find_event_ids():
        try:
            html = fetch_bracket(event_id)
        except requests.RequestException:
            continue
        if player_name in html:
            return html
    return None


def clean_name(name):
    # KGA prefixes some names with a status letter (e.g. "G Davis Boland")
    # that isn't part of how anyone actually refers to the player.
    return re.sub(r"^[A-Z]\s+(?=[A-Z][a-z])", "", name or "")


def analyze(html, player_name):
    """Returns a dict describing the player's current tournament status."""
    soup = BeautifulSoup(html, "html.parser")
    columns = soup.find_all("div", class_="column")

    def round_label(col):
        el = col.find("div", class_="round_name")
        return el.get_text(strip=True) if el else "Bracket"

    def match_divs_in(col):
        return col.find_all("div", class_="match")

    def combatants(match_div):
        # Slots that have officially advanced use an <a class=
        # aggregate_bracket_match> link for the name; PREVIEW slots (a
        # projected finalist while their semifinal is still live) use
        # plain text in the same position instead -- fall back to that.
        out = []
        for slot in match_div.find_all("div", recursive=False):
            name_box = slot.find("div", class_="text_box")
            if not name_box:
                continue
            link = name_box.find("a", class_="aggregate_bracket_match")
            name = link.get("data-disable-with", link.get_text(strip=True)) if link \
                else name_box.get_text(strip=True)
            if not name or name.lower() == "bye":
                continue
            status_el = slot.find("div", class_="status_or_affiliation")
            status = status_el.get_text(strip=True) if status_el else ""
            out.append({"name": name, "status": status})
        tee_time_el = match_div.find("div", class_="in-match-spacing-text")
        tee_time = tee_time_el.get_text(strip=True) if tee_time_el else None
        return out, tee_time

    # 1. structural progress: highest column index containing the player.
    # Do this FIRST because the live THRU score for the player's own match
    # is often NOT in a match_div that names the player at all -- GolfGenius
    # previews an in-progress semifinal's score inside the *next* round's
    # bracket slot, paired against whoever wins the OTHER semifinal (a name
    # that has nothing to do with the player). So: find the player's real
    # opponent structurally first, then search the whole page for THAT
    # opponent's name showing a THRU status, wherever it happens to sit.
    last_idx = None
    for i, col in enumerate(columns):
        for md in match_divs_in(col):
            names = [a.get("data-disable-with", "") for a in md.find_all("a", class_="aggregate_bracket_match")]
            if player_name in names:
                last_idx = i

    if last_idx is None:
        return {"found": False}

    current_round = round_label(columns[last_idx])
    people, tee_time = combatants(match_divs_in(columns[last_idx])[
        next(i for i, md in enumerate(match_divs_in(columns[last_idx]))
             if player_name in [a.get("data-disable-with", "") for a in md.find_all("a", class_="aggregate_bracket_match")])
    ])
    me = next(p for p in people if p["name"] == player_name)
    opp = next((p for p in people if p["name"] != player_name), None)

    # 2. live (THRU) score: search every match_div on the page for the
    # opponent's name, wherever it sits, and take their status if it's a
    # live (THRU) reading -- this is what actually catches the preview-slot
    # quirk described above.
    live = None
    if opp:
        for col in columns:
            for md in match_divs_in(col):
                people2, tee_time2 = combatants(md)
                for p in people2:
                    if p["name"] == opp["name"] and "THRU" in p["status"].upper():
                        live = {
                            "leader": p["name"],
                            "leader_status": p["status"],
                            "opponent": player_name,
                            "tee_time": tee_time2,
                            "round": current_round,
                        }

    advanced = False
    eliminated = False
    if last_idx + 1 < len(columns):
        next_names = set()
        for md in match_divs_in(columns[last_idx + 1]):
            next_names.update(a.get("data-disable-with", "") for a in md.find_all("a", class_="aggregate_bracket_match"))
        if player_name in next_names:
            advanced = True
        elif next_names:
            eliminated = True

    if live:
        live["leader"] = clean_name(live["leader"])

    return {
        "found": True,
        "round": current_round,
        "opponent": clean_name(opp["name"]) if opp else None,
        "prior_round_margin": me["status"],  # this round-column's carryover text, NOT this round's own result
        "live": live,
        "advanced": advanced,
        "eliminated": eliminated,
        "is_last_column": last_idx == len(columns) - 1,
    }


def build_banner_html(info):
    if info.get("live"):
        lv = info["live"]
        status = lv["leader_status"]
        # status is always the OPPONENT's own self-referential reading
        # (e.g. "1 up THRU 6" or "2 down THRU 10") -- "up" means the
        # opponent leads, "down" means the tracked player leads.
        if re.search(r"\bdown\b", status, re.I):
            detail = (f'{lv["round"]} &mdash; <strong>{PLAYER_NAME}</strong> leads '
                       f'{lv["leader"]} {status.replace("down", "up")}, {VENUE}')
        elif "all square" in status.lower() or status.strip().upper().startswith("AS"):
            detail = f'{lv["round"]} &mdash; <strong>{PLAYER_NAME}</strong> vs {lv["leader"]}, {status}, {VENUE}'
        else:
            detail = (f'{lv["round"]} &mdash; {lv["leader"]} leads '
                       f'<strong>{PLAYER_NAME}</strong> {status}, {VENUE}')
        return '<span class="live-dot"></span>\n  <span class="live-label">Live</span>\n  ' \
               f'<span class="live-detail">{detail}</span>'
    if info.get("eliminated"):
        detail = (f'<strong>{PLAYER_NAME}</strong> falls to {info["opponent"]} '
                   f'{info["prior_round_margin"]} in the {info["round"]}')
        return '<span class="live-dot result"></span>\n  <span class="live-label">Result</span>\n  ' \
               f'<span class="live-detail">{detail}</span>'
    if info.get("advanced"):
        detail = (f'<strong>{PLAYER_NAME}</strong> defeats {info["opponent"]} '
                   f'{info["prior_round_margin"]} &mdash; advances')
        return '<span class="live-dot result"></span>\n  <span class="live-label">Result</span>\n  ' \
               f'<span class="live-detail">{detail}</span>'
    return None


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

    html = find_player_bracket_html(PLAYER_NAME)
    if not html:
        print(f"Could not find {PLAYER_NAME} in any division bracket right now.")
        return

    info = analyze(html, PLAYER_NAME)
    if not info.get("found"):
        print(f"{PLAYER_NAME} not found in the bracket structure.")
        return

    state = load_state()
    fingerprint = json.dumps(info, sort_keys=True)
    if state.get("fingerprint") == fingerprint:
        print("No change since last check.")
        return

    banner = build_banner_html(info)
    if not banner:
        print("Found player but nothing bannerable yet (no live/advanced/eliminated signal).")
        return

    changed = update_banner(banner)
    state["fingerprint"] = fingerprint
    save_state(state)

    if not changed:
        print("Computed banner matches current file content, nothing to commit.")
        return

    git("add", "templates/index.html")
    git("commit", "-m", "Auto-update live banner")
    git("push", "origin", "main")
    git("push", "bryan", "main")
    print("Banner updated and pushed to both remotes:", banner)


if __name__ == "__main__":
    main()
