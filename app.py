import io
import json
import os
import re
import smtplib
import subprocess
import time
import unicodedata
import uuid
from datetime import date
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, flash, get_flashed_messages, redirect, render_template, request, jsonify, session, url_for
from PIL import Image, ImageFilter, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "conwaygolf-admin-dev-key")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB upload cap

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
CONTACT_TO = "info@bryanconwaygolf.com"

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
IMAGES_DIR = BASE_DIR / "static" / "images"

# --- Admin auth + auto-publish -------------------------------------------
# Single shared password, no user accounts -- fine for now since it's just
# Jimmy/Bryan. Change via CONWAYGOLF_ADMIN_PASSWORD env var (local .env or
# a Render env var) rather than editing this default.
ADMIN_PASSWORD = os.getenv("CONWAYGOLF_ADMIN_PASSWORD", "BCGadmin2026!")

# Every successful admin save commits + pushes to `origin` so Render's
# auto-deploy publishes it -- that's the whole point of the admin page (no
# manual git step). GITHUB_PUSH_TOKEN (a repo-scoped PAT) is only required
# when this is running on Render itself, where there's no cached git login;
# locally, Jimmy's own git credentials are used instead (token stays unset).
# Set ADMIN_AUTO_PUBLISH=0 to save files locally without pushing (e.g. while
# testing) -- defaults on.
GITHUB_PUSH_TOKEN = os.getenv("GITHUB_PUSH_TOKEN")
GITHUB_REPO = "jstout5/ConwayGolf-"
GIT_PUBLISH_ENABLED = os.getenv("ADMIN_AUTO_PUBLISH", "1") != "0"


def git_publish(paths, message):
    """Best-effort commit + push of the given paths. Never raises -- an
    admin upload should still succeed locally even if the publish step
    fails (no token configured yet, no network, etc.)."""
    if not GIT_PUBLISH_ENABLED:
        return
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
        if GITHUB_PUSH_TOKEN:
            remote = f"https://x-access-token:{GITHUB_PUSH_TOKEN}@github.com/{GITHUB_REPO}.git"
        else:
            remote = "origin"
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

# --- TEMPORARY: live top-7 leaderboard overlay on the hero, for the
# 27th Kentucky Senior Open (Aug 17-18, 2026). Pulls the same GolfGenius
# data as tools/live_senior_open_tracker.py -- see that file for how this
# was reverse-engineered. Remove this block (and the overlay in
# index.html) once the tournament's over / the experiment's done.
GG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
GG_OVERALL_SENIOR_EVENT_ID = "4427251"  # "Senior Division 50+" -- am + pro combined
# Clean (no site chrome) GolfGenius widget page -- same one this scraper reads,
# but rendered live in a browser with real tabs across all 8 divisions. Link
# here from the widget header so viewers can find anyone not in our top 7.
GG_PUBLIC_LEADERBOARD_URL = ("https://www.golfgenius.com/leagues/511281/widgets/"
                             "tournament_results?no_header=true&round=1575855&shared=false")
_leaderboard_cache = {"rows": [], "fetched_at": 0}


def fetch_top7_leaderboard():
    now = time.time()
    if _leaderboard_cache["rows"] and now - _leaderboard_cache["fetched_at"] < 180:
        return _leaderboard_cache["rows"]
    try:
        url = (f"https://www.golfgenius.com/v2tournaments/{GG_OVERALL_SENIOR_EVENT_ID}"
               f"?player_stats_for_portal=true")
        r = requests.get(url, headers=GG_HEADERS, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        all_rows = []
        for tr in soup.find_all("tr", class_="aggregate-row"):
            pos = tr.find("td", class_="pos")
            name_link = tr.find("a", class_="open-aggregate-details")
            affiliation = tr.find("div", class_="affiliation")
            score = tr.find("td", class_="score")
            thru = tr.find("td", class_="past_round_thru")
            all_rows.append({
                "pos": pos.get_text(strip=True) if pos else "",
                "name": name_link.get_text(strip=True) if name_link else "",
                "city": affiliation.get_text(strip=True) if affiliation else "",
                "score": score.get_text(strip=True) if score else "",
                "thru": (thru.get_text(" ", strip=True) if thru else "").replace("*", "").strip(),
                "aggregate_id": tr.get("data-aggregate-id", ""),
            })
        rows = all_rows[:7]
        # Bryan drops out of the top 7 -- pin him on as an 8th row (with a
        # divider) rather than losing him off the widget entirely.
        if rows and not any("Conway" in r["name"] for r in rows):
            bryan = next((r for r in all_rows if "Conway" in r["name"]), None)
            if bryan:
                rows.append({**bryan, "pinned": True})
        if rows:
            _leaderboard_cache["rows"] = rows
            _leaderboard_cache["fetched_at"] = now
    except requests.RequestException:
        pass
    return _leaderboard_cache["rows"]


# Hole-by-hole scorecard popup, same GolfGenius data source. Each player's
# "aggregate_id" (captured above) has its own details page with one
# <tr class="net-line"> per round played so far -- see tools/live_senior_open_tracker.py
# header comment for how the widget/event-id chain was reverse-engineered;
# this endpoint was found the same way (data-remote link on the player name).
_scorecard_cache = {}  # aggregate_id -> {"rounds": [...], "fetched_at": ts}
SCORECARD_CACHE_TTL = 120


def fetch_scorecard(aggregate_id):
    cached = _scorecard_cache.get(aggregate_id)
    now = time.time()
    if cached and now - cached["fetched_at"] < SCORECARD_CACHE_TTL:
        return cached["rounds"]
    url = f"https://www.golfgenius.com/tournaments2/details/{aggregate_id}"
    r = requests.get(url, headers=GG_HEADERS, timeout=8)
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
            # GolfGenius doesn't keep a fixed class order -- a double bogey renders
            # 'double_square hole3 score' (marker first) while a plain hole renders
            # 'hole4 score' (marker absent) or 'hole1 score simple_circle' (marker
            # last). Find the holeN token wherever it lands instead of assuming a
            # position -- assuming "first class" here previously dropped any hole
            # whose marker class came first (e.g. a double bogey), shifting every
            # later hole in the display by one.
            hole_class = next((c for c in classes if re.fullmatch(r"hole\d+", c)), None)
            if not hole_class:
                continue
            n = int(hole_class[4:])
            box = hole_td.find("span", class_="score_box")
            strokes = box.get_text(strip=True) if box else ""
            if "double_circle" in classes:
                mark = "birdie"  # eagle -- no distinct glyph yet, reuse birdie styling
            elif "simple_circle" in classes:
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

# Press & Archives -- add new entries here as more archive cards are created.
# era must be one of: early-years, franklin-county, college, professional-years,
# the-comeback, 2026 (matches the filter pills in press_archives.html).
ARCHIVE_CARDS = [
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
    },
    {
        "id": "2005-florida-open",
        "title": "2005 Florida Open",
        "source": "Florida State Golf Association",
        "reporter": None,
        "date": "May 6–8, 2005",
        "era": "professional-years",
        "summary": "Conway finished T-10 and low amateur at the Florida Open at Lake City Country Club, a field that included PGA TOUR winner Corey Pavin.",
        "stats": "54-hole score: 215 (-1)  ·  Finish: T-10  ·  Low Amateur in the field",
        "image": "press_2005_florida_open.png",
        "original_url": None,
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
    },
]

# Photo Gallery, Sponsors, and Hero config are admin-editable (see /admin
# routes below) so they live in JSON files under data/, not hardcoded here.
# These are the seed values used the first time each JSON file is created.
GALLERY_JSON = DATA_DIR / "gallery.json"
SPONSORS_JSON = DATA_DIR / "sponsors.json"
HERO_JSON = DATA_DIR / "hero.json"

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

DEFAULT_HERO = {
    "image": "action_swing.png",
    "object_position": "50% 6%",
    "object_position_mobile": "50% 12%",
    "lb_corner": "top-right",
}


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


# ---------------------------------------------------------------------------
# Admin image helpers -- upload/process for gallery photos, sponsor logos,
# and the hero image. No auth yet (Jimmy's doing permissions in a later
# pass) -- the /admin route is unlinked from the public nav, only reachable
# via the discreet footer link or a direct URL.
# ---------------------------------------------------------------------------

def slugify(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text) or "item"


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


GG_BANNER_SHOW_FROM = date(2026, 8, 18)  # hide the top live-banner strip until Bryan tees off Round 2
GG_BANNER_MANUALLY_HIDDEN = True  # temporarily hidden 2026-08-18 -- hero leaderboard box covers this now; flip to False to bring it back


@app.route("/")
def index():
    show_live_banner = date.today() >= GG_BANNER_SHOW_FROM and not GG_BANNER_MANUALLY_HIDDEN
    return render_template("index.html", leaderboard=fetch_top7_leaderboard(), show_live_banner=show_live_banner,
                            gg_leaderboard_url=GG_PUBLIC_LEADERBOARD_URL, hero=HERO)


@app.route("/press-archives")
def press_archives():
    return render_template("press_archives.html", cards=ARCHIVE_CARDS, eras=ERAS)


@app.route("/roots")
def roots():
    return render_template("roots.html")


@app.route("/gallery")
def gallery():
    return render_template("gallery.html", photos=GALLERY_PHOTOS)


@app.route("/sponsors")
def sponsors():
    return render_template("sponsors.html", sponsors=SPONSORS)


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
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        error = "Wrong password."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html", gallery=GALLERY_PHOTOS, sponsors=SPONSORS, hero=HERO,
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
    GALLERY_PHOTOS.append({"image": filename, "caption": caption or "Bryan Conway Golf"})
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

    SPONSORS.append({"name": name, "blurb": blurb, "url": site_url or None, "logo": logo_filename})
    save_json(SPONSORS_JSON, SPONSORS)
    publish_paths = [SPONSORS_JSON]
    if logo_filename:
        publish_paths.append(IMAGES_DIR / logo_filename)
    git_publish(publish_paths, f"Admin: add sponsor ({name})")
    flash(f'Added sponsor "{name}."{logo_note}', "ok" if not logo_note else "warn")
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


if __name__ == "__main__":
    app.run(debug=True, port=5053)
