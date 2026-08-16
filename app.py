from flask import Flask, render_template

app = Flask(__name__)

# Press & Archives -- add new entries here as more archive cards are created.
# era must be one of: early-years, franklin-county, college, professional-years,
# the-comeback, 2026 (matches the filter pills in press_archives.html).
ARCHIVE_CARDS = [
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
    return render_template("index.html")


@app.route("/press-archives")
def press_archives():
    return render_template("press_archives.html", cards=ARCHIVE_CARDS, eras=ERAS)


@app.route("/roots")
def roots():
    return render_template("roots.html")


if __name__ == "__main__":
    app.run(debug=True, port=5053)
