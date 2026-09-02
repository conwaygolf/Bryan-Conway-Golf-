import io
import json
import os
import re
import smtplib
import subprocess
import threading
import time
import unicodedata
import uuid
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, flash, g, get_flashed_messages, redirect, render_template, request, jsonify, session, url_for
from PIL import Image, ImageFilter, ImageOps
from werkzeug.middleware.proxy_fix import ProxyFix

import analytics

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "conwaygolf-admin-dev-key")
# Render sits behind a proxy -- without this, request.remote_addr would be
# Render's internal load-balancer IP for every visitor, not the real one,
# which would break analytics' geo lookup entirely.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB upload cap

# Self-hosted analytics (see analytics.py's own docstring for the full
# rationale/tradeoffs) -- only the real content pages count as a "pageview",
# not admin, the scorecard API, or static assets.
ANALYTICS_TRACKED_PATHS = {"/", "/roots", "/gallery", "/sponsors", "/press-archives"}


@app.before_request
def _track_pageview():
    if request.method != "GET" or request.path not in ANALYTICS_TRACKED_PATHS:
        return
    vid = request.cookies.get(analytics.VISITOR_COOKIE)
    g.new_visitor = not vid
    g.visitor_id = vid or uuid.uuid4().hex
    analytics.track(
        path=request.path,
        referrer_url=request.referrer,
        own_host=request.host,
        user_agent_string=request.headers.get("User-Agent", ""),
        ip=request.remote_addr,
        visitor_id=g.visitor_id,
    )


@app.after_request
def _set_visitor_cookie(response):
    if getattr(g, "new_visitor", False):
        response.set_cookie(analytics.VISITOR_COOKIE, g.visitor_id,
                             max_age=60 * 60 * 24 * 365 * 2, httponly=True, samesite="Lax")
    return response


GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
CONTACT_TO = "info@bryanconwaygolf.com"

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
IMAGES_DIR = BASE_DIR / "static" / "images"

# --- Admin auth + auto-publish -------------------------------------------
# Single shared password, no user accounts -- fine for now since it's just
# Jimmy/Bryan. MUST be set via CONWAYGOLF_ADMIN_PASSWORD env var (local .env
# or a Render env var) -- deliberately NO hardcoded fallback here. This repo
# is public (conwaygolf/Bryan-Conway-Golf-), so any default baked into this
# file would be readable by anyone on github.com -- that happened once
# already (BCGadmin2026!, now rotated) and cost a real security scare.
ADMIN_PASSWORD = os.getenv("CONWAYGOLF_ADMIN_PASSWORD")

# Every successful admin save commits + pushes to `origin` so Render's
# auto-deploy publishes it -- that's the whole point of the admin page (no
# manual git step). GITHUB_PUSH_TOKEN (a repo-scoped PAT) is only required
# when this is running on Render itself, where there's no cached git login;
# locally, Jimmy's own git credentials are used instead (token stays unset).
# Set ADMIN_AUTO_PUBLISH=0 to save files locally without pushing (e.g. while
# testing) -- defaults on.
GITHUB_PUSH_TOKEN = os.getenv("GITHUB_PUSH_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "jstout5/ConwayGolf-")
GIT_PUBLISH_ENABLED = os.getenv("ADMIN_AUTO_PUBLISH", "1") != "0"

# Admin requests and the two background pollers (leaderboard, analytics
# rollup) all call git_publish() from separate threads in the same process.
# Without this lock, two concurrent git operations on the same working tree
# can interleave (or one's push can land between the other's commit and
# push, rejecting it) -- the loser's change was a real local commit that
# never reached origin, and got silently wiped by the next Render restart
# even though it briefly looked correct in /admin (same in-memory state).
_git_publish_lock = threading.Lock()


def git_publish(paths, message):
    """Best-effort commit + push of the given paths. Never raises -- an
    admin upload should still succeed locally even if the publish step
    fails (no token configured yet, no network, etc.)."""
    if not GIT_PUBLISH_ENABLED:
        return
    with _git_publish_lock:
        _git_publish_locked(paths, message)


def _git_publish_locked(paths, message):
    if GITHUB_PUSH_TOKEN:
        remote = f"https://x-access-token:{GITHUB_PUSH_TOKEN}@github.com/{GITHUB_REPO}.git"
    else:
        remote = "origin"
    try:
        subprocess.run(["git", "add", *[str(p) for p in paths]], cwd=BASE_DIR, check=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
        if staged.returncode == 0:
            return  # nothing actually changed
        subprocess.run(
            ["git", "-c", "user.email=admin@bryanconwaygolf.com", "-c", "user.name=ConwayGolf Admin",
             "commit", "-m", message],
            cwd=BASE_DIR, check=True,
        )
        push = subprocess.run(["git", "push", remote, "HEAD:main"], cwd=BASE_DIR)
        if push.returncode != 0:
            # Remote moved since we last fetched (another dyno, Jimmy's own
            # machine, or -- pre-lock -- a same-process race). Rebase our
            # commit on top and retry once rather than stranding it locally.
            subprocess.run(["git", "fetch", remote, "main"], cwd=BASE_DIR, check=True)
            rebase = subprocess.run(["git", "rebase", "FETCH_HEAD"], cwd=BASE_DIR)
            if rebase.returncode != 0:
                # A real content conflict (rare -- two admin edits to the
                # same JSON file at once). Abort so the working tree isn't
                # left stuck mid-rebase, which would break every future
                # publish until someone fixes it by hand.
                subprocess.run(["git", "rebase", "--abort"], cwd=BASE_DIR)
                print("[admin publish] rebase conflict, change not published -- aborted cleanly")
                return
            subprocess.run(["git", "push", remote, "HEAD:main"], cwd=BASE_DIR, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[admin publish] git command failed (exit {e.returncode})")
    except Exception as e:
        print(f"[admin publish] git publish failed: {type(e).__name__}")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

# Press & Archives -- add new entries here as more archive cards are created.
# era must be one of: early-years, franklin-county, college, professional-years,
# the-comeback, 2026 (matches the filter pills in press_archives.html).
DEFAULT_ARCHIVE_CARDS = [
    {
        "id": "1995-khsaa-state-title",
        "title": "Conway Coasts to Five-Shot Victory",
        "source": "The State Journal",
        "reporter": "Mike Folta",
        "date": "July 16, 1995",
        "era": "franklin-county",
        "summary": "Conway closed out his high school golf career with a final-round 68 to win the KHSAA Class AAA championship by five shots at Bowling Green Country Club, dedicating the win to his late brother Ben. He signed to play golf at Morehead State the following year.",
        "stats": "72-hole score: 213 (72-73-68)  ·  Margin: 5 shots  ·  KHSAA Class AAA Champion",
        "image": "press_1995_khsaa_state_journal.png",
        "original_url": None,
        "hidden": False,
    },
    {
        "id": "college-morehead-spotlight",
        "title": "A Tradition of Excellence — Morehead State Eagle Golf",
        "source": "Morehead State Eagle Golf",
        "reporter": None,
        "date": "1994–1996",
        "era": "college",
        "summary": "Conway earned OVC Freshman of the Year honors in 1994, then First Team All-Ohio Valley Conference in 1995 — the same year he won the Kentucky Amateur and the Daniel Boone Invitational — before being named team MVP and transferring to the University of Louisville.",
        "stats": "1994: OVC Freshman of the Year, 3rd at OVC Championship  ·  1995: First Team All-OVC, KY Amateur Champion, Daniel Boone Champion  ·  Team MVP",
        "image": "press_college_morehead_spotlight.png",
        "original_url": None,
        "hidden": False,
    },
    {
        "id": "2001-qschool-qualify",
        "title": "Five Qualify at Canadian Q-School",
        "source": "NBC Sports — The Sports Network",
        "reporter": None,
        "date": "September 26, 2001",
        "era": "professional-years",
        "summary": "Bryan Conway finished T-3 at 285 (-3) in the final stage of Canadian Tour Qualifying School at Lora Bay Golf Club, earning full Canadian Tour playing status for the 2002 season.",
        "stats": "72-hole score: 285 (-3)  ·  Finish: T-3  ·  Qualified for full 2002 Canadian Tour status",
        "image": "press_2001_nbc_sports_qschool_qualify.png",
        "original_url": None,
        "hidden": False,
    },
    {
        "id": "2004-qschool-round2",
        "title": "Americans Share Canadian Q-School Lead",
        "source": "NBC Sports — The Sports Network",
        "reporter": "Marty Henwood",
        "date": "February 4, 2004",
        "era": "professional-years",
        "summary": "Conway fired a 4-under 68 to share the lead with Scott Stiles after two rounds of Canadian Q-School's final stage, reaching 8-under 136 at Dufferin Heights GC.",
        "stats": "36-hole score: 136 (-8)  ·  Position after Round 2: T-1 (co-leader)",
        "image": "press_2004_nbc_sports_qschool_round2.png",
        "original_url": None,
        "hidden": False,
    },
    {
        "id": "2004-qschool-lead",
        "title": "Conway Leads Canadian Tour Q-School",
        "source": "Golf Channel",
        "reporter": "Marty Henwood",
        "date": "February 5, 2004",
        "era": "professional-years",
        "summary": "Conway carried a one-shot lead into the final round of Canadian Tour Q-School at Dufferin Heights GC, posting rounds of 69-67-70 to reach 10-under 206.",
        "stats": "54-hole score: 206 (-10)  ·  Position entering final round: 1st (leader)",
        "image": "press_2004_golf_channel_qschool_lead.png",
        "original_url": None,
        "hidden": False,
    },
    {
        "id": "2003-pga-tour-qualifying",
        "title": "Conway Has Sights Set on PGA Tour",
        "source": "The State Journal",
        "reporter": None,
        "date": "October 29, 2003",
        "era": "professional-years",
        "summary": "Conway finished third in the first stage of PGA Tour qualifying in Ft. Lauderdale, shooting 71-68-68-64 to advance to the second stage in St. Augustine — five years after turning pro and playing the Canadian Tour.",
        "stats": "First Stage score: 271  ·  Finish: 3rd  ·  Advanced to Second Stage (St. Augustine, Nov. 19–22, 2003)",
        "image": "press_2003_state_journal_pga_qualifying.png",
        "original_url": None,
        "hidden": False,
    },
    {
        "id": "2018-usga-midam",
        "title": "38th U.S. Mid-Amateur Championship Field",
        "source": "United States Golf Association",
        "reporter": None,
        "date": "September 22–27, 2018",
        "era": "the-comeback",
        "summary": "Conway earned a place in the field for the 38th U.S. Mid-Amateur Championship, contested at Charlotte Country Club and Carolina Golf Club — competing for the Robert T. Jones Jr. Memorial Trophy against the country's top amateur golfers.",
        "stats": "Charlotte Country Club & Carolina Golf Club, Charlotte, N.C.  ·  Sept. 22–27, 2018",
        "image": "press_2018_usga_midam_field.png",
        "original_url": None,
        "hidden": False,
    },
    {
        "id": "2019-chichi-rodriguez",
        "title": "The One and Only Chi Chi Rodriguez",
        "source": "Bryan Conway",
        "reporter": None,
        "date": "March 14, 2019",
        "era": "the-comeback",
        "summary": "Out practicing one morning, Conway ran into World Golf Hall of Famer Chi Chi Rodriguez -- an eight-time PGA TOUR winner and one of the game's most beloved legends.",
        "stats": "Chi Chi Rodriguez, 1935–2024  ·  World Golf Hall of Fame",
        "image": "press_2019_chichi_rodriguez.png",
        "original_url": None,
        "hidden": False,
    },
]

# Photo Gallery, Sponsors, and Hero config are admin-editable (see /admin
# routes below) so they live in JSON files under data/, not hardcoded here.
# These are the seed values used the first time each JSON file is created.
GALLERY_JSON = DATA_DIR / "gallery.json"
SPONSORS_JSON = DATA_DIR / "sponsors.json"
HERO_JSON = DATA_DIR / "hero.json"
RESULTS_JSON = DATA_DIR / "results.json"
SCHEDULE_JSON = DATA_DIR / "schedule.json"
ARCHIVE_CARDS_JSON = DATA_DIR / "archive_cards.json"
POY_STANDING_JSON = DATA_DIR / "poy_season_standing.json"
RESULT_DRAFTS_JSON = DATA_DIR / "result_drafts.json"
PRESS_HEADER_JSON = DATA_DIR / "press_header.json"
LEADERBOARD_CONFIG_JSON = DATA_DIR / "leaderboard_config.json"
LIVE_LEADERBOARD_JSON = DATA_DIR / "live_leaderboard.json"
ANALYTICS_DAILY_JSON = DATA_DIR / "analytics_daily.json"

DEFAULT_GALLERY_PHOTOS = [
    {"image": "gallery_swing_1.jpg", "caption": "Full extension off the tee."},
    {"image": "gallery_swing_2.jpg", "caption": "Eyes on the ball flight."},
    {"image": "gallery_portrait_1.jpg", "caption": "Between shots, framed up by the flowers."},
    {"image": "gallery_portrait_2.jpg", "caption": "Locked in mid-round."},
    {"image": "action_swing.png", "caption": "Follow-through."},
    {"image": "action_swing_2.jpg", "caption": "Watching it land."},
    {"image": "hero.jpg", "caption": "2026 Lexington Senior City Championship."},
]

DEFAULT_SPONSORS = [
    {
        "name": "Brushy Creek Outfitters",
        "blurb": "A proud supporter of Bryan Conway Golf.",
        "url": "https://brushycreekoutfitters.com",
        "logo": "brushy_creek_logo.png",
    },
    {
        "name": "Whitetail Heaven Outfitters",
        "blurb": "A proud supporter of Bryan Conway Golf.",
        "url": "https://whitetailheavenoutfitters.com",
        "logo": "whitetail_logo.png",
    },
]

DEFAULT_RESULTS = [
    {
        "date": "Aug 17–18, 2026",
        "title": "27th Kentucky Senior Open",
        "note": "Country Club of Paducah — a KPGA partner event on the KGA calendar. Conway finished T3 at +3.",
        "tag": "Low Amateur",
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "Player of the Year Contender",
        "note": "Conway's 2026 performance has placed him prominently in multiple Kentucky PGA Player of the Year races, including the senior standings, while also competing among players of all ages.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "Kentucky Senior Match Play — Semifinalist",
        "note": "Won his opening match at the 15th Clark's Pump-N-Shop Kentucky Match Play Championship, Owensboro Country Club, 6&5, then beat David Horning 3&2 in the Quarterfinals before falling to G. Davis Boland 3&2 in the Senior Semifinals — a season of deep championship runs against Kentucky's top senior competition.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "Kentucky Men's Senior Stroke Play",
        "note": "Won the Kentucky Golf Association Men's Senior Stroke Play Championship, July 27–28, at Bardstown Country Club — another statewide title more than 30 years after winning the Kentucky Amateur.",
        "tag": "Champion",
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "The Resurgence",
        "note": "More than three decades after his first state championships, Conway has again emerged as one of Kentucky's most competitive golfers. At 51 years old, he's turned the 2026 season into another championship chapter in a career that began at the highest levels of Kentucky golf.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "2025",
        "title": "Kentucky Men's Match Play — Senior Division Qualifying Medalist",
        "note": "Led the Senior Division qualifying round at the 14th Clark's Pump-N-Shop Kentucky Men's Match Play Championship, Kearney Hill Golf Links — three-under 69 with an eagle on hole 3 and birdies on 9 and 11.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "2×",
        "title": "Daniel Boone Invitational Champion",
        "note": "Two-time Daniel Boone Invitational Champion (1995 & 2018) — a title with a Conway family connection, as father Walter Conway won the same championship in 1968 during his own competitive career.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "5×",
        "title": "Frankfort City Champion",
        "note": "Five-time Frankfort City Champion, adding victories in one of the region's longstanding competitive golf events.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "Pro",
        "title": "The Professional Years — Canadian Tour",
        "note": "Turned professional and pursued tournament golf at the highest level, competing through multiple trips to Canadian Tour Qualifying School as he pursued his professional career — the next chapter in a progression from Kentucky high school champion to decorated collegiate golfer, Kentucky Amateur champion and professional competitor.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "UofL",
        "title": "University of Louisville — Collegiate Career",
        "note": "Transferred to the University of Louisville, continuing his collegiate career at another Division I program.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "1995",
        "title": "Kentucky Amateur",
        "note": "Captured the Kentucky Amateur Championship at Kearney Hill Links, representing The Players Club, with a score of 281 — one of the signature victories of his career, won while still competing collegiately.",
        "tag": "Champion",
        "hidden": False,
    },
    {
        "date": "94/95",
        "title": "Morehead State University — Collegiate Career",
        "note": "Began his collegiate career at Morehead State and quickly established himself as one of the Ohio Valley Conference's top young golfers — named Freshman Player of the Year and finished third in the OVC Championship, then earned Most Valuable Player as a sophomore and First Team All-OVC honors in 1995.",
        "tag": None,
        "hidden": False,
    },
    {
        "date": "1992",
        "title": "Back-to-Back KHSAA State Champion",
        "note": "Franklin County returned to the top the following season, giving Conway back-to-back state championships and cementing one of the defining chapters of his early career.",
        "tag": "Champion",
        "hidden": False,
    },
    {
        "date": "1992",
        "title": "Kentucky High School Regional Champion",
        "note": "Won his high school Regional Championship, advancing to the KHSAA State Championship where Franklin County repeated as state champions.",
        "tag": "Champion",
        "hidden": False,
    },
    {
        "date": "1991",
        "title": "KHSAA State Champion",
        "note": "Helped lead Franklin County to a Kentucky high school state golf championship, establishing himself as one of the state's premier young players.",
        "tag": "Champion",
        "hidden": False,
    },
]

# Real dates (not just display labels) so the daily event-completion checker
# (tools/check_event_completion.py) knows when an event has actually ended.
DEFAULT_SCHEDULE = [
    {
        "date_label": "Sep 28–29",
        "start_date": "2026-09-28",
        "end_date": "2026-09-29",
        "title": "Clark's Pump-N-Shop Kentucky Men's Senior Amateur",
        "note": "Frankfort Country Club — Conway's home county.",
        "hidden": False,
        "result_checked": False,
    },
]

DEFAULT_HERO = {
    "image": "action_swing.png",
    "object_position": "50% 6%",
    "object_position_mobile": "50% 12%",
    "lb_corner": "top-right",
}

# Designed banner graphic (title/tagline baked into the image itself), not
# a photo with overlay text -- rendered at a fixed 1983:793 aspect ratio via
# CSS object-fit:cover, so no crop/overlay-position analysis needed here
# unlike the hero image.
DEFAULT_PRESS_HEADER = {"image": "press_archives_header.jpg"}

# Admin-toggled live leaderboard (hero top-left, opposite the Facebook card).
# tools/update_live_leaderboard.py reads tournament_code/enabled from this
# file and writes its polled results to LIVE_LEADERBOARD_JSON separately --
# see that script's header for how a tournament_code is turned into real data.
DEFAULT_LEADERBOARD_CONFIG = {"enabled": False, "tournament_code": "", "description": ""}
DEFAULT_LIVE_LEADERBOARD = {"event_label": None, "venue": None, "rows": [], "updated": None, "note": None}


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    save_json(path, default)
    return json.loads(json.dumps(default))  # deep copy


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


GALLERY_PHOTOS = load_json(GALLERY_JSON, DEFAULT_GALLERY_PHOTOS)
SPONSORS = load_json(SPONSORS_JSON, DEFAULT_SPONSORS)
HERO = load_json(HERO_JSON, DEFAULT_HERO)
POY_STANDING = load_json(POY_STANDING_JSON, {"league": None, "category": None, "field": [], "top7": []})
RESULTS = load_json(RESULTS_JSON, DEFAULT_RESULTS)
SCHEDULE = load_json(SCHEDULE_JSON, DEFAULT_SCHEDULE)
ARCHIVE_CARDS = load_json(ARCHIVE_CARDS_JSON, DEFAULT_ARCHIVE_CARDS)
LEADERBOARD_CONFIG = load_json(LEADERBOARD_CONFIG_JSON, DEFAULT_LEADERBOARD_CONFIG)
LIVE_LEADERBOARD = load_json(LIVE_LEADERBOARD_JSON, DEFAULT_LIVE_LEADERBOARD)
RESULT_DRAFTS = load_json(RESULT_DRAFTS_JSON, [])
PRESS_HEADER = load_json(PRESS_HEADER_JSON, DEFAULT_PRESS_HEADER)


# tools/update_live_leaderboard.py runs via a Windows Scheduled Task on
# Jimmy's own PC, not on Render -- if his PC is off, the poller simply
# doesn't fire and live_leaderboard.json stops updating, but the site stays
# up serving whatever was last deployed. Without a check like this, that
# looks exactly like a real live score to a visitor (a frozen "LIVE" badge
# showing hours-old numbers) instead of an obvious outage. Compare against
# real wall-clock time on every request (not deploy time) so this catches
# the gap even though nothing has redeployed since the outage started.
LEADERBOARD_STALE_AFTER_MINUTES = 30


def leaderboard_staleness_minutes():
    """Minutes since the leaderboard was last actually polled, or None if
    it's never been polled at all."""
    updated = LIVE_LEADERBOARD.get("updated")
    if not updated:
        return None
    try:
        ts = datetime.fromisoformat(updated)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60


def public_live_leaderboard():
    """What the public homepage shows -- same as LIVE_LEADERBOARD, but with
    rows cleared if the poller has gone quiet too long, so the site quietly
    falls back to the Facebook-only layout instead of displaying frozen,
    possibly-wrong scores as if they were live."""
    staleness = leaderboard_staleness_minutes()
    if staleness is not None and staleness > LEADERBOARD_STALE_AFTER_MINUTES:
        return {**LIVE_LEADERBOARD, "rows": []}
    return LIVE_LEADERBOARD


# Server-side leaderboard poller, running in-process instead of relying only
# on the Windows Scheduled Task on Jimmy's PC ("ConwayGolf Live Leaderboard
# Poller"). That task still exists and still runs, but it's a single point
# of failure -- confirmed 2026-08-27 when Jimmy shut his PC down to go play
# and the leaderboard froze on stale scores for ~6.5 hours before anyone
# noticed. This background thread does the exact same poll/publish cycle,
# but lives on Render itself, so it keeps running whether or not the PC is
# on. Reuses tools/update_live_leaderboard.py's resolution/parsing
# functions (single source of truth for "how a tournament_code becomes real
# scores") but publishes through THIS file's own git_publish() -- that
# script's own git_publish() assumes cached local git credentials and would
# fail on a fresh Render container with no cached login; this app's
# git_publish() already knows how to push via GITHUB_PUSH_TOKEN, the same
# mechanism the admin auto-publish routes already rely on.
#
# Caveat, worth checking if this doesn't seem to be running: if this
# service is on Render's free tier, Render can spin the whole container
# down after ~15 min with no inbound web traffic, which would pause this
# thread too until the next visitor wakes it back up. A paid plan (no
# idle spin-down) makes this fully reliable; confirm the plan in Render's
# dashboard if live updates seem to lag on a quiet tournament day.
from tools.update_live_leaderboard import (  # noqa: E402
    resolve_widget_url, find_event_ids, fetch_event_html,
    parse_stroke_play_field, top7_with_pinned_bryan, PLAYER_NAME,
)

LEADERBOARD_POLL_INTERVAL_SECONDS = 600  # 10 min, matches the Task Scheduler cadence


def poll_live_leaderboard_once():
    global LIVE_LEADERBOARD
    if not LEADERBOARD_CONFIG.get("enabled"):
        return
    widget_url, err, matched_name = resolve_widget_url(LEADERBOARD_CONFIG.get("tournament_code", ""))
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    label = LEADERBOARD_CONFIG.get("description") or matched_name or None
    result = None

    if err:
        result = {"event_label": label, "venue": None, "rows": [], "updated": now, "note": err}
    else:
        try:
            event_ids = find_event_ids(widget_url)
        except requests.RequestException as e:
            result = {"event_label": label, "venue": None, "rows": [], "updated": now,
                      "note": f"Couldn't fetch widget: {e}"}
        else:
            for event_id in event_ids:
                try:
                    html = fetch_event_html(event_id)
                except requests.RequestException:
                    continue
                if PLAYER_NAME not in html:
                    continue
                field = parse_stroke_play_field(html)
                if not field:
                    result = {"event_label": label, "venue": None, "rows": [], "updated": now,
                              "note": ("Found Bryan Conway's division but it's not a flat stroke-play "
                                       "leaderboard (likely a match-play bracket) -- needs a hand-port, "
                                       "see live_match_tracker.py.")}
                else:
                    bryan = next((r for r in field if PLAYER_NAME in r["name"]), None)
                    result = {"event_label": label, "venue": bryan["city"] if bryan else None,
                              "rows": top7_with_pinned_bryan(field), "updated": now, "note": None}
                break
            if result is None:
                result = {"event_label": label, "venue": None, "rows": [], "updated": now,
                          "note": "Couldn't find Bryan Conway in any division of this tournament code right now."}

    if result != LIVE_LEADERBOARD:
        LIVE_LEADERBOARD = result
        save_json(LIVE_LEADERBOARD_JSON, LIVE_LEADERBOARD)
        git_publish([LIVE_LEADERBOARD_JSON], "Auto-update: live leaderboard (server-side poller)")


def _leaderboard_poll_loop():
    while True:
        try:
            poll_live_leaderboard_once()
        except Exception as e:
            print(f"[leaderboard poller] error: {type(e).__name__}: {e}")
        try:
            time.sleep(LEADERBOARD_POLL_INTERVAL_SECONDS)
        except Exception:
            pass


_leaderboard_thread = None


def _spawn_leaderboard_thread():
    global _leaderboard_thread
    _leaderboard_thread = threading.Thread(target=_leaderboard_poll_loop, daemon=True)
    _leaderboard_thread.start()


def _leaderboard_watchdog():
    # Real incident, 2026-08-27: the poller thread died from something its
    # own try/except didn't catch and just never came back -- a plain
    # background thread has no supervisor, so a dead thread stays dead
    # until the next full process restart. That produced a real 64.7-hour
    # outage during a live tournament before anyone noticed. Rather than
    # trying to enumerate every possible failure mode, just check every 5
    # min whether the thread is still alive and restart it if not -- cheap
    # insurance against whatever the next unknown failure turns out to be.
    while True:
        time.sleep(300)
        if _leaderboard_thread is None or not _leaderboard_thread.is_alive():
            print("[leaderboard poller] watchdog: thread was dead, restarting")
            _spawn_leaderboard_thread()


def start_leaderboard_poller():
    # Avoid a double-start under Flask's debug-mode reloader, which forks a
    # watcher parent process in addition to the real running one -- only
    # start in the actual running process. Under gunicorn (Render), app.debug
    # is always False, so this never skips in production.
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    _spawn_leaderboard_thread()
    threading.Thread(target=_leaderboard_watchdog, daemon=True).start()


start_leaderboard_poller()


# Analytics daily rollup -- same background-thread pattern as the leaderboard
# poller above, just on a longer interval since this only needs to survive a
# redeploy with minimal lost granularity, not track something changing by the
# minute. See analytics.py's docstring for why this doesn't git-commit every
# single pageview.
ANALYTICS_ROLLUP_INTERVAL_SECONDS = 4 * 3600  # 4 hours -- was 15 min, cut down to reduce how often this collides with admin publishes


def _analytics_rollup_loop():
    while True:
        try:
            analytics.rollup_today(load_json, save_json, git_publish, ANALYTICS_DAILY_JSON)
        except Exception as e:
            print(f"[analytics rollup] error: {type(e).__name__}: {e}")
        time.sleep(ANALYTICS_ROLLUP_INTERVAL_SECONDS)


def start_analytics_rollup():
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    threading.Thread(target=_analytics_rollup_loop, daemon=True).start()


start_analytics_rollup()


# Hole-by-hole scorecard popup for the live leaderboard, same GolfGenius
# source as tools/update_live_leaderboard.py -- each row's aggregate_id
# (captured by that script) has its own details page with one
# <tr class="net-line"> per round played so far.
GG_SCORECARD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
_scorecard_cache = {}  # aggregate_id -> {"rounds": [...], "fetched_at": ts}
SCORECARD_CACHE_TTL = 120


def fetch_scorecard(aggregate_id):
    cached = _scorecard_cache.get(aggregate_id)
    now = time.time()
    if cached and now - cached["fetched_at"] < SCORECARD_CACHE_TTL:
        return cached["rounds"]
    url = f"https://www.golfgenius.com/tournaments2/details/{aggregate_id}"
    r = requests.get(url, headers=GG_SCORECARD_HEADERS, timeout=8)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rounds = []
    for net_row in soup.find_all("tr", class_="net-line"):
        label_link = net_row.find("a", class_="expand-tee-details")
        label = label_link.get_text(strip=True) if label_link else ""
        label = re.sub(r"\s*-\s*[^-]+$", "", label)  # drop trailing "- Player Name (a)"
        holes = []
        for hole_td in net_row.find_all("td"):
            classes = hole_td.get("class", [])
            # GolfGenius doesn't keep a fixed class order -- e.g. a double bogey
            # renders 'double_square hole3 score' (marker first). Find the holeN
            # token wherever it lands instead of assuming a position.
            hole_class = next((c for c in classes if re.fullmatch(r"hole\d+", c)), None)
            if not hole_class:
                continue
            n = int(hole_class[4:])
            box = hole_td.find("span", class_="score_box")
            strokes = box.get_text(strip=True) if box else ""
            if "double_circle" in classes or "simple_circle" in classes:
                mark = "birdie"
            elif "double_square" in classes or "simple_square" in classes:
                mark = "bogey"
            else:
                mark = "par"
            holes.append({"n": n, "strokes": strokes, "mark": mark})
        holes.sort(key=lambda h: h["n"])
        if not any(h["strokes"] for h in holes):
            continue  # round not started yet
        out_td = net_row.find("td", class_="sum_front")
        in_td = net_row.find("td", class_="sum_back")
        total_td = net_row.find("td", class_="sum")
        rounds.append({
            "label": label,
            "holes": holes,
            "out": out_td.get_text(strip=True) if out_td else "",
            "in": in_td.get_text(strip=True) if in_td else "",
            "total": total_td.get_text(strip=True) if total_td else "",
        })
    _scorecard_cache[aggregate_id] = {"rounds": rounds, "fetched_at": now}
    return rounds


@app.route("/api/scorecard/<aggregate_id>")
def api_scorecard(aggregate_id):
    if not aggregate_id.isdigit():
        return jsonify({"ok": False, "error": "bad id"}), 400
    try:
        rounds = fetch_scorecard(aggregate_id)
    except requests.RequestException:
        return jsonify({"ok": False, "error": "GolfGenius unavailable"}), 502
    if not rounds:
        return jsonify({"ok": False, "error": "No rounds started yet"})
    return jsonify({"ok": True, "rounds": rounds})


# ---------------------------------------------------------------------------
# Admin image helpers -- upload/process for gallery photos, sponsor logos,
# and the hero image. Password-gated (see admin_required above); the
# /admin route itself is unlinked from the public nav, only reachable via
# the discreet footer link or a direct URL.
# ---------------------------------------------------------------------------

def slugify(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text) or "item"


_MONTH_DAY_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})"
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def parse_card_sort_key(date_str):
    """Best-effort (year, month, day) sort key from a free-text Press &
    Archives date like "July 16, 1995", "1994-1996", or "September 22-27,
    2018" -- these are hand-typed, not real dates, so this can't be exact.
    A year range sorts by its LATEST year (an era card sits alongside
    events near when it ended); a date range uses its start day. Missing
    pieces default to 0, which sorts before any real month/day in the same
    year -- reasonable since a vague "sometime in 1996" card should read as
    slightly older than a specifically-dated mid-1996 event."""
    if not date_str:
        return (0, 0, 0)
    years = [int(y) for y in _YEAR_RE.findall(date_str)]
    year = max(years) if years else 0
    month, day = 0, 0
    m = _MONTH_DAY_RE.search(date_str)
    if m:
        try:
            month = datetime.strptime(m.group(1), "%B").month
            day = int(m.group(2))
        except ValueError:
            pass
    return (year, month, day)


# Newest-first, regardless of insertion order in the JSON file -- Jimmy asked
# for this 2026-08-28. Sorted once here at load (and persisted back to disk,
# so the committed file itself reflects it too) so it's correct even if the
# file on disk isn't; admin_press_archives_add() re-sorts after every add so
# a new card always lands in the right chronological slot, not just at the
# end.
ARCHIVE_CARDS.sort(key=lambda c: parse_card_sort_key(c.get("date", "")), reverse=True)
save_json(ARCHIVE_CARDS_JSON, ARCHIVE_CARDS)
git_publish([ARCHIVE_CARDS_JSON], "Admin: sort Press & Archives cards newest-first")


def open_upload_image(file_storage):
    img = Image.open(file_storage.stream)
    img.load()
    return ImageOps.exif_transpose(img)  # auto-rotate per EXIF; EXIF itself is dropped on re-save


def save_gallery_image(file_storage, caption):
    img = open_upload_image(file_storage).convert("RGB")
    max_w = 2000
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    filename = f"gallery_{slugify(caption)}_{uuid.uuid4().hex[:6]}.jpg"
    img.save(IMAGES_DIR / filename, "JPEG", quality=87, optimize=True)
    return filename


def analyze_hero_image(img):
    """Heuristic placement: find where the image is visually 'busy' (the
    subject) vs. 'empty' (background/sky) so the scoreboard overlay lands
    on open space and the crop keeps the subject in frame. Uses edge-density
    on a downsampled grayscale copy -- no face detection library available
    in this environment, but this is a solid stand-in for photos with a
    clear subject against a simpler background."""
    gray = img.convert("L")
    thumb_w = 200
    thumb_h = max(1, round(gray.height * thumb_w / gray.width))
    thumb = gray.resize((thumb_w, thumb_h))
    edges = thumb.filter(ImageFilter.FIND_EDGES)
    px = edges.load()
    w, h = edges.size

    row_energy = [sum(px[x, y] for x in range(0, w, 3)) for y in range(h)]
    total = sum(row_energy) or 1
    weighted_y = sum(y * e for y, e in enumerate(row_energy)) / total
    obj_pos_y = max(0, min(100, round(weighted_y / h * 100)))

    band_h = max(1, round(h * 0.45))  # roughly where the top-corner overlay box sits
    left_energy = sum(px[x, y] for y in range(band_h) for x in range(0, w // 2, 2))
    right_energy = sum(px[x, y] for y in range(band_h) for x in range(w // 2, w, 2))
    lb_corner = "top-right" if right_energy <= left_energy else "top-left"

    return f"50% {obj_pos_y}%", lb_corner


def save_hero_image(file_storage):
    img = open_upload_image(file_storage).convert("RGB")
    object_position, lb_corner = analyze_hero_image(img)
    max_w = 2400
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    filename = f"hero_{uuid.uuid4().hex[:8]}.jpg"
    img.save(IMAGES_DIR / filename, "JPEG", quality=88, optimize=True)
    return filename, object_position, lb_corner


def save_press_header_image(file_storage):
    img = open_upload_image(file_storage).convert("RGB")
    max_w = 2400
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    filename = f"press_header_{uuid.uuid4().hex[:8]}.jpg"
    img.save(IMAGES_DIR / filename, "JPEG", quality=88, optimize=True)
    return filename


# Press & Archives card images live in their own subfolder (with a real
# space in the name, matching the original hand-uploaded batch) rather than
# flat under IMAGES_DIR like gallery/sponsor/hero images -- press_archives.html
# already references /static/images/Press And Archives/<file>.
PRESS_ARCHIVE_IMAGES_DIR = IMAGES_DIR / "Press And Archives"


def save_archive_card_image(file_storage, title):
    PRESS_ARCHIVE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    img = open_upload_image(file_storage).convert("RGB")
    max_w = 1400  # these are read as text (newspaper clippings), keep them sharp
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    filename = f"press_{slugify(title)}_{uuid.uuid4().hex[:6]}.jpg"
    img.save(PRESS_ARCHIVE_IMAGES_DIR / filename, "JPEG", quality=90, optimize=True)
    return filename


LOGO_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}


def find_logo_url(site_url):
    """Look up a sponsor's own site and guess their best logo image:
    prefer an actual <img class/alt~=logo> (the real header wordmark) over
    favicons, which are usually too small/generic to read well on a card."""
    r = requests.get(site_url, headers=LOGO_FETCH_HEADERS, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    candidates = []

    for img_tag in soup.find_all("img"):
        blob_parts = [img_tag.get("id") or "", img_tag.get("alt") or "", img_tag.get("src") or ""]
        classes = img_tag.get("class") or []
        blob = " ".join(blob_parts + classes).lower()
        if "logo" in blob:
            src = img_tag.get("src") or img_tag.get("data-src")
            if src:
                candidates.append((3, urljoin(site_url, src)))

    for link in soup.find_all("link", rel=lambda v: v and "apple-touch-icon" in v):
        href = link.get("href")
        if href:
            candidates.append((2, urljoin(site_url, href)))

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        candidates.append((1, urljoin(site_url, og["content"])))

    for link in soup.find_all("link", rel=lambda v: v and "icon" in v):
        href = link.get("href")
        if href:
            candidates.append((0, urljoin(site_url, href)))

    if not candidates:
        parsed = urlparse(site_url)
        candidates.append((0, f"{parsed.scheme}://{parsed.netloc}/favicon.ico"))

    candidates.sort(key=lambda c: -c[0])
    return candidates[0][1]


def save_sponsor_logo(img, slug):
    if img.mode not in ("RGBA", "LA"):
        img = img.convert("RGBA")
    max_dim = 600
    if max(img.width, img.height) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    filename = f"sponsor_{slug}_logo.png"
    img.save(IMAGES_DIR / filename, "PNG", optimize=True)
    return filename

ERAS = [
    ("all", "All"),
    ("early-years", "Early Years"),
    ("franklin-county", "Franklin County"),
    ("college", "College"),
    ("professional-years", "Professional Years"),
    ("the-comeback", "The Comeback"),
    ("2026", "2026"),
]


# Homepage "From the Archives" teaser -- three cards spanning the real career
# arc (Franklin County -> Canadian Tour -> the comeback), picked by id from
# ARCHIVE_CARDS so the full detail still only lives in one place.
ARCHIVE_TEASER_IDS = ["1995-khsaa-state-title", "2004-qschool-lead", "2018-usga-midam"]


@app.route("/")
def index():
    visible_schedule = [s for s in SCHEDULE if not s.get("hidden")]
    upcoming = [s for s in visible_schedule if s["end_date"] >= date.today().isoformat()]
    next_event = min(upcoming, key=lambda s: s["start_date"], default=None)
    cards_by_id = {c["id"]: c for c in ARCHIVE_CARDS if not c.get("hidden")}
    archive_teaser = [cards_by_id[i] for i in ARCHIVE_TEASER_IDS if i in cards_by_id]
    return render_template("index.html", hero=HERO, results=[r for r in RESULTS if not r.get("hidden")],
                            schedule=visible_schedule, next_event=next_event,
                            archive_teaser=archive_teaser,
                            sponsors=[s for s in SPONSORS if not s.get("hidden")],
                            poy_standing=POY_STANDING,
                            leaderboard_config=LEADERBOARD_CONFIG, live_leaderboard=public_live_leaderboard())


@app.route("/press-archives")
def press_archives():
    return render_template("press_archives.html", cards=[c for c in ARCHIVE_CARDS if not c.get("hidden")],
                            eras=ERAS, header=PRESS_HEADER)


@app.route("/roots")
def roots():
    return render_template("roots.html")


@app.route("/gallery")
def gallery():
    return render_template("gallery.html", photos=[p for p in GALLERY_PHOTOS if not p.get("hidden")])


@app.route("/sponsors")
def sponsors():
    return render_template("sponsors.html", sponsors=[s for s in SPONSORS if not s.get("hidden")])


@app.route("/contact", methods=["POST"])
def contact():
    data = request.get_json(silent=True) or request.form

    # honeypot -- real users never fill this hidden field, bots often do
    if (data.get("website") or "").strip():
        return jsonify({"ok": True})

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    reason = (data.get("reason") or "").strip()

    if not name or not email or not reason:
        return jsonify({"ok": False, "error": "Please fill in your name, email, and reason for reaching out."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "That email address doesn't look right."}), 400

    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return jsonify({"ok": False, "error": "The contact form isn't fully set up yet -- email info@bryanconwaygolf.com directly for now."}), 500

    body = (
        "New inquiry from the Bryan Conway Golf website contact form:\n\n"
        f"Name: {name}\n"
        f"Email: {email}\n\n"
        f"Reason for inquiry:\n{reason}\n"
    )
    msg = MIMEText(body, "plain")
    msg["From"] = GMAIL_USER
    msg["To"] = CONTACT_TO
    msg["Reply-To"] = email
    msg["Subject"] = f"Bryan Conway Golf -- Inquiry from {name}"

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo()
            s.starttls()
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [CONTACT_TO], msg.as_string())
    except Exception:
        return jsonify({"ok": False, "error": "Something went wrong sending your message. Try emailing info@bryanconwaygolf.com directly."}), 500

    return jsonify({"ok": True})


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if ADMIN_PASSWORD and request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        error = "Wrong password." if ADMIN_PASSWORD else "Admin login isn't configured (CONWAYGOLF_ADMIN_PASSWORD not set)."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


PAGE_DISPLAY_NAMES = {"/": "Home", "/roots": "Roots", "/gallery": "Gallery",
                       "/sponsors": "Sponsors", "/press-archives": "Press & Archives"}
PAGE_RAW_PATHS = {v: k for k, v in PAGE_DISPLAY_NAMES.items()}


def _relabel(items, mapping):
    return [(mapping.get(label, label), count) for label, count in items]


@app.route("/admin/analytics/detail")
@admin_required
def admin_analytics_detail():
    """Backs every click-for-detail popup on the Analytics tab. Read-only --
    just re-slices data already gathered by the tracking/rollup above."""
    kind = request.args.get("type", "")
    key = request.args.get("key", "")
    today_stats = analytics.stats_today()
    today_str = datetime.now(timezone.utc).date().isoformat()
    history = [d for d in load_json(ANALYTICS_DAILY_JSON, []) if d.get("date") != today_str]
    recent_30 = history[-30:]

    if kind == "day":
        detail = analytics.day_detail(history, key, today_stats, today_str)
        if not detail:
            return jsonify({"ok": False, "error": "No data recorded for that day."})
        return jsonify({"ok": True, "kind": "day", "data": detail})

    if kind == "referrer":
        bars = analytics.dimension_trend(recent_30, today_stats["top_referrers"], key, "top_referrers")
        return jsonify({"ok": True, "kind": "trend", "bars": bars})

    if kind == "page":
        bars = analytics.dimension_trend(recent_30, today_stats["top_pages"], PAGE_RAW_PATHS.get(key, key), "top_pages")
        return jsonify({"ok": True, "kind": "trend", "bars": bars})

    if kind == "country":
        bars = analytics.dimension_trend(recent_30, today_stats["top_countries"], key, "top_countries")
        return jsonify({"ok": True, "kind": "trend", "bars": bars})

    if kind == "device":
        bars = analytics.device_trend(recent_30, today_stats["devices"], key)
        return jsonify({"ok": True, "kind": "trend", "bars": bars})

    if kind == "kpi" and key == "pageviews":
        return jsonify({"ok": True, "kind": "hourly", "field": "pageviews", "hours": analytics.hourly_breakdown_today()})
    if kind == "kpi" and key == "visitors":
        return jsonify({"ok": True, "kind": "hourly", "field": "unique_visitors", "hours": analytics.hourly_breakdown_today()})
    if kind == "kpi" and key == "avg_session":
        return jsonify({"ok": True, "kind": "sessions", "sessions": analytics.sessions_today()})
    if kind == "kpi" and key == "alltime":
        full_history = [d for d in load_json(ANALYTICS_DAILY_JSON, []) if d.get("date") != today_str]
        bars = analytics.full_history_bars(full_history, today_stats["pageviews"])
        return jsonify({"ok": True, "kind": "trend", "bars": bars})

    return jsonify({"ok": False, "error": "Unknown detail type."})


@app.route("/admin")
@admin_required
def admin():
    today_stats = analytics.stats_today()
    today_str = datetime.now(timezone.utc).date().isoformat()
    history = [d for d in load_json(ANALYTICS_DAILY_JSON, []) if d.get("date") != today_str]
    trend_bars, trend_max = analytics.daily_trend_bars(history, days=30)
    recent_30 = history[-30:]
    return render_template(
        "admin.html", gallery=GALLERY_PHOTOS, sponsors=SPONSORS, hero=HERO, results=RESULTS,
        schedule=SCHEDULE, drafts=RESULT_DRAFTS, press_header=PRESS_HEADER,
        archive_cards=ARCHIVE_CARDS, eras=ERAS[1:],  # skip the "All" filter-pill entry
        leaderboard_config=LEADERBOARD_CONFIG, live_leaderboard=LIVE_LEADERBOARD,
        leaderboard_staleness_minutes=leaderboard_staleness_minutes(),
        analytics_today=today_stats,
        analytics_alltime_pageviews=sum(d.get("pageviews", 0) for d in history) + today_stats["pageviews"],
        analytics_trend_bars=trend_bars, analytics_trend_max=trend_max,
        analytics_device_segments=analytics.device_segments(today_stats["devices"]),
        analytics_top_referrers=analytics.ranked_bars(
            analytics.merge_ranked_over_period([d.get("top_referrers", []) for d in recent_30], today_stats["top_referrers"])),
        analytics_top_pages=analytics.ranked_bars(_relabel(
            analytics.merge_ranked_over_period([d.get("top_pages", []) for d in recent_30], today_stats["top_pages"]),
            PAGE_DISPLAY_NAMES)),
        analytics_top_countries=analytics.ranked_bars(
            analytics.merge_ranked_over_period([d.get("top_countries", []) for d in recent_30], today_stats["top_countries"])),
        messages=get_flashed_messages(with_categories=True))


@app.route("/admin/gallery/upload", methods=["POST"])
@admin_required
def admin_gallery_upload():
    file = request.files.get("photo")
    caption = (request.form.get("caption") or "").strip()
    if not file or not file.filename:
        flash("No photo selected.", "error")
        return redirect(url_for("admin"))
    try:
        filename = save_gallery_image(file, caption)
    except Exception:
        flash("Couldn't process that image -- try a different file.", "error")
        return redirect(url_for("admin"))
    GALLERY_PHOTOS.append({"image": filename, "caption": caption or "Bryan Conway Golf", "hidden": False})
    save_json(GALLERY_JSON, GALLERY_PHOTOS)
    git_publish([IMAGES_DIR / filename, GALLERY_JSON], f"Admin: add gallery photo ({caption or filename})")
    flash(f'Added "{caption or filename}" to the gallery.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/sponsors/add", methods=["POST"])
@admin_required
def admin_sponsors_add():
    name = (request.form.get("name") or "").strip()
    site_url = (request.form.get("url") or "").strip()
    blurb = (request.form.get("blurb") or "").strip() or "A proud supporter of Bryan Conway Golf."
    logo_file = request.files.get("logo")

    if not name:
        flash("Sponsor name is required.", "error")
        return redirect(url_for("admin"))

    slug = slugify(name)
    logo_filename = None
    logo_note = ""
    try:
        if logo_file and logo_file.filename:
            img = open_upload_image(logo_file)
            logo_filename = save_sponsor_logo(img, slug)
        elif site_url:
            logo_url = find_logo_url(site_url)
            resp = requests.get(logo_url, headers=LOGO_FETCH_HEADERS, timeout=10)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content))
            logo_filename = save_sponsor_logo(img, slug)
    except Exception:
        logo_note = " (couldn't find/fetch a logo from their site -- added without one, upload one manually if needed)"

    SPONSORS.append({"name": name, "blurb": blurb, "url": site_url or None, "logo": logo_filename, "hidden": False})
    save_json(SPONSORS_JSON, SPONSORS)
    publish_paths = [SPONSORS_JSON]
    if logo_filename:
        publish_paths.append(IMAGES_DIR / logo_filename)
    git_publish(publish_paths, f"Admin: add sponsor ({name})")
    flash(f'Added sponsor "{name}."{logo_note}', "ok" if not logo_note else "warn")
    return redirect(url_for("admin"))


@app.route("/admin/results/add", methods=["POST"])
@admin_required
def admin_results_add():
    date = (request.form.get("date") or "").strip()
    title = (request.form.get("title") or "").strip()
    note = (request.form.get("note") or "").strip()
    tag = (request.form.get("tag") or "").strip() or None

    if not date or not title:
        flash("Date and title are required.", "error")
        return redirect(url_for("admin"))

    # Reverse-chronological list -- a newly-added result is the newest thing
    # that happened, so it goes on top rather than at the end.
    RESULTS.insert(0, {"date": date, "title": title, "note": note, "tag": tag, "hidden": False})
    save_json(RESULTS_JSON, RESULTS)
    git_publish([RESULTS_JSON], f"Admin: add result ({title})")
    flash(f'Added "{title}" to the top of Results.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/schedule/add", methods=["POST"])
@admin_required
def admin_schedule_add():
    title = (request.form.get("title") or "").strip()
    start_date = (request.form.get("start_date") or "").strip()
    end_date = (request.form.get("end_date") or "").strip() or start_date
    note = (request.form.get("note") or "").strip()

    if not title or not start_date:
        flash("Title and start date are required.", "error")
        return redirect(url_for("admin"))

    try:
        start_dt = date.fromisoformat(start_date)
        end_dt = date.fromisoformat(end_date)
    except ValueError:
        flash("Dates need to be in YYYY-MM-DD format.", "error")
        return redirect(url_for("admin"))

    # Build "Sep 28" / "Sep 28-29" / "Sep 28-Oct 1" without relying on
    # platform-specific strftime flags for a no-leading-zero day (%-d is a
    # glibc extension -- works on Render but not on Windows, where this is
    # also run locally for testing).
    start_label = f"{start_dt.strftime('%b')} {start_dt.day}"
    if start_dt == end_dt:
        date_label = start_label
    elif start_dt.month == end_dt.month:
        date_label = f"{start_label}–{end_dt.day}"
    else:
        date_label = f"{start_label}–{end_dt.strftime('%b')} {end_dt.day}"

    SCHEDULE.append({
        "date_label": date_label, "start_date": start_date, "end_date": end_date,
        "title": title, "note": note, "hidden": False, "result_checked": False,
    })
    save_json(SCHEDULE_JSON, SCHEDULE)
    git_publish([SCHEDULE_JSON], f"Admin: add schedule event ({title})")
    flash(f'Added "{title}" to the schedule.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/hero/upload", methods=["POST"])
@admin_required
def admin_hero_upload():
    file = request.files.get("hero")
    if not file or not file.filename:
        flash("No image selected.", "error")
        return redirect(url_for("admin"))
    try:
        filename, object_position, lb_corner = save_hero_image(file)
    except Exception:
        flash("Couldn't process that image -- try a different file.", "error")
        return redirect(url_for("admin"))
    HERO["image"] = filename
    HERO["object_position"] = object_position
    HERO["object_position_mobile"] = object_position
    HERO["lb_corner"] = lb_corner
    save_json(HERO_JSON, HERO)
    git_publish([IMAGES_DIR / filename, HERO_JSON], "Admin: update hero image")
    flash("Hero image updated -- scoreboard repositioned automatically.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/press-header/upload", methods=["POST"])
@admin_required
def admin_press_header_upload():
    file = request.files.get("press_header")
    if not file or not file.filename:
        flash("No image selected.", "error")
        return redirect(url_for("admin"))
    try:
        filename = save_press_header_image(file)
    except Exception:
        flash("Couldn't process that image -- try a different file.", "error")
        return redirect(url_for("admin"))
    PRESS_HEADER["image"] = filename
    save_json(PRESS_HEADER_JSON, PRESS_HEADER)
    git_publish([IMAGES_DIR / filename, PRESS_HEADER_JSON], "Admin: update Press & Archives header")
    flash("Press & Archives header updated.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/press-archives/add", methods=["POST"])
@admin_required
def admin_press_archives_add():
    title = (request.form.get("title") or "").strip()
    source = (request.form.get("source") or "").strip()
    reporter = (request.form.get("reporter") or "").strip() or None
    date_str = (request.form.get("date") or "").strip()
    era = (request.form.get("era") or "").strip()
    summary = (request.form.get("summary") or "").strip()
    stats = (request.form.get("stats") or "").strip()
    original_url = (request.form.get("original_url") or "").strip() or None
    file = request.files.get("image")

    if not title or not source or not date_str or not era or not summary:
        flash("Title, source, date, era, and summary are all required.", "error")
        return redirect(url_for("admin"))
    if not file or not file.filename:
        flash("An image is required -- upload the clipping/photo to match the rest of Press & Archives.", "error")
        return redirect(url_for("admin"))
    if era not in dict(ERAS):
        flash("That era isn't recognized.", "error")
        return redirect(url_for("admin"))

    # Card id is a slug used for the homepage teaser lookup and the corkboard's
    # data-index -- make it unique even if two cards share a title/date.
    base_id = slugify(f"{date_str}-{title}")
    existing_ids = {c["id"] for c in ARCHIVE_CARDS}
    card_id = base_id
    n = 2
    while card_id in existing_ids:
        card_id = f"{base_id}-{n}"
        n += 1

    try:
        filename = save_archive_card_image(file, title)
    except Exception:
        flash("Couldn't process that image -- try a different file.", "error")
        return redirect(url_for("admin"))

    ARCHIVE_CARDS.append({
        "id": card_id, "title": title, "source": source, "reporter": reporter,
        "date": date_str, "era": era, "summary": summary, "stats": stats,
        "image": filename, "original_url": original_url, "hidden": False,
    })
    ARCHIVE_CARDS.sort(key=lambda c: parse_card_sort_key(c.get("date", "")), reverse=True)
    save_json(ARCHIVE_CARDS_JSON, ARCHIVE_CARDS)
    git_publish([PRESS_ARCHIVE_IMAGES_DIR / filename, ARCHIVE_CARDS_JSON], f"Admin: add press archive card ({title})")
    flash(f'Added "{title}" to Press & Archives.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/press-archives/<int:idx>/toggle", methods=["POST"])
@admin_required
def admin_press_archives_toggle(idx):
    if not 0 <= idx < len(ARCHIVE_CARDS):
        flash("That archive card no longer exists.", "error")
        return redirect(url_for("admin"))
    card = ARCHIVE_CARDS[idx]
    card["hidden"] = not card.get("hidden", False)
    save_json(ARCHIVE_CARDS_JSON, ARCHIVE_CARDS)
    git_publish([ARCHIVE_CARDS_JSON], f"Admin: {'hide' if card['hidden'] else 'show'} press archive card ({card.get('title')})")
    flash(f'"{card.get("title")}" is now {"hidden" if card["hidden"] else "visible"}.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/press-archives/<int:idx>/delete", methods=["POST"])
@admin_required
def admin_press_archives_delete(idx):
    if not 0 <= idx < len(ARCHIVE_CARDS):
        flash("That archive card no longer exists.", "error")
        return redirect(url_for("admin"))
    card = ARCHIVE_CARDS.pop(idx)
    image_name = card.get("image")
    if image_name:
        path = PRESS_ARCHIVE_IMAGES_DIR / image_name
        if path.exists():
            path.unlink()
    save_json(ARCHIVE_CARDS_JSON, ARCHIVE_CARDS)
    git_publish([ARCHIVE_CARDS_JSON, PRESS_ARCHIVE_IMAGES_DIR], f"Admin: delete press archive card ({card.get('title')})")
    flash(f'Deleted "{card.get("title")}" from Press & Archives.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/leaderboard/save", methods=["POST"])
@admin_required
def admin_leaderboard_save():
    enabled = request.form.get("enabled") == "on"
    tournament_code = (request.form.get("tournament_code") or "").strip()
    description = (request.form.get("description") or "").strip()

    was_enabled = LEADERBOARD_CONFIG.get("enabled")
    LEADERBOARD_CONFIG["enabled"] = enabled
    LEADERBOARD_CONFIG["tournament_code"] = tournament_code
    LEADERBOARD_CONFIG["description"] = description
    save_json(LEADERBOARD_CONFIG_JSON, LEADERBOARD_CONFIG)
    git_publish([LEADERBOARD_CONFIG_JSON], "Admin: update live leaderboard config")

    if enabled and not tournament_code:
        flash("Leaderboard turned on, but no tournament code was entered yet -- it won't poll until one is set.", "warn")
    elif enabled and not was_enabled:
        flash("Leaderboard enabled. It'll go live on the site once the next poll finds real scores.", "ok")
    elif not enabled:
        flash("Leaderboard turned off.", "ok")
    else:
        flash("Leaderboard settings saved.", "ok")
    return redirect(url_for("admin"))


def _delete_image_file(filename):
    if not filename:
        return
    path = IMAGES_DIR / filename
    if path.exists():
        path.unlink()


@app.route("/admin/gallery/<int:idx>/delete", methods=["POST"])
@admin_required
def admin_gallery_delete(idx):
    if not 0 <= idx < len(GALLERY_PHOTOS):
        flash("That photo no longer exists.", "error")
        return redirect(url_for("admin"))
    photo = GALLERY_PHOTOS.pop(idx)
    _delete_image_file(photo.get("image"))
    save_json(GALLERY_JSON, GALLERY_PHOTOS)
    git_publish([GALLERY_JSON, IMAGES_DIR], f"Admin: delete gallery photo ({photo.get('caption')})")
    flash(f'Deleted "{photo.get("caption")}" from the gallery.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/gallery/<int:idx>/toggle", methods=["POST"])
@admin_required
def admin_gallery_toggle(idx):
    if not 0 <= idx < len(GALLERY_PHOTOS):
        flash("That photo no longer exists.", "error")
        return redirect(url_for("admin"))
    photo = GALLERY_PHOTOS[idx]
    photo["hidden"] = not photo.get("hidden", False)
    save_json(GALLERY_JSON, GALLERY_PHOTOS)
    git_publish([GALLERY_JSON], f"Admin: {'hide' if photo['hidden'] else 'show'} gallery photo ({photo.get('caption')})")
    flash(f'"{photo.get("caption")}" is now {"hidden" if photo["hidden"] else "visible"}.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/sponsors/<int:idx>/delete", methods=["POST"])
@admin_required
def admin_sponsors_delete(idx):
    if not 0 <= idx < len(SPONSORS):
        flash("That sponsor no longer exists.", "error")
        return redirect(url_for("admin"))
    sponsor = SPONSORS.pop(idx)
    _delete_image_file(sponsor.get("logo"))
    save_json(SPONSORS_JSON, SPONSORS)
    git_publish([SPONSORS_JSON, IMAGES_DIR], f"Admin: delete sponsor ({sponsor.get('name')})")
    flash(f'Deleted sponsor "{sponsor.get("name")}."', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/sponsors/<int:idx>/toggle", methods=["POST"])
@admin_required
def admin_sponsors_toggle(idx):
    if not 0 <= idx < len(SPONSORS):
        flash("That sponsor no longer exists.", "error")
        return redirect(url_for("admin"))
    sponsor = SPONSORS[idx]
    sponsor["hidden"] = not sponsor.get("hidden", False)
    save_json(SPONSORS_JSON, SPONSORS)
    git_publish([SPONSORS_JSON], f"Admin: {'hide' if sponsor['hidden'] else 'show'} sponsor ({sponsor.get('name')})")
    flash(f'"{sponsor.get("name")}" is now {"hidden" if sponsor["hidden"] else "visible"}.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/results/<int:idx>/delete", methods=["POST"])
@admin_required
def admin_results_delete(idx):
    if not 0 <= idx < len(RESULTS):
        flash("That result no longer exists.", "error")
        return redirect(url_for("admin"))
    result = RESULTS.pop(idx)
    save_json(RESULTS_JSON, RESULTS)
    git_publish([RESULTS_JSON], f"Admin: delete result ({result.get('title')})")
    flash(f'Deleted "{result.get("title")}" from Results.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/results/<int:idx>/toggle", methods=["POST"])
@admin_required
def admin_results_toggle(idx):
    if not 0 <= idx < len(RESULTS):
        flash("That result no longer exists.", "error")
        return redirect(url_for("admin"))
    result = RESULTS[idx]
    result["hidden"] = not result.get("hidden", False)
    save_json(RESULTS_JSON, RESULTS)
    git_publish([RESULTS_JSON], f"Admin: {'hide' if result['hidden'] else 'show'} result ({result.get('title')})")
    flash(f'"{result.get("title")}" is now {"hidden" if result["hidden"] else "visible"}.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/results/<int:idx>/edit", methods=["POST"])
@admin_required
def admin_results_edit(idx):
    if not 0 <= idx < len(RESULTS):
        flash("That result no longer exists.", "error")
        return redirect(url_for("admin"))
    date = (request.form.get("date") or "").strip()
    title = (request.form.get("title") or "").strip()
    note = (request.form.get("note") or "").strip()
    tag = (request.form.get("tag") or "").strip() or None

    if not date or not title:
        flash("Date and title are required.", "error")
        return redirect(url_for("admin"))

    result = RESULTS[idx]
    result["date"] = date
    result["title"] = title
    result["note"] = note
    result["tag"] = tag
    save_json(RESULTS_JSON, RESULTS)
    git_publish([RESULTS_JSON], f"Admin: edit result ({title})")
    flash(f'Updated "{title}".', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/results/drafts/<int:idx>/publish", methods=["POST"])
@admin_required
def admin_results_draft_publish(idx):
    if not 0 <= idx < len(RESULT_DRAFTS):
        flash("That draft no longer exists.", "error")
        return redirect(url_for("admin"))
    date_ = (request.form.get("date") or "").strip()
    title = (request.form.get("title") or "").strip()
    note = (request.form.get("note") or "").strip()
    tag = (request.form.get("tag") or "").strip() or None
    if not date_ or not title:
        flash("Date and title are required.", "error")
        return redirect(url_for("admin"))

    RESULT_DRAFTS.pop(idx)
    save_json(RESULT_DRAFTS_JSON, RESULT_DRAFTS)
    RESULTS.insert(0, {"date": date_, "title": title, "note": note, "tag": tag, "hidden": False})
    save_json(RESULTS_JSON, RESULTS)
    git_publish([RESULT_DRAFTS_JSON, RESULTS_JSON], f"Admin: publish result draft ({title})")
    flash(f'Published "{title}" to Results.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/results/drafts/<int:idx>/dismiss", methods=["POST"])
@admin_required
def admin_results_draft_dismiss(idx):
    if not 0 <= idx < len(RESULT_DRAFTS):
        flash("That draft no longer exists.", "error")
        return redirect(url_for("admin"))
    draft = RESULT_DRAFTS.pop(idx)
    save_json(RESULT_DRAFTS_JSON, RESULT_DRAFTS)
    git_publish([RESULT_DRAFTS_JSON], f"Admin: dismiss result draft ({draft.get('title')})")
    flash(f'Dismissed the draft for "{draft.get("title")}."', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/schedule/<int:idx>/delete", methods=["POST"])
@admin_required
def admin_schedule_delete(idx):
    if not 0 <= idx < len(SCHEDULE):
        flash("That event no longer exists.", "error")
        return redirect(url_for("admin"))
    event = SCHEDULE.pop(idx)
    save_json(SCHEDULE_JSON, SCHEDULE)
    git_publish([SCHEDULE_JSON], f"Admin: delete schedule event ({event.get('title')})")
    flash(f'Deleted "{event.get("title")}" from the schedule.', "ok")
    return redirect(url_for("admin"))


@app.route("/admin/schedule/<int:idx>/toggle", methods=["POST"])
@admin_required
def admin_schedule_toggle(idx):
    if not 0 <= idx < len(SCHEDULE):
        flash("That event no longer exists.", "error")
        return redirect(url_for("admin"))
    event = SCHEDULE[idx]
    event["hidden"] = not event.get("hidden", False)
    save_json(SCHEDULE_JSON, SCHEDULE)
    git_publish([SCHEDULE_JSON], f"Admin: {'hide' if event['hidden'] else 'show'} schedule event ({event.get('title')})")
    flash(f'"{event.get("title")}" is now {"hidden" if event["hidden"] else "visible"}.', "ok")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=True, port=5053)
