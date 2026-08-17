import os
import re
import smtplib
import time
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

load_dotenv()

app = Flask(__name__)

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
CONTACT_TO = "info@bryanconwaygolf.com"

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
        rows = []
        for tr in soup.find_all("tr", class_="aggregate-row")[:7]:
            pos = tr.find("td", class_="pos")
            name_link = tr.find("a", class_="open-aggregate-details")
            affiliation = tr.find("div", class_="affiliation")
            score = tr.find("td", class_="score")
            thru = tr.find("td", class_="past_round_thru")
            rows.append({
                "pos": pos.get_text(strip=True) if pos else "",
                "name": name_link.get_text(strip=True) if name_link else "",
                "city": affiliation.get_text(strip=True) if affiliation else "",
                "score": score.get_text(strip=True) if score else "",
                "thru": (thru.get_text(" ", strip=True) if thru else "").replace("*", "").strip(),
            })
        if rows:
            _leaderboard_cache["rows"] = rows
            _leaderboard_cache["fetched_at"] = now
    except requests.RequestException:
        pass
    return _leaderboard_cache["rows"]

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

# Photo Gallery -- action shots and candid photos, separate from the
# newspaper/press-style cards above. Add new entries here as photos come in.
GALLERY_PHOTOS = [
    {"image": "gallery_swing_1.jpg", "caption": "Full extension off the tee."},
    {"image": "gallery_swing_2.jpg", "caption": "Eyes on the ball flight."},
    {"image": "gallery_portrait_1.jpg", "caption": "Between shots, framed up by the flowers."},
    {"image": "gallery_portrait_2.jpg", "caption": "Locked in mid-round."},
    {"image": "action_swing.png", "caption": "Follow-through."},
    {"image": "action_swing_2.jpg", "caption": "Watching it land."},
    {"image": "dorkandfirl.jpeg", "caption": "Trophy in hand at Bardstown CC."},
]

# Sponsors -- grows over time as Bryan adds partners. Add new entries here.
SPONSORS = [
    {
        "name": "Brushy Creek Outfitters",
        "blurb": "A proud supporter of Bryan Conway Golf.",
        "url": "https://brushycreekoutfitters.com",
        "logo": "brushy_creek_logo.png",
    },
]

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
    return render_template("index.html", leaderboard=fetch_top7_leaderboard())


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


if __name__ == "__main__":
    app.run(debug=True, port=5053)
