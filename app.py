import io
import json
import os
import re
import smtplib
import subprocess
import unicodedata
import uuid
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
RESULTS_JSON = DATA_DIR / "results.json"

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
        "champion": False,
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "Player of the Year Contender",
        "note": "Conway's 2026 performance has placed him prominently in multiple Kentucky PGA Player of the Year races, including the senior standings, while also competing among players of all ages.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "Kentucky Senior Match Play — Semifinalist",
        "note": "Won his opening match at the 15th Clark's Pump-N-Shop Kentucky Match Play Championship, Owensboro Country Club, 6&5, then beat David Horning 3&2 in the Quarterfinals before falling to G. Davis Boland 3&2 in the Senior Semifinals — a season of deep championship runs against Kentucky's top senior competition.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "Kentucky Men's Match Play — Senior Division Qualifying Medalist",
        "note": "Led the Senior Division qualifying round at the 14th Clark's Pump-N-Shop Kentucky Men's Match Play Championship, Kearney Hill Golf Links — three-under 69 with an eagle on hole 3 and birdies on 9 and 11.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "Kentucky Men's Senior Stroke Play",
        "note": "Won the Kentucky Golf Association Men's Senior Stroke Play Championship, July 27–28, at Bardstown Country Club — another statewide title more than 30 years after winning the Kentucky Amateur.",
        "champion": True,
        "hidden": False,
    },
    {
        "date": "2026",
        "title": "The Resurgence",
        "note": "More than three decades after his first state championships, Conway has again emerged as one of Kentucky's most competitive golfers. At 51 years old, he's turned the 2026 season into another championship chapter in a career that began at the highest levels of Kentucky golf.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "2×",
        "title": "Daniel Boone Invitational Champion",
        "note": "Two-time Daniel Boone Invitational Champion (1995 & 2018) — a title with a Conway family connection, as father Walter Conway won the same championship in 1968 during his own competitive career.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "5×",
        "title": "Frankfort City Champion",
        "note": "Five-time Frankfort City Champion, adding victories in one of the region's longstanding competitive golf events.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "Pro",
        "title": "The Professional Years — Canadian Tour",
        "note": "Turned professional and pursued tournament golf at the highest level, competing through multiple trips to Canadian Tour Qualifying School as he pursued his professional career — the next chapter in a progression from Kentucky high school champion to decorated collegiate golfer, Kentucky Amateur champion and professional competitor.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "UofL",
        "title": "University of Louisville — Collegiate Career",
        "note": "Transferred to the University of Louisville, continuing his collegiate career at another Division I program.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "1995",
        "title": "Kentucky Amateur",
        "note": "Captured the Kentucky Amateur Championship at Kearney Hill Links, representing The Players Club, with a score of 281 — one of the signature victories of his career, won while still competing collegiately.",
        "champion": True,
        "hidden": False,
    },
    {
        "date": "MSU",
        "title": "Morehead State University — Collegiate Career",
        "note": "Began his collegiate career at Morehead State and quickly established himself as one of the Ohio Valley Conference's top young golfers — named Freshman Player of the Year and finished third in the OVC Championship, then earned Most Valuable Player as a sophomore and First Team All-OVC honors in 1995.",
        "champion": False,
        "hidden": False,
    },
    {
        "date": "1991",
        "title": "Back-to-Back KHSAA State Champion",
        "note": "Franklin County returned to the top the following season, giving Conway back-to-back state championships and cementing one of the defining chapters of his early career.",
        "champion": True,
        "hidden": False,
    },
    {
        "date": "1990",
        "title": "KHSAA State Champion",
        "note": "Helped lead Franklin County to a Kentucky high school state golf championship, establishing himself as one of the state's premier young players.",
        "champion": True,
        "hidden": False,
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
RESULTS = load_json(RESULTS_JSON, DEFAULT_RESULTS)


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


@app.route("/")
def index():
    return render_template("index.html", hero=HERO, results=[r for r in RESULTS if not r.get("hidden")])


@app.route("/press-archives")
def press_archives():
    return render_template("press_archives.html", cards=ARCHIVE_CARDS, eras=ERAS)


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


@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html", gallery=GALLERY_PHOTOS, sponsors=SPONSORS, hero=HERO, results=RESULTS,
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
    champion = bool(request.form.get("champion"))

    if not date or not title:
        flash("Date and title are required.", "error")
        return redirect(url_for("admin"))

    # Reverse-chronological list -- a newly-added result is the newest thing
    # that happened, so it goes on top rather than at the end.
    RESULTS.insert(0, {"date": date, "title": title, "note": note, "champion": champion, "hidden": False})
    save_json(RESULTS_JSON, RESULTS)
    git_publish([RESULTS_JSON], f"Admin: add result ({title})")
    flash(f'Added "{title}" to the top of Results.', "ok")
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


if __name__ == "__main__":
    app.run(debug=True, port=5053)
