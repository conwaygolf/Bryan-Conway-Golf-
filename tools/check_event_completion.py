"""
Daily check: has any Schedule event's end date passed without a result
being fetched yet? If so, try to auto-find Bryan Conway's final result on
GolfGenius and drop a DRAFT into the admin panel for review -- this never
auto-publishes to the live Results section. If auto-fetch fails or finds
nothing, a draft still gets created with a placeholder note, so the
reminder always happens even when the scrape doesn't.

Run daily via Windows Task Scheduler (see run_check_event_completion.bat).
Real precedent for why this stays best-effort + human-reviewed rather than
fully autonomous: earlier sessions hand-tracking specific tournaments hit
a wrong-event mixup (a same-named Maine golf association page) and a
parsing bug that silently dropped a hole from a scorecard. Auto-publishing
without a human glancing at it first risks the same class of mistake
going live unnoticed.
"""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent.parent
DATA_DIR = HERE / "data"
SCHEDULE_FILE = DATA_DIR / "schedule.json"
DRAFTS_FILE = DATA_DIR / "result_drafts.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
PLAYER_NAME = "Bryan Conway"


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _keywords(text):
    return set(re.findall(r"[a-z]{4,}", text.lower()))


def _try_results_page(page_url):
    """Given a GolfGenius page believed to hold a live/final leaderboard,
    follow widget -> event-id -> v2tournaments and look for the player.
    Returns a result dict or None."""
    try:
        pr = requests.get(page_url, headers=HEADERS, timeout=15)
        pr.raise_for_status()
    except requests.RequestException:
        return None

    widget_urls = re.findall(r"golfgenius\.com/leagues/\d+/widgets/tournament_results[^\"'\s]*", pr.text)
    for widget_url in widget_urls:
        if not widget_url.startswith("http"):
            widget_url = "https://www." + widget_url
        try:
            wr = requests.get(widget_url, headers=HEADERS, timeout=15)
            wr.raise_for_status()
        except requests.RequestException:
            continue

        event_ids = sorted(set(re.findall(r'data-tournament-event-id="(\d+)"', wr.text)))
        for event_id in event_ids:
            try:
                tr = requests.get(
                    f"https://www.golfgenius.com/v2tournaments/{event_id}?player_stats_for_portal=true",
                    headers=HEADERS, timeout=15,
                )
                tr.raise_for_status()
            except requests.RequestException:
                continue
            if PLAYER_NAME not in tr.text:
                continue

            tsoup = BeautifulSoup(tr.text, "html.parser")
            for row in tsoup.find_all("tr", class_="aggregate-row"):
                name_link = row.find("a", class_="open-aggregate-details")
                if not name_link or PLAYER_NAME not in name_link.get_text():
                    continue
                pos = row.find("td", class_="pos")
                score = row.find("td", class_="score")
                return {
                    "pos": pos.get_text(strip=True) if pos else "",
                    "score": score.get_text(strip=True) if score else "",
                    "source_page": page_url,
                }
    return None


def find_golfgenius_result(event_title):
    """Best-effort search for Bryan Conway's final result at a KGA event,
    given just its title. Returns {"pos", "score", "source_page"} or None.

    Two strategies, tried in order:

    1. kygolf.org's homepage features live/recent events as a "row" block
       pairing the event name (a `.title-alt` span) with "Tee Times &
       Pairings" / "Leaderboard" buttons in a sibling column of the same
       row -- reverse-engineered 2026-08-20 by inspecting the real markup
       for the 27th Kentucky Senior Open, still showing there 2 days after
       it ended. This is the precise, reliable path when it applies, but
       only major/featured events seem to get this homepage treatment, and
       only for a limited window after the event.
    2. Fall back to generic golfgenius.com/pages/ links anywhere on the
       homepage whose visible text overlaps the event title, then look on
       each candidate page for an embedded tournament_results widget
       directly (works when the info page IS the results page, unlike the
       Senior Open where they're separate pages).

    No stable search API exists for any of this -- a None return is the
    normal/expected outcome for many events, not necessarily a bug.
    """
    try:
        r = requests.get("https://www.kygolf.org/", headers=HEADERS, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    keywords = _keywords(event_title)

    # Strategy 1: featured homepage row with a direct Leaderboard button.
    best_row, best_overlap = None, 0
    for title_span in soup.find_all(class_="title-alt"):
        overlap = len(keywords & _keywords(title_span.get_text(" ", strip=True)))
        if overlap > best_overlap:
            row = title_span.find_parent(class_="row")
            if row:
                best_row, best_overlap = row, overlap
    if best_row:
        lb_link = best_row.find("a", string=lambda s: s and s.strip() in ("Leaderboard", "Live Scoring", "Results", "Final Results"))
        if lb_link and lb_link.get("href"):
            result = _try_results_page(lb_link["href"])
            if result:
                return result

    # Strategy 2: generic link-text matching + check each page directly.
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "golfgenius.com/pages/" not in href:
            continue
        overlap = len(keywords & _keywords(a.get_text(" ", strip=True)))
        if overlap:
            candidates.append((overlap, href))
    candidates.sort(key=lambda c: -c[0])

    for _, page_url in candidates[:3]:
        result = _try_results_page(page_url)
        if result:
            return result
    return None


def build_draft(event):
    try:
        found = find_golfgenius_result(event["title"])
    except Exception:
        found = None

    if found:
        score_word = "Even" if found["score"] == "E" else found["score"]
        note = f"Conway finished {found['pos']} at {score_word}. Auto-filled -- verify before publishing."
        auto_filled = True
    else:
        note = "Auto-fetch couldn't find a result on GolfGenius -- check kygolf.org and fill this in by hand."
        auto_filled = False

    return {
        "date": event["date_label"],
        "title": event["title"],
        "note": note,
        "tag": None,
        "source_event_title": event["title"],
        "auto_filled": auto_filled,
    }


def git_publish(paths, message):
    try:
        subprocess.run(["git", "add", *[str(p) for p in paths]], cwd=HERE, check=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE)
        if staged.returncode == 0:
            return
        subprocess.run(
            ["git", "-c", "user.email=admin@bryanconwaygolf.com", "-c", "user.name=ConwayGolf Admin",
             "commit", "-m", message],
            cwd=HERE, check=True,
        )
        subprocess.run(["git", "push", "origin", "main"], cwd=HERE, check=True)
        subprocess.run(["git", "push", "bryan", "main"], cwd=HERE, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[check_event_completion] git publish failed (exit {e.returncode})")


def send_notification(event, auto_filled):
    try:
        sys.path.insert(0, str(Path.home() / ".claude" / "secrets"))
        import send_email  # type: ignore
        subject = f"ConwayGolf: {event['title']} result ready for review"
        body = (
            f"{event['title']} ended on {event['end_date']}.\n\n"
            + ("A draft result was auto-filled from GolfGenius and is waiting in the admin panel.\n"
               if auto_filled else
               "Auto-fetch couldn't find a result -- a blank draft is waiting in the admin panel to fill in by hand.\n")
            + "\nReview and publish at: https://conwaygolf.onrender.com/admin"
        )
        send_email.send("frostbytehero@gmail.com", subject, body, from_name="ConwayGolf Site")
    except Exception as e:
        print(f"[check_event_completion] notification email failed: {type(e).__name__}: {e}")


def main():
    schedule = load_json(SCHEDULE_FILE, [])
    drafts = load_json(DRAFTS_FILE, [])
    today = date.today()
    changed = False

    for event in schedule:
        if event.get("result_checked"):
            continue
        try:
            end_dt = date.fromisoformat(event["end_date"])
        except (KeyError, ValueError):
            continue
        if end_dt >= today:
            continue  # not over yet

        print(f"Event ended: {event['title']} ({event['end_date']}) -- building draft...")
        draft = build_draft(event)
        drafts.append(draft)
        event["result_checked"] = True
        changed = True
        send_notification(event, draft["auto_filled"])

    if changed:
        save_json(SCHEDULE_FILE, schedule)
        save_json(DRAFTS_FILE, drafts)
        git_publish([SCHEDULE_FILE, DRAFTS_FILE], "Auto: draft result(s) for ended event(s)")
        print("Done -- draft(s) saved and pushed.")
    else:
        print("No newly-ended events.")


if __name__ == "__main__":
    main()
