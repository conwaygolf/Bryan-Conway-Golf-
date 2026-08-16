from flask import Flask, render_template

app = Flask(__name__)

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
