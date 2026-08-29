"""
Lightweight, self-hosted visitor analytics for the ConwayGolf site -- no
external analytics account, no new database. Built 2026-08-28 after Jimmy
asked for a "nice dashboard" (users, origin, visit length) inside /admin.

--- Why this shape, not a real database ---
This app already stores all its content as JSON files committed to git,
redeployed on every push (see app.py's git_publish()). That pattern is fine
for content that changes rarely, but analytics events happen on every single
pageview -- git-committing each one would spam the repo and, worse, each
push triggers a Render auto-deploy, which would put the site into a
redeploy loop. So raw events live ONLY in an in-memory ring buffer
(_EVENTS) -- fast, zero disk writes, but it resets on every redeploy (a
new Render container starts empty). That's an accepted tradeoff: it's
transparently labeled "live / since last deploy" in the dashboard, and the
periodic rollup below is what makes day-over-day trends actually durable.

Every ANALYTICS_ROLLUP_INTERVAL_SECONDS, app.py's background thread calls
rollup_today() which computes *today's* aggregate stats from whatever
events are currently in memory and upserts (overwrites, not appends) a
single small JSON entry for today's date into data/analytics_daily.json,
which IS git-committed. So even if the container redeploys mid-day, at
most ANALYTICS_ROLLUP_INTERVAL_SECONDS of granularity is lost from that
day's running total -- history from prior days is untouched and durable.

--- Privacy note ---
IP addresses are only ever used transiently to resolve a country (via the
free, keyless ip-api.com) and are never stored -- events keep the resolved
country name, not the IP. Visitor identity is a random cookie value, not
tied to any personal info.
"""
import ipaddress
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

VISITOR_COOKIE = "cg_vid"
SESSION_GAP_SECONDS = 30 * 60  # standard analytics convention: a 30+ min gap = a new session
MAX_EVENTS = 8000  # ring buffer cap -- plenty for a personal site between rollups

_events = deque(maxlen=MAX_EVENTS)
_events_lock = threading.Lock()

_geo_cache = {}  # ip -> country name or None (also used to de-dupe in-flight lookups)
_geo_lock = threading.Lock()

DEVICE_ORDER = ["Desktop", "Mobile", "Tablet"]

REFERRER_LABELS = [
    ("google.", "Google"), ("bing.", "Bing"), ("duckduckgo.", "DuckDuckGo"), ("yahoo.", "Yahoo"),
    ("facebook.", "Facebook"), ("fb.watch", "Facebook"), ("instagram.", "Instagram"),
    ("twitter.", "Twitter/X"), ("t.co", "Twitter/X"), ("x.com", "Twitter/X"),
    ("golfgenius.", "GolfGenius"), ("kygolf.", "Golf House Kentucky"),
    ("l.facebook.", "Facebook"), ("lm.facebook.", "Facebook"),
]


def classify_device(user_agent_string):
    ua = (user_agent_string or "").lower()
    if "ipad" in ua or "tablet" in ua or ("android" in ua and "mobile" not in ua):
        return "Tablet"
    if "mobile" in ua or "iphone" in ua or "ipod" in ua or "android" in ua:
        return "Mobile"
    return "Desktop"


def classify_referrer(referrer_url, own_host):
    """None means "internal navigation, not a real acquisition source" --
    the pageview still counts, it just doesn't count as a referrer."""
    if not referrer_url:
        return "Direct"
    try:
        netloc = urlparse(referrer_url).netloc.lower()
    except ValueError:
        return "Direct"
    if not netloc:
        return "Direct"
    bare_host = (own_host or "").split(":")[0].lower()
    if bare_host and (netloc == bare_host or netloc.endswith("." + bare_host)):
        return None
    for needle, label in REFERRER_LABELS:
        if needle in netloc:
            return label
    return re.sub(r"^www\.", "", netloc)


def _resolve_geo_async(ip):
    def worker():
        country = None
        try:
            r = requests.get(f"http://ip-api.com/json/{ip}", params={"fields": "status,country"}, timeout=3)
            data = r.json()
            if data.get("status") == "success":
                country = data.get("country")
        except requests.RequestException:
            pass
        with _geo_lock:
            _geo_cache[ip] = country
    threading.Thread(target=worker, daemon=True).start()


def geo_lookup_cached(ip):
    """Never blocks the request: returns the cached country if known, else
    kicks off a background lookup and returns None for THIS pageview (a
    later one from the same IP will have it). Skips private/loopback IPs
    (local dev, internal health checks) without ever calling the API."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback:
            return None
    except ValueError:
        return None
    with _geo_lock:
        if ip in _geo_cache:
            return _geo_cache[ip]
        _geo_cache[ip] = None  # placeholder so a burst of requests doesn't spawn N lookups
    _resolve_geo_async(ip)
    return None


def track(path, referrer_url, own_host, user_agent_string, ip, visitor_id):
    event = {
        "ts": time.time(),
        "path": path,
        "vid": visitor_id,
        "referrer": classify_referrer(referrer_url, own_host),
        "device": classify_device(user_agent_string),
        "country": geo_lookup_cached(ip),
    }
    with _events_lock:
        _events.append(event)


def _snapshot(since_ts=None):
    with _events_lock:
        events = list(_events)
    if since_ts is not None:
        events = [e for e in events if e["ts"] >= since_ts]
    return events


def compute_stats(events):
    """Aggregate a list of raw events into dashboard-ready stats. Sessions
    are derived at read time (grouping each visitor's events by the 30-min
    gap rule) rather than tracked as their own stateful thing per request --
    simpler, and works the same whether the events span 20 minutes or a
    whole day."""
    if not events:
        return {
            "pageviews": 0, "unique_visitors": 0, "sessions": 0, "avg_session_minutes": 0.0,
            "top_referrers": [], "top_pages": [], "devices": {d: 0 for d in DEVICE_ORDER},
            "top_countries": [],
        }

    by_vid = {}
    for e in events:
        by_vid.setdefault(e["vid"], []).append(e)

    session_durations = []
    first_touch_referrers = Counter()
    for evs in by_vid.values():
        evs.sort(key=lambda e: e["ts"])
        session_start = evs[0]["ts"]
        session_ref = evs[0]["referrer"]
        last_ts = evs[0]["ts"]
        for e in evs[1:]:
            if e["ts"] - last_ts > SESSION_GAP_SECONDS:
                session_durations.append(last_ts - session_start)
                if session_ref:
                    first_touch_referrers[session_ref] += 1
                session_start = e["ts"]
                session_ref = e["referrer"]
            last_ts = e["ts"]
        session_durations.append(last_ts - session_start)
        if session_ref:
            first_touch_referrers[session_ref] += 1

    sessions = len(session_durations)
    avg_session_minutes = round((sum(session_durations) / sessions / 60), 1) if sessions else 0.0
    devices = Counter(e["device"] for e in events)

    return {
        "pageviews": len(events),
        "unique_visitors": len(by_vid),
        "sessions": sessions,
        "avg_session_minutes": avg_session_minutes,
        "top_referrers": first_touch_referrers.most_common(6),
        "top_pages": Counter(e["path"] for e in events).most_common(6),
        "devices": {d: devices.get(d, 0) for d in DEVICE_ORDER},
        "top_countries": Counter(e["country"] for e in events if e["country"]).most_common(6),
    }


def today_start_ts():
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def stats_today():
    return compute_stats(_snapshot(since_ts=today_start_ts()))


# ---------------------------------------------------------------------------
# Chart data prep -- turns aggregate stats into plain percentages/labels the
# Jinja template renders as CSS bars (no SVG or JS charting library, matching
# the rest of this site's plain-HTML/CSS approach).

# Categorical palette slots 1-3 (blue/orange/aqua) from the dataviz skill's
# reference palette -- pre-validated there as passing all-pairs CVD safety in
# both light and dark mode (worst pair CVD dE 9.2 light / 9.4 dark, well clear
# of the dE>=8 target). Device breakdown is the one place here with multiple
# categories shown at once, so it's the one place that needs real categorical
# color; everything else below is a ranked list (sequential blue is correct
# there per "compare magnitude -> bar, sequential one hue").
DEVICE_COLORS = {"Desktop": "#2a78d6", "Mobile": "#eb6834", "Tablet": "#1baf7a"}


def daily_trend_bars(history, days=30):
    """Last `days` durable daily-history entries (pre-today; today is always
    shown separately as a live stat since it isn't finalized yet) as bar
    heights relative to the period's max."""
    recent = history[-days:]
    max_v = max((d.get("pageviews", 0) for d in recent), default=0)
    return [
        {"date": d["date"], "value": d.get("pageviews", 0),
         "pct": round(d.get("pageviews", 0) / max_v * 100) if max_v else 0}
        for d in recent
    ], max_v


def ranked_bars(items):
    """[(label, count), ...] -> [{label, count, pct}], width relative to the
    largest item, for a horizontal bar list."""
    if not items:
        return []
    max_v = max(count for _, count in items)
    return [{"label": label, "count": count, "pct": round(count / max_v * 100) if max_v else 0}
            for label, count in items]


def merge_ranked_over_period(per_day_lists, today_list, limit=6):
    """Combine several days' already-ranked top-N lists (as stored in the
    daily history file) with today's, summing counts per label. This is an
    approximation -- a label that fell outside some day's own top-N is
    invisible to that day's contribution -- but daily history only stores
    each day's top few to keep the file small, and for a personal site's
    "last 30 days" dashboard view this is more than close enough."""
    combined = Counter()
    for day_list in per_day_lists:
        for label, count in day_list:
            combined[label] += count
    for label, count in today_list:
        combined[label] += count
    return combined.most_common(limit)


def device_segments(devices):
    total = sum(devices.values())
    return [
        {"label": label, "count": devices.get(label, 0),
         "pct": round(devices.get(label, 0) / total * 100) if total else 0,
         "color": DEVICE_COLORS[label]}
        for label in DEVICE_ORDER
    ]


def rollup_today(load_json, save_json, git_publish, path, history_days=90):
    """Upsert today's aggregate into the durable daily-history file. Safe to
    call often -- overwrites today's own entry each time rather than
    appending duplicates, and git_publish() already no-ops when nothing
    actually changed."""
    stats = stats_today()
    today = datetime.now(timezone.utc).date().isoformat()
    history = load_json(path, [])
    history = [d for d in history if d.get("date") != today]
    history.append({"date": today, **stats})
    history.sort(key=lambda d: d["date"])
    history = history[-history_days:]
    save_json(path, history)
    git_publish([path], f"Analytics: daily rollup ({today})")
