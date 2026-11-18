"""
Five data providers, polled concurrently:
  - SofaScore    (unofficial, no key — session cookies via cloudscraper)
  - API-Football (official, needs APIFOOTBALL_API_KEY)
  - ESPN         (unofficial, no key, bundles events with the scoreboard —
                  usually the fastest since it needs only one HTTP call)
  - TheSportsDB  (free public API, no key — daily schedules + live scores)
  - 1xBet        (unofficial lite-mirror service API, no key — featured games)
All public functions return normalized dicts with consistent field names.
Event fingerprinting uses a cross-provider semantic key (team + minute +
category) so the same real event from multiple providers collapses to one
notification — see canonical_fingerprint().
"""
import hashlib
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import http_client as hc
import cache
import sofa_session
from config import (
    APIFOOTBALL_API_KEY, CURRENT_SEASON, LEAGUE_MAP, LEAGUE_MAP_INV,
    TRACKED_LEAGUES, SPORTSDB_LEAGUE_MAP, ONEXBET_BASE_URL,
)
from normalizer import normalize

log = logging.getLogger("providers")

# ══════════════════════════════════════════════════════════════════════════
# SHARED TYPES
# ══════════════════════════════════════════════════════════════════════════

Match = dict[str, Any]
Event = dict[str, Any]

# ══════════════════════════════════════════════════════════════════════════
# SOFASCORE
# ══════════════════════════════════════════════════════════════════════════

_SOFA = "https://api.sofascore.com/api/v1"

# Full Chrome 126 headers — incomplete UA or missing Sec-Fetch-* triggers 403.
# The Cookie key is populated at runtime by _ensure_sofa_session().
_SOFA_HDR: dict[str, str] = {
    "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept":             "application/json, text/plain, */*",
    "Accept-Language":    "en-US,en;q=0.9",
    "Accept-Encoding":    "gzip, deflate, br",
    "Referer":            "https://www.sofascore.com/",
    "Origin":             "https://www.sofascore.com",
    "Sec-Ch-Ua":          '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "Sec-Ch-Ua-Mobile":   "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest":     "empty",
    "Sec-Fetch-Mode":     "cors",
    "Sec-Fetch-Site":     "same-site",
    "Cache-Control":      "no-cache",
    "Pragma":             "no-cache",
}

_SOFA_STATUS: dict[int, str] = {
    6: "1H", 7: "HT", 8: "2H", 9: "ET", 10: "ET HT",
    11: "ET 2H", 12: "Pen. W/U", 13: "PEN", 100: "FT",
    31: "PST", 60: "CANC", 70: "ABD", 40: "INT",
}
_SOFA_FINISHED = {100, 31, 60, 70}
_SOFA_LIVE     = {6, 7, 8, 9, 10, 11, 12, 13}

_SOFA_INCIDENT_MAP = {
    "goal":          ("⚽", "Goal"),
    "card":          (None, "Card"),
    "substitution":  ("🔄", "Substitution"),
    "varDecision":   ("📺", "VAR"),
    "missedPenalty": ("❌🎯", "Missed Penalty"),
    "penalty":       ("⚽🎯", "Penalty"),
    "inGamePenalty": ("⚽🎯", "Penalty"),
    "periodStart":   ("▶️", "Period Start"),
}
_SOFA_CARD_ICONS = {"yellow": "🟨", "red": "🟥", "yellowRed": "🟨🟥"}
_SOFA_GOAL_ICONS = {"ownGoal": ("⚽🙈", "Own Goal"), "penalty": ("⚽🎯", "Penalty Goal")}


async def _ensure_sofa_session() -> None:
    """Prime the SofaScore session on first use and inject cookies into _SOFA_HDR."""
    if sofa_session.needs_refresh():
        await sofa_session.refresh()
    cookie = sofa_session.cookie_header()
    if cookie:
        _SOFA_HDR["Cookie"] = cookie


async def _sofa_refresh() -> bool:
    """
    Passed as refresh_fn to hc.get for all SofaScore requests.
    Forces a new cloudscraper session and updates the shared header dict
    so the next retry attempt picks up fresh Cloudflare cookies.
    """
    ok = await sofa_session.refresh(force=True)
    if ok:
        _SOFA_HDR["Cookie"] = sofa_session.cookie_header()
    return ok


def _sofa_norm_match(e: dict) -> Match | None:
    """Returns None on malformed entries so callers can filter safely."""
    home_team = e.get("homeTeam") or {}
    away_team = e.get("awayTeam") or {}
    if not home_team.get("name") or not away_team.get("name"):
        return None
    eid = e.get("id")
    if not eid:
        return None

    code   = e.get("status", {}).get("code", 0)
    stype  = e.get("status", {}).get("type", "")
    minute = None
    if stype == "inprogress":
        ts  = (e.get("time") or {}).get("currentPeriodStartTimestamp")
        ini = (e.get("time") or {}).get("initial", 0)
        if ts:
            minute = min(int((time.time() - ts) / 60) + ini, 120)
    sofa_lid = e.get("tournament", {}).get("uniqueTournament", {}).get("id", 0)
    apf_lid  = LEAGUE_MAP_INV.get(sofa_lid, 0)
    return {
        "id":          f"sofa_{eid}",
        "sofa_id":     eid,
        "apf_id":      None,
        "home":        home_team["name"],
        "away":        away_team["name"],
        "home_norm":   normalize(home_team["name"]),
        "away_norm":   normalize(away_team["name"]),
        "home_score":  (e.get("homeScore") or {}).get("current"),
        "away_score":  (e.get("awayScore") or {}).get("current"),
        "home_logo":   f"https://api.sofascore.com/api/v1/team/{home_team.get('id', 0)}/image",
        "away_logo":   f"https://api.sofascore.com/api/v1/team/{away_team.get('id', 0)}/image",
        "status":      _SOFA_STATUS.get(code, e.get("status", {}).get("description", "?")),
        "status_code": code,
        "minute":      minute,
        "league":      e.get("tournament", {}).get("name", "Unknown"),
        "league_id":   apf_lid,
        "sofa_lid":    sofa_lid,
        "date":        "",
        "kickoff":     datetime.fromtimestamp(e["startTimestamp"], timezone.utc).isoformat() if e.get("startTimestamp") else "",
        "stadium":     "",
        "referee":     "",
        "provider":    "sofascore",
        "finished":    code in _SOFA_FINISHED,
    }


def _sofa_category(itype: str, cls: str) -> str:
    if itype == "card":
        return "red" if cls in ("red", "yellowRed") else "yellow"
    if itype in ("goal", "penalty", "inGamePenalty"):
        return "goal"
    if itype == "substitution":
        return "sub"
    if itype == "varDecision":
        return "var"
    if itype == "missedPenalty":
        return "missed_pen"
    return itype


def _sofa_norm_incident(inc: dict, home: str, away: str) -> Event | None:
    itype = inc.get("incidentType", "")
    if itype not in _SOFA_INCIDENT_MAP:
        return None

    icon, label = _SOFA_INCIDENT_MAP[itype]
    cls = inc.get("incidentClass", "")

    if itype == "card":
        icon  = _SOFA_CARD_ICONS.get(cls, "🟨")
        label = {"yellow": "Yellow Card", "red": "Red Card", "yellowRed": "2nd Yellow → Red"}.get(cls, "Card")
    elif itype in ("goal", "penalty", "inGamePenalty"):
        if cls in _SOFA_GOAL_ICONS:
            icon, label = _SOFA_GOAL_ICONS[cls]

    team_side = inc.get("teamSide", "home")
    team_name = home if team_side == "home" else away
    player    = (inc.get("player") or {}).get("name", "Unknown")
    assist    = (inc.get("assist") or {}).get("name", "")
    minute    = inc.get("time", 0)
    added     = inc.get("addedTime")
    min_str   = f"{minute}+{added}" if added else str(minute)

    category = _sofa_category(itype, cls)
    fp = canonical_fingerprint(normalize(team_name), minute, category)

    return {
        "type":        itype,
        "detail":      label,
        "minute":      min_str,
        "minute_int":  minute,
        "player":      player,
        "assist":      assist,
        "team":        team_name,
        "icon":        icon,
        "provider":    "sofascore",
        "fingerprint": fp,
    }


def canonical_fingerprint(team_norm: str, minute: int, category: str) -> str:
    """
    Cross-provider dedup key.

    Each provider assigns its own internal player/event IDs, so hashing those
    (as this used to do) means the *same real goal* reported by SofaScore and
    API-Football never produces the same fingerprint — both would get sent.
    Team + minute + event category is provider-agnostic: whichever source
    reports a given goal/card first "claims" it, and the same event coming
    in from another source a moment later is recognized as a duplicate.

    Trade-off: two distinct events by the same team, in the same minute, in
    the same category (e.g. a brace in stoppage time) would collide and only
    the first would be sent. This is rare enough to accept for a live-alert
    bot; a stricter key would need reliable cross-provider player-ID mapping,
    which doesn't exist between these sources.
    """
    raw = f"{team_norm}_{minute}_{category}"
    return hashlib.sha1(raw.encode()).hexdigest()


async def sofa_live_matches() -> list[Match]:
    await _ensure_sofa_session()
    data = await hc.get(f"{_SOFA}/sport/football/events/live", _SOFA_HDR,
                        cache_key="sofa_live", provider="sofascore",
                        refresh_fn=_sofa_refresh)
    return [m for e in (data or {}).get("events", [])
            if (m := _sofa_norm_match(e)) is not None]


async def sofa_incidents(sofa_id: int, home: str, away: str) -> list[Event]:
    await _ensure_sofa_session()
    data = await hc.get(f"{_SOFA}/event/{sofa_id}/incidents", _SOFA_HDR,
                        cache_key=f"sofa_inc_{sofa_id}", provider="sofascore",
                        refresh_fn=_sofa_refresh)
    return [n for inc in (data or {}).get("incidents", [])
            if (n := _sofa_norm_incident(inc, home, away)) is not None]


async def sofa_upcoming(sofa_lid: int, days: int = 7) -> list[Match]:
    from datetime import date, timedelta
    await _ensure_sofa_session()
    results = []
    for i in range(days):
        d = (date.today() + timedelta(days=i)).strftime("%Y-%m-%d")
        data = await hc.get(f"{_SOFA}/sport/football/scheduled-events/{d}",
                            _SOFA_HDR, provider="sofascore",
                            refresh_fn=_sofa_refresh)
        for e in (data or {}).get("events", []):
            tid = e.get("tournament", {}).get("uniqueTournament", {}).get("id")
            if tid == sofa_lid:
                m = _sofa_norm_match(e)
                if m:
                    results.append(m)
    return results[:10]


async def sofa_lineups(sofa_id: int) -> dict:
    await _ensure_sofa_session()
    data = await hc.get(f"{_SOFA}/event/{sofa_id}/lineups", _SOFA_HDR,
                        provider="sofascore", refresh_fn=_sofa_refresh)
    return data or {}


async def sofa_h2h(sofa_id: int) -> list[Match]:
    await _ensure_sofa_session()
    data = await hc.get(f"{_SOFA}/event/{sofa_id}/h2h/events", _SOFA_HDR,
                        provider="sofascore", refresh_fn=_sofa_refresh)
    events = (data or {}).get("previousEvents", [])
    return [m for e in events[:10] if (m := _sofa_norm_match(e)) is not None]


# ══════════════════════════════════════════════════════════════════════════
# API-FOOTBALL
# ══════════════════════════════════════════════════════════════════════════

_APF = "https://v3.football.api-sports.io"
_APF_HDR = {"x-apisports-key": APIFOOTBALL_API_KEY}

_APF_ICONS: dict[str, str] = {
    "Goal":           "⚽",
    "own goal":       "⚽🙈",
    "Penalty":        "⚽🎯",
    "Missed Penalty": "❌🎯",
    "yellowcard":     "🟨",
    "redcard":        "🟥",
    "yellowredcard":  "🟨🟥",
    "subst":          "🔄",
    "Var":            "📺",
}


def _apf_norm(f: dict) -> Match:
    g   = f["goals"]
    fix = f["fixture"]
    tm  = f["teams"]
    lg  = f["league"]
    return {
        "id":          fix["id"],
        "sofa_id":     None,
        "apf_id":      fix["id"],
        "home":        tm["home"]["name"],
        "away":        tm["away"]["name"],
        "home_norm":   normalize(tm["home"]["name"]),
        "away_norm":   normalize(tm["away"]["name"]),
        "home_score":  g["home"],
        "away_score":  g["away"],
        "home_logo":   tm["home"].get("logo", ""),
        "away_logo":   tm["away"].get("logo", ""),
        "status":      fix["status"]["short"],
        "status_code": 0,
        "minute":      fix["status"].get("elapsed"),
        "league":      lg["name"],
        "league_id":   lg["id"],
        "sofa_lid":    LEAGUE_MAP.get(lg["id"], 0),
        "date":        fix["date"][:10],
        "kickoff":     fix.get("date", ""),
        "stadium":     (fix.get("venue") or {}).get("name", ""),
        "referee":     fix.get("referee", "") or "",
        "provider":    "apifootball",
        "finished":    fix["status"]["short"] in ("FT", "AET", "PEN", "PST", "CANC", "ABD"),
    }


def _apf_category(etype: str, detail: str) -> str:
    et, d = etype.lower(), detail.lower()
    if et == "goal":
        return "goal"
    if et == "card":
        return "red" if "red" in d else "yellow"
    if et == "subst":
        return "sub"
    if et == "var":
        return "var"
    if "missed penalty" in d:
        return "missed_pen"
    return et


def _apf_norm_event(e: dict, fixture_id: int) -> Event:
    detail    = e.get("detail", "") or ""
    etype     = e.get("type", "") or ""
    icon      = _APF_ICONS.get(detail) or _APF_ICONS.get(etype, "📌")
    time_data = e.get("time") or {}
    minute    = time_data.get("elapsed", 0)
    added     = time_data.get("extra")
    team_name = (e.get("team") or {}).get("name", "")

    category = _apf_category(etype, detail)
    fp = canonical_fingerprint(normalize(team_name), minute, category)

    return {
        "type":        etype,
        "detail":      detail,
        "minute":      f"{minute}+{added}" if added else str(minute),
        "minute_int":  minute,
        "player":      (e.get("player") or {}).get("name", ""),
        "assist":      (e.get("assist") or {}).get("name", ""),
        "team":        team_name,
        "icon":        icon,
        "provider":    "apifootball",
        "fingerprint": fp,
    }


async def apf_live() -> list[Match]:
    data = await hc.get(f"{_APF}/fixtures", _APF_HDR, {"live": "all"},
                        cache_key="apf_live", provider="apifootball")
    return [_apf_norm(f) for f in (data or {}).get("response", [])]


async def apf_events(fixture_id: int) -> list[Event]:
    data = await hc.get(f"{_APF}/fixtures/events", _APF_HDR, {"fixture": fixture_id},
                        cache_key=f"apf_ev_{fixture_id}", provider="apifootball")
    return [_apf_norm_event(e, fixture_id) for e in (data or {}).get("response", [])]


async def apf_upcoming(league_id: int, next_n: int = 10) -> list[Match]:
    from cache import get as cget, set as cset
    from config import TTL_FIXTURES
    key  = f"apf_fix_{league_id}"
    hit  = cget(key)
    if hit is not None:
        return hit
    data = await hc.get(f"{_APF}/fixtures", _APF_HDR,
                        {"league": league_id, "season": CURRENT_SEASON, "next": next_n},
                        provider="apifootball")
    result = [_apf_norm(f) for f in (data or {}).get("response", [])]
    cset(key, result, TTL_FIXTURES)
    return result


async def apf_stats(fixture_id: int) -> list:
    data = await hc.get(f"{_APF}/fixtures/statistics", _APF_HDR,
                        {"fixture": fixture_id}, provider="apifootball")
    return (data or {}).get("response", [])


























# ══════════════════════════════════════════════════════════════════════════
# THE SPORTS DB (public free daily schedule fallback)
# ══════════════════════════════════════════════════════════════════════════

_SPORTSDB = "https://www.thesportsdb.com/api/v1/json/123"
_SPORTSDB_HDR = {"Accept": "application/json", "User-Agent": "FootballBot/1.0"}


def _sportsdb_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _sportsdb_norm(event: dict, league_id: int) -> Match | None:
    home = event.get("strHomeTeam")
    away = event.get("strAwayTeam")
    if not home or not away:
        return None
    status = event.get("strStatus") or event.get("strProgress") or "Scheduled"
    return {
        "id":          f"sportsdb_{event.get('idEvent', '')}",
        "sofa_id":     None,
        "apf_id":      None,
        "home":        home,
        "away":        away,
        "home_norm":   normalize(home),
        "away_norm":   normalize(away),
        "home_score":  _sportsdb_int(event.get("intHomeScore")),
        "away_score":  _sportsdb_int(event.get("intAwayScore")),
        "home_logo":   event.get("strHomeTeamBadge") or "",
        "away_logo":   event.get("strAwayTeamBadge") or "",
        "status":      status,
        "status_code": 0,
        "minute":      None,
        "league":      event.get("strLeague") or TRACKED_LEAGUES.get(league_id, "Football"),
        "league_id":   league_id,
        "sofa_lid":    LEAGUE_MAP.get(league_id, 0),
        "date":        event.get("dateEvent") or "",
        "kickoff":     event.get("strTimestamp") or "",
        "stadium":     event.get("strVenue") or "",
        "referee":     "",
        "provider":    "sportsdb",
        "finished":    status.lower() in {"match finished", "finished", "ft", "aet", "pen"},
    }


def sportsdb_is_live(match: Match) -> bool:
    """Best-effort live detection from TheSportsDB's public schedule feed."""
    if match.get("finished"):
        return False
    status = str(match.get("status") or "").strip().lower()
    not_started = {"", "scheduled", "not started", "ns", "tbd", "postponed"}
    if status in not_started or (len(status) == 8 and status[2:3] == ":" and status[5:6] == ":"):
        return False
    # A populated score is the most reliable signal across TheSportsDB's
    # varying status strings (0-0 is represented by integer zero, not None).
    return match.get("home_score") is not None or match.get("away_score") is not None


async def sportsdb_day(league_id: int, day: str) -> list[Match]:
    """Free daily schedule for supported leagues; no user API key required."""
    sportsdb_league_id = SPORTSDB_LEAGUE_MAP.get(league_id)
    if not sportsdb_league_id:
        return []
    data = await hc.get(
        f"{_SPORTSDB}/eventsday.php", _SPORTSDB_HDR,
        {"d": day, "s": "Soccer", "l": sportsdb_league_id},
        cache_key=f"sportsdb_{league_id}_{day}", provider="sportsdb",
    )
    # TheSportsDB returns `"events": null` for a valid date with no matches.
    # Treat it as an empty list rather than crashing a slash command or poller.
    return [m for event in ((data or {}).get("events") or [])
            if (m := _sportsdb_norm(event, league_id)) is not None]


async def sportsdb_live_matches(league_ids: list[int] | None = None) -> list[Match]:
    """Return currently live matches from all supported public leagues."""
    from datetime import date
    ids = league_ids or list(SPORTSDB_LEAGUE_MAP)
    batches = await asyncio.gather(
        *(sportsdb_day(league_id, date.today().isoformat()) for league_id in ids),
        return_exceptions=True,
    )
    matches: list[Match] = []
    for batch in batches:
        if isinstance(batch, list):
            matches.extend(match for match in batch if sportsdb_is_live(match))
    return matches


async def free_fixtures_for_day(league_id: int, day: str) -> list[Match]:
    """Use a public provider first, then API-Football only if it has data."""
    cache_key = f"fixtures:{league_id}:{day}"
    free = await sportsdb_day(league_id, day)
    if free:
        cache.set(cache_key, free, ttl=1800)
        return free
    # Keep the existing paid provider as a secondary source for leagues not
    # covered by TheSportsDB or dates where its schedule is incomplete.
    data = await hc.get(
        f"{_APF}/fixtures", _APF_HDR,
        {"league": league_id, "season": CURRENT_SEASON, "date": day},
        provider="apifootball",
    )
    fallback = [_apf_norm(f) for f in (data or {}).get("response", [])]
    if fallback:
        cache.set(cache_key, fallback, ttl=1800)
        return fallback
    # A short cache keeps commands and reminders useful through a temporary
    # provider outage without inventing fixtures for an uncached empty day.
    return cache.get(cache_key) or []


# ══════════════════════════════════════════════════════════════════════════
# ESPN  (unofficial, free, no API key, no signed-request headers)
#
# Unlike SofaScore/API-Football, ESPN's scoreboard endpoint bundles each
# match's goal/card feed directly in the response (`competitions[].details`),
# so a single HTTP call gets both scores *and* events — no follow-up request
# per fixture. That makes it the fastest of the three providers here.
#
# Coverage note: ESPN's `details` feed includes goals, own goals, penalties,
# and cards, but not substitutions.
# ══════════════════════════════════════════════════════════════════════════

_ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_ESPN_HDR = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _espn_parse_minute(display: str) -> tuple[int | None, int | None]:
    """"43'" -> (43, None); "90'+4'" -> (90, 4)."""
    if not display:
        return None, None
    s = display.replace("'", "")
    try:
        if "+" in s:
            base, extra = s.split("+", 1)
            return int(base), int(extra)
        return int(s), None
    except ValueError:
        return None, None


def _espn_norm(ev: dict, apf_lid: int) -> tuple[Match, list[Event]]:
    comp        = ev["competitions"][0]
    status_type = comp["status"]["type"]
    competitors = comp["competitors"]
    home        = next(c for c in competitors if c["homeAway"] == "home")
    away        = next(c for c in competitors if c["homeAway"] == "away")
    home_name   = home["team"]["displayName"]
    away_name   = away["team"]["displayName"]
    home_norm, away_norm = normalize(home_name), normalize(away_name)

    minute = None
    if status_type.get("state") == "in":
        minute, _ = _espn_parse_minute(status_type.get("shortDetail", ""))

    league_name = TRACKED_LEAGUES.get(apf_lid, ev.get("name", ""))

    match: Match = {
        "id":          f"espn_{ev['id']}",
        "sofa_id":     None,
        "apf_id":      None,
        "home":        home_name,
        "away":        away_name,
        "home_norm":   home_norm,
        "away_norm":   away_norm,
        "home_score":  int(home["score"]) if home.get("score") not in (None, "") else None,
        "away_score":  int(away["score"]) if away.get("score") not in (None, "") else None,
        "home_logo":   home["team"].get("logo", ""),
        "away_logo":   away["team"].get("logo", ""),
        "status":      status_type.get("shortDetail", "?"),
        "status_code": 0,
        "minute":      minute,
        "league":      league_name,
        "league_id":   apf_lid,
        "sofa_lid":    0,
        "date":        (comp.get("date") or "")[:10],
        "kickoff":     comp.get("date") or "",
        "stadium":     (comp.get("venue") or {}).get("fullName", ""),
        "referee":     "",
        "provider":    "espn",
        "finished":    bool(status_type.get("completed")),
    }

    events: list[Event] = []
    for d in comp.get("details", []):
        scoring, red, yellow = d.get("scoringPlay"), d.get("redCard"), d.get("yellowCard")
        if not (scoring or red or yellow):
            continue
        category = "goal" if scoring else ("red" if red else "yellow")

        team_id   = (d.get("team") or {}).get("id")
        team_name = home_name if team_id == home["team"]["id"] else away_name
        team_norm = normalize(team_name)

        minute_int, added = _espn_parse_minute((d.get("clock") or {}).get("displayValue", ""))
        players = d.get("athletesInvolved") or []
        player  = players[0].get("displayName", "Unknown") if players else "Unknown"

        if scoring:
            icon = "⚽🙈" if d.get("ownGoal") else ("⚽🎯" if d.get("penaltyKick") else "⚽")
        else:
            icon = "🟥" if red else "🟨"
        label = (d.get("type") or {}).get("text", category.title())

        fp = canonical_fingerprint(team_norm, minute_int or 0, category)
        events.append({
            "type":        category,
            "detail":      label,
            "minute":      f"{minute_int}+{added}" if added else str(minute_int),
            "minute_int":  minute_int,
            "player":      player,
            "assist":      "",
            "team":        team_name,
            "icon":        icon,
            "provider":    "espn",
            "fingerprint": fp,
        })

    return match, events


async def espn_live(espn_slug: str, apf_lid: int) -> tuple[list[Match], dict[str, list[Event]]]:
    """
    Returns (matches, events_by_team_pair_key) for one league's scoreboard.
    events_by_team_pair_key is keyed by "home_norm|away_norm" so the caller
    can cross-link it to whatever it already knows this match as.
    """
    data = await hc.get(f"{_ESPN}/{espn_slug}/scoreboard", _ESPN_HDR,
                        cache_key=f"espn_live_{espn_slug}", provider="espn")
    matches: list[Match] = []
    events_by_key: dict[str, list[Event]] = {}
    for ev in (data or {}).get("events", []):
        try:
            m, evs = _espn_norm(ev, apf_lid)
        except (KeyError, StopIteration, TypeError):
            continue
        matches.append(m)
        events_by_key[f"{m['home_norm']}|{m['away_norm']}"] = evs
    return matches, events_by_key


# ══════════════════════════════════════════════════════════════════════════
# 1XBET ("onexbet")
#
# Unofficial: the lite mirror's service-api (main-live-feed/v3), captured
# from the user's Chrome via the Muse Network Inspector, 17 Nov 2026.
# No key, no login — the API answers a plain GET with browser-like headers.
# The dynamic `x-hd` header the browser sends is NOT needed (verified:
# the endpoint answers 200 without it) and is never reproduced here.
#
# Coverage limit (read this before expecting every match): topGamesByStat
# returns only the games 1xBet features (~5 at capture time while 41
# football games were live). There is no verified "all live games" list
# call yet — per-champ games can be expanded via leftMenuSports with a
# `selectedMs` value for that champ, but the 1xBet champ IDs for the
# tracked leagues are not known yet. Until that is captured, 1xBet covers
# featured games (usually the big matches) and merges its events into
# matches the other providers already track.
# ══════════════════════════════════════════════════════════════════════════

_ONEX = ONEXBET_BASE_URL.rstrip("/")
_ONEX_FEED = f"{_ONEX}/service-api/main-live-feed/v3"

# Headers reproduced from the captured browser request. Referer is the
# live-football index page (the browser's Referer was the match page it
# happened to be on — the index page answers the same way).
_ONEX_HDR: dict[str, str] = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36",
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Referer":          f"{_ONEX}/en/live/football",
    "x-app-n":          "__BETTING_APP__",
    "x-svc-source":     "__BETTING_APP__",
    "x-requested-with": "XMLHttpRequest",
}

# Query params as captured. country/fcountry 130 = Nepal (the user's
# locale); gr is 1xBet's internal group id — keep as captured.
_ONEX_TOP_PARAMS: dict[str, Any] = {
    "cfView": 3, "country": 130, "fcountry": 130, "gr": 909, "lng": "en", "ref": 1,
}
_ONEX_GAME_PARAMS: dict[str, Any] = {
    "fcountry": 130, "gr": 909, "lng": "en", "ref": 1,
}

# 1xBet liga name (normalised) -> APF league id. Matched by substring so
# "England. Premier League" and "Premier League" both hit. Longer aliases
# are checked first ("UEFA Europa Conference League" must win over
# "UEFA Europa League"). Leagues that are not tracked keep league_id 0
# Leagues that are not tracked keep league_id 0.
_ONEX_LEAGUE_ALIASES: dict[str, int] = {
    "uefa europa conference league": 848,
    "uefa champions league":         2,
    "uefa europa league":            3,
    "fifa club world cup":           15,
    "premier league":                39,
    "la liga":                       140,
    "serie a":                       135,
    "bundesliga":                    78,
    "ligue 1":                       61,
}
# Substrings that disqualify a liga even if an alias matches — second
# divisions, women's and youth competitions share the base name.
_ONEX_LEAGUE_EXCLUDE = (
    "women", "u21", "u19", "u17", "youth", "reserve", "amateur",
    "la liga 2", "segunda", "2. bundesliga", "bundesliga 2", "serie b", "ligue 2",
)


def _onex_league_id(liga_name: str) -> int:
    n = normalize(liga_name or "")
    if any(x in n for x in _ONEX_LEAGUE_EXCLUDE):
        return 0
    for alias in sorted(_ONEX_LEAGUE_ALIASES, key=len, reverse=True):
        if alias in n:
            return _ONEX_LEAGUE_ALIASES[alias]
    return 0


def _onex_status(scores: dict) -> tuple[str, int | None]:
    """(status, minute) from a 1xBet `scores` object.

    Status strings reuse the bot's vocabulary (HT/2H/FT/...).
    The clock is `timer.timeSec`
    (seconds since kick-off); `statusLineStr` ("64 minutes") is the
    fallback. A paused clock in the 2nd half still reports 2H.
    """
    if not scores:
        return "?", None
    timer = scores.get("timer") or {}
    time_sec = timer.get("timeSec")
    minute = None
    if isinstance(time_sec, (int, float)):
        minute = min(int(time_sec // 60), 120)
    if minute is None:
        line = (scores.get("statusLineStr") or "").split()
        try:
            minute = int(line[0]) if line else None
        except ValueError:
            minute = None
    line = (scores.get("statusLineStr") or "").lower()
    if "half time" in line or "half-time" in line or "break" in line:
        return "HT", minute
    if "full time" in line or "finished" in line:
        return "FT", minute
    period = scores.get("currentPeriod")
    pname = (scores.get("currentPeriodName") or "").lower()
    if period == 1 or "1st half" in pname:
        return "1H", minute
    if period == 2 or "2nd half" in pname:
        return "2H", minute
    if period in (3, 4) or "extra" in pname:
        return "ET", minute
    if "penalt" in pname:
        return "PEN", minute
    return scores.get("currentPeriodName") or "?", minute


def _onex_parse_minute(raw: str) -> tuple[int | None, int | None]:
    """"53'" -> (53, None); "45+2'" -> (45, 2)."""
    s = (raw or "").strip().rstrip("'")
    try:
        if "+" in s:
            base, extra = s.split("+", 1)
            return int(base), int(extra)
        return int(s), None
    except ValueError:
        return None, None


def _onex_norm_match(g: dict, liga: dict | None = None) -> Match | None:
    o1 = g.get("opponent1") or {}
    o2 = g.get("opponent2") or {}
    home = o1.get("fullName") or ""
    away = o2.get("fullName") or ""
    gid = g.get("id") or g.get("mainGameId")
    if not home or not away or not gid:
        return None

    scores = g.get("scores") or {}
    status, minute = _onex_status(scores)
    liga = liga or g.get("liga") or {}
    liga_name = liga.get("name") or "Unknown"

    def _logo(opp: dict) -> str:
        opps = opp.get("opps") or []
        img = (opps[0].get("image") if opps else "") or ""
        return f"{_ONEX}{img}" if img.startswith("/") else img

    date = ""
    ts = g.get("startTs")
    if ts:
        try:
            from datetime import datetime, timezone
            date = datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            date = ""

    return {
        "id":          f"onex_{gid}",
        "sofa_id":     None,
        "apf_id":      None,
        "onex_id":     gid,
        "home":        home,
        "away":        away,
        "home_norm":   normalize(home),
        "away_norm":   normalize(away),
        "home_score":  scores.get("scoreOpp1"),
        "away_score":  scores.get("scoreOpp2"),
        "home_logo":   _logo(o1),
        "away_logo":   _logo(o2),
        "status":      status,
        "status_code": 0,
        "minute":      minute,
        "league":      liga_name,
        "league_id":   _onex_league_id(liga_name),
        "sofa_lid":    0,
        "date":        date,
        "stadium":     ((g.get("matchInfoObj") or {}).get("location") or ""),
        "referee":     "",
        "provider":    "onexbet",
        "finished":    status == "FT",
    }


# chronoLine eventType -> (category, icon, detail). Verified from the
# Iceland-Luxembourg fixture: 200 Goal, 218 Penalty (= penalty scored —
# it accounts for the 37' goal in the 2-0 half-time scoreline), 204
# Substitution. Corners (201) and other stat events are not bot alerts.
# Card event types were NOT in the captured fixtures — add them here once
# one is observed (yellow/red card in a chronoLine response).
_ONEX_EVENT_MAP: dict[int, tuple[str, str, str]] = {
    200: ("goal", "⚽",   "Goal"),
    218: ("goal", "⚽🎯", "Penalty Goal"),
    204: ("sub",  "🔄",   "Substitution"),
}


def _onex_norm_event(ev: dict, home: str, away: str) -> Event | None:
    etype = ev.get("eventType")
    if etype not in _ONEX_EVENT_MAP:
        return None
    category, icon, label = _ONEX_EVENT_MAP[etype]
    minute_int, added = _onex_parse_minute(ev.get("minute", ""))
    team_name = home if ev.get("side") == 1 else away
    player = (ev.get("player") or {}).get("fullName") or "Unknown"
    second = (ev.get("secondPlayer") or {}).get("fullName") or ""

    if category == "sub":
        # chronoLine lists the INCOMING player as `player` and the
        # OUTGOING one as `secondPlayer` — verified: Andri Gudjohnsen
        # scored at 37' yet is `secondPlayer` of the 46' sub, so he was
        # already on the pitch and must be the outgoing one.
        player = f"{player} on, {second} off" if second else player
        second = ""

    fp = canonical_fingerprint(normalize(team_name), minute_int or 0, category)
    return {
        "type":        category,
        "detail":      label,
        "minute":      f"{minute_int}+{added}" if added else str(minute_int),
        "minute_int":  minute_int,
        "player":      player,
        "assist":      second,
        "team":        team_name,
        "icon":        icon,
        "provider":    "onexbet",
        "fingerprint": fp,
    }


async def onex_live_matches(use_cache: bool = True) -> list[Match]:
    """Featured live football games from 1xBet (topGamesByStat).

    One HTTP call. Only the games 1xBet features — not every live match
    (see the section header). Unmapped leagues keep league_id 0 and are
    but are still returned for /live.
    use_cache=False skips http_client's cache entirely: the scoreboard
    watches 1xBet every second and must reach the server on every poll.
    """
    data = await hc.get(f"{_ONEX_FEED}/topGamesByStat", _ONEX_HDR,
                        dict(_ONEX_TOP_PARAMS),
                        cache_key="onex_live" if use_cache else "",
                        provider="onexbet")
    if not isinstance(data, list):
        return []
    return [m for g in data if (m := _onex_norm_match(g)) is not None]


async def onex_all_live_matches() -> list[Match]:
    """Every live game from 1xBet (games1x2 + topGamesByStat merged,
    via the shared tick cache) — the full live list, not just the
    ~5 featured games topGamesByStat returns. Served from the same
    cache /game and /watch use, so no extra 1xBet calls. [] when
    1xBet answers with no live games."""
    raw = await onex_live_raw()
    out: list[Match] = []
    seen: set[str] = set()
    for g in raw:
        m = _onex_norm_match(g)
        if m is None:
            continue
        gid = str(m["onex_id"])
        if gid in seen:
            continue
        seen.add(gid)
        out.append(m)
    return out


async def onex_events(game_id: int, home: str, away: str) -> list[Event]:
    """Match timeline (goals, penalty goals, substitutions) from chronoLine.

    Returns the whole timeline every call.
    """
    params = dict(_ONEX_GAME_PARAMS)
    params["gameId"] = game_id
    data = await hc.get(f"{_ONEX_FEED}/chronoLine", _ONEX_HDR, params,
                        cache_key=f"onex_chrono_{game_id}", provider="onexbet")
    if not isinstance(data, dict):
        return []
    events: list[Event] = []
    for period in data.get("chronoLine", []):
        for ev in period.get("events", []):
            if (n := _onex_norm_event(ev, home, away)) is not None:
                events.append(n)
    return events


# ══════════════════════════════════════════════════════════════════════════
# NPL (Nepal Premier League) — live cricket for the scoreboard
#
# onex_live_matches() only returns featured games, and NPL is rarely
# featured, so the scoreboard fetches it directly:
#   1. 1xBet live list (games1x2, all live games, count=50 max) filtered
#      by liga name "Nepal. Premier league" — score + overs from
#      scores.extendedScore (cricketScore.wicketScore + innsStats.overStr,
#      the same fields the site's Vercel proxy uses).
# 1xBet only — no other source is fetched for the scoreboard, so when
# 1xBet carries no live NPL score the NPL section stays empty.
# NPL is always T20, so the chasing target is first-innings + 1.
# ══════════════════════════════════════════════════════════════════════════

_ONEX_GAMES1X2_PARAMS: dict[str, Any] = {
    "cfView": 3, "count": 50, "country": 130, "fcountry": 130,
    "gr": 909, "grMode": 4, "lng": "en", "ref": 1,
}
_NPL_LIGA_NAME = "nepal. premier league"
_NPL_TOTAL_BALLS = 120  # T20


def _onex_cricket_clock(scores: dict) -> str | None:
    """Overs line for a live cricket game, e.g. "16.4 ov".

    Innings break (isBreak / "Half-time"): the overs read 0.0 for the
    innings that hasn't started — say so instead of "0.0 ov".
    """
    ext = (scores or {}).get("extendedScore") or {}
    inns = ext.get("innsStats") or {}
    if scores and scores.get("isBreak"):
        return "Innings break"
    over = inns.get("overStr")
    clock = f"{over} ov" if over is not None else None
    pname = str((scores or {}).get("currentPeriodName") or "")
    if pname in ("1st innings", "2nd innings") and clock:
        return f"{pname} \u00b7 {clock}"
    return clock


def _onex_cricket_target(home_score: Any, away_score: Any,
                         batting_team: int | None,
                         over_str: str | None,
                         total_balls: int = _NPL_TOTAL_BALLS) -> dict:
    """Chasing target for a limited-overs game: team 2 needs
    first-innings + 1. total_balls defaults to 120 (T20/NPL)."""
    out: dict[str, Any] = {"target": None, "need": None, "balls_left": None}
    if batting_team != 2:
        return out

    def _runs(v: Any) -> int | None:
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            try:
                return int(v.split("/")[0])
            except ValueError:
                return None
        return None

    first, second = _runs(home_score), _runs(away_score)
    if first is None or second is None:
        return out
    out["target"] = first + 1
    out["need"] = max(0, out["target"] - second)
    if over_str:
        try:
            ov, balls = str(over_str).split(".")
            bowled = int(ov) * 6 + int(balls)
            out["balls_left"] = max(0, total_balls - bowled)
        except ValueError:
            pass
    return out


def _onex_norm_npl_match(g: dict) -> Match | None:
    """A live NPL game from a games1x2 entry, with cricket detail."""
    m = _onex_norm_match(g)
    if m is None:
        return None
    scores = g.get("scores") or {}
    ext = scores.get("extendedScore") or {}
    wk = (ext.get("cricketScore") or {}).get("wicketScore") or {}
    if wk.get("opponent1") is not None:
        m["home_score"] = str(wk["opponent1"])
    if wk.get("opponent2") is not None:
        m["away_score"] = str(wk["opponent2"])
    inns = ext.get("innsStats") or {}
    team = inns.get("team") if isinstance(inns.get("team"), int) else None
    m["league"] = "Nepal Premier League"
    m["clock"] = _onex_cricket_clock(scores)
    m["cricket"] = {
        "is_break": scores.get("isBreak") is True,
        "team": team,
        "over_str": str(inns["overStr"]) if inns.get("overStr") is not None else None,
        **_onex_cricket_target(m["home_score"], m["away_score"], team,
                               inns.get("overStr")),
    }
    return m


def _onex_npl_from_games1x2(data: Any) -> list[Match]:
    """Pure filter: live NPL games out of a games1x2 list."""
    if not isinstance(data, list):
        return []
    out = []
    for g in data:
        liga = (g.get("liga") or {}).get("name") or ""
        if _NPL_LIGA_NAME not in normalize(liga):
            continue
        if (m := _onex_norm_npl_match(g)) is not None:
            out.append(m)
    return out


async def onex_npl_live() -> list[Match]:
    """Live NPL cricket from 1xBet only — no other source.

    Returns [] when no NPL match is live (or 1xBet carries no score
    for it). Never raises — a failed fetch is an empty list, so the
    scoreboard loop keeps running.
    """
    try:
        data = await hc.get(f"{_ONEX_FEED}/games1x2", _ONEX_HDR,
                            dict(_ONEX_GAMES1X2_PARAMS),
                            cache_key="", provider="onexbet")
        npl = _onex_npl_from_games1x2(data)
    except Exception as exc:
        log.warning("[onexbet] NPL games1x2 failed (%s)", type(exc).__name__)
        npl = []
    return npl


# ══════════════════════════════════════════════════════════════════════════
# 1xBet GAME SEARCH + DETAIL (for /game and /watch)
#
# Mirrors the site's Vercel proxy search (api/onexbet.js in
# aayush-tv-minter): live list (games1x2, 50 max) merged with the
# featured list (topGamesByStat), alias IDs resolved through
# gameInfo's mainGameId, detail enriched with live gameInfo
# (venue, weather, format, constId, line-up flags), stats from
# scores.tabloStats, cricket target from the scorecard.
# 1xBet ONLY — no other provider is touched here.
# ══════════════════════════════════════════════════════════════════════════

_ONEX_LINE_FEED = f"{_ONEX}/service-api/main-line-feed/v3"
_ONEX_NPL_CHAMP_ID = 2791573
_ONEX_NPL_MENU_PARAMS: dict[str, Any] = {
    "country": 130, "fcountry": 130, "gr": 909, "lng": "en", "ref": 1,
    "selectedMs": "1.66.2791573,2.66.2791573,10.66.2791573",
}
_ONEX_UEFA_MAX_CHAMPS = 3
_ONEX_SEARCH_MAX = 10

_TABLO_ORDER = [
    "Yellow cards", "Red card", "Corner", "Fouls",
    "Attacks", "Dangerous attacks", "Possession %",
    "Shots on target", "Shots off target", "xG", "Saves",
    "Penalty", "Substitutions", "Key Passes",
    "Passing Accuracy %", "Crosses",
]


def _onex_tabular_stats(scores: dict) -> list[dict] | None:
    """1xBet's own stats table (scores.tabloStats['0']).

    Cards, corners, attacks, possession, shots, xG, saves, subs...
    The list varies by game; fouls are not in it. None when the
    game carries no table (cricket never does).
    """
    t = (scores or {}).get("tabloStats")
    arr = None
    if isinstance(t, dict) and isinstance(t.get("0"), list):
        arr = t["0"]
    elif isinstance(t, list):
        arr = t
    if not arr:
        return None
    out = []
    for x in arr:
        if not isinstance(x, dict) or x.get("name") is None:
            continue
        out.append({
            "name": str(x["name"]),
            "home": "" if x.get("s1") is None else str(x["s1"]),
            "away": "" if x.get("s2") is None else str(x["s2"]),
        })
    if not out:
        return None
    order = {name: i for i, name in enumerate(_TABLO_ORDER)}
    out.sort(key=lambda s: order.get(s["name"], 99))
    return out


def _onex_cricket_run_rate(scores: dict) -> str | None:
    """Current-innings run rate from the scorecard JSON string
    (scores.statistic.main.InningsStatistic, "13,62" -> "13.62").
    Player names are NOT in this payload (null, only ids)."""
    try:
        main = (scores or {}).get("statistic") or {}
        main = main.get("main") or {}
        raw = main.get("InningsStatistic")
        if not raw:
            return None
        import json as _json
        data = _json.loads(raw)
        st = data.get("St") or []
        rr = (st[0].get("RR") if st else None) or ""
        rr = str(rr).replace(",", ".")
        return rr or None
    except Exception:
        return None


def _onex_cricket_raw(scores: dict) -> dict:
    ext = (scores or {}).get("extendedScore") or {}
    inns = ext.get("innsStats") or {}
    return {
        "is_break": bool((scores or {}).get("isBreak")),
        "team": inns.get("team") if isinstance(inns.get("team"), int) else None,
        "over_str": str(inns["overStr"]) if inns.get("overStr") is not None else None,
        "run_rate": _onex_cricket_run_rate(scores),
    }


def _onex_format_balls(fmt: str) -> int | None:
    f = str(fmt or "")
    if "T20" in f:
        return 120
    if "T10" in f:
        return 60
    if "ODI" in f or "One Day" in f:
        return 300
    if "Hundred" in f:
        return 100
    return None


def _onex_cricket_target_fmt(fmt: str, home_score: Any, away_score: Any,
                             batting_team: int | None,
                             over_str: str | None) -> dict:
    """Chasing target for a limited-overs game, format-aware.
    Tests have no fixed target this way, so non-limited formats
    return all-None."""
    out: dict[str, Any] = {"target": None, "need": None, "balls_left": None}
    total = _onex_format_balls(fmt)
    if total is None or batting_team != 2:
        return out
    return _onex_cricket_target(home_score, away_score, batting_team,
                                over_str, total)


def _onex_apply_cricket(m: dict, scores: dict) -> None:
    """Override football scores with the cricket wicketScore and
    set the overs clock + cricket detail on a normalized match."""
    ext = (scores or {}).get("extendedScore") or {}
    wk = (ext.get("cricketScore") or {}).get("wicketScore") or {}
    if wk.get("opponent1") is not None:
        m["home_score"] = str(wk["opponent1"])
    if wk.get("opponent2") is not None:
        m["away_score"] = str(wk["opponent2"])
    m["clock"] = _onex_cricket_clock(scores)
    old = m.get("cricket") or {}
    m["cricket"] = {**_onex_cricket_raw(scores),
                    "target": old.get("target"),
                    "need": old.get("need"),
                    "balls_left": old.get("balls_left")}


def format_mmss(seconds: int | float | None) -> str | None:
    """Seconds since kick-off -> 'MM:SS', e.g. 4354 -> '72:34'."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _onex_detail_from_raw(g: dict) -> dict | None:
    """Rich detail dict from one raw live-list entry (no network).
    Score, clock/overs, stats table, cricket detail. Venue/format/
    weather come from gameInfo via onex_game_detail()."""
    liga_name = ((g.get("liga") or {}).get("name") or "")
    if "nepal" in normalize(liga_name):
        m = _onex_norm_npl_match(g)
    else:
        m = _onex_norm_match(g)
    if m is None:
        return None
    scores = g.get("scores") or {}
    ext = scores.get("extendedScore") or {}
    if (ext.get("cricketScore") or {}).get("wicketScore") is not None:
        _onex_apply_cricket(m, scores)
        if "nepal" not in normalize(liga_name):
            # Non-NPL cricket: target needs the format, which only
            # gameInfo knows — filled in by onex_game_detail().
            m["cricket"].update({"target": None, "need": None,
                                "balls_left": None})
    # Sport, in 1xBet's own words ("Football", "Cricket", ...):
    # the live list carries it on every entry. Older/trimmed
    # shapes have no sport key — a cricket score shape is
    # unambiguous, anything else stays unknown (never guessed).
    sport = (g.get("sport") or {}).get("name") if isinstance(
        g.get("sport"), dict) else None
    if not sport and (ext.get("cricketScore") or {}).get(
            "wicketScore") is not None:
        sport = "Cricket"
    m["sport"] = sport or None
    m["stats"] = _onex_tabular_stats(scores)
    timer = scores.get("timer") or {}
    m["timer_sec"] = timer.get("timeSec") if isinstance(
        timer.get("timeSec"), (int, float)) else None
    if not m.get("clock"):
        # Football entries carry no clock string — build MM:SS from
        # the timer while the game is live. HT/FT get a label, never
        # a fake clock; no timer means no clock at all. (Cricket
        # already sets its overs clock above.)
        status = str(m.get("status") or "")
        if status in ("1H", "2H", "ET", "PEN"):
            m["clock"] = format_mmss(m["timer_sec"]) or ""
        elif status == "HT":
            m["clock"] = "Half-time"
        elif status == "FT":
            m["clock"] = "Full-time"
    m["start_ts"] = g.get("startTs") if isinstance(
        g.get("startTs"), (int, float)) else None
    info = g.get("matchInfoObj") or {}
    m["venue"] = info.get("location") or ""
    m["format"] = info.get("matchFormat") or ""
    return m


async def onex_game_info(game_id: int | str) -> dict:
    """Live-feed gameInfo: venue, weather, format, constId,
    line-up/video flags. No scores, no odds — those come from
    the live list. {} when 1xBet doesn't answer (204/400)."""
    try:
        params = dict(_ONEX_GAME_PARAMS)
        params["gameId"] = game_id
        data = await hc.get(f"{_ONEX_FEED}/gameInfo", _ONEX_HDR, params,
                            cache_key="", provider="onexbet")
    except Exception as exc:
        log.warning("[onexbet] gameInfo %s failed (%s)", game_id,
                    type(exc).__name__)
        return {}
    if not isinstance(data, dict) or not data.get("id"):
        return {}
    mi = data.get("matchInfoObj") or {}
    weather = None
    if mi.get("weatherDescription"):
        weather = mi["weatherDescription"] + (
            f" · {mi['temperature']}" if mi.get("temperature") else "")
    return {
        "const_id": (str(data["constId"]) if data.get("constId") is not None
                    else str(data["mainConstId"]) if data.get("mainConstId") is not None
                    else None),
        "sport": ((data.get("sport") or {}).get("name") or None),
        "venue": mi.get("location") or None,
        "format": mi.get("matchFormat") or None,
        "weather": weather,
        "has_lineups": bool((data.get("statisticInfo") or {}).get("hasLineUps")),
        "has_stadium": bool((data.get("statisticInfo") or {}).get("hasStadiumInfo")),
        "video": bool((data.get("video") or {}).get("enabled")),
        "main_game_id": (str(data["mainGameId"]) if data.get("mainGameId") is not None
                        else None),
        "start_ts": data.get("startTs") if isinstance(
            data.get("startTs"), (int, float)) else None,
    }


async def onex_game_detail(raw: dict) -> dict | None:
    """Full detail for one raw live entry: live scores/stats plus
    gameInfo enrichment. A failed gameInfo never fails the detail."""
    detail = _onex_detail_from_raw(raw)
    if detail is None:
        return None
    info = await onex_game_info(detail["onex_id"])
    if info:
        detail["const_id"] = info.get("const_id")
        detail["sport"] = info.get("sport") or detail.get("sport")
        if info.get("venue"):
            detail["venue"] = info["venue"]
        if info.get("format"):
            detail["format"] = info["format"]
        detail["weather"] = info.get("weather")
        detail["has_lineups"] = info.get("has_lineups", False)
        detail["has_stadium"] = info.get("has_stadium", False)
        detail["video"] = info.get("video", False)
        if info.get("start_ts"):
            detail["start_ts"] = info["start_ts"]
        cricket = detail.get("cricket")
        if cricket and detail.get("format"):
            cricket.update(_onex_cricket_target_fmt(
                detail["format"], detail.get("home_score"),
                detail.get("away_score"), cricket.get("team"),
                cricket.get("over_str")))
    return detail


# --- shared tick cache --------------------------------------------------
# The scoreboard loop fetches the live list every second
# (onex_fetch_tick). Command handlers (/game, /watch) need the SAME
# data — they must not re-fetch it. onex_live_raw() serves a copy
# younger than _ONEX_LIVE_TTL_S; fixtures change slowly, so they are
# cached for _ONEX_FIXTURES_TTL_S. Single event loop only — no locks
# needed. Only non-empty results are cached: on a failed/empty fetch
# the next call retries immediately, exactly like before, so the
# scoreboard's back-off still sees one empty tick per second.
_ONEX_LIVE_TTL_S = 3.0
_ONEX_FIXTURES_TTL_S = 60.0
_live_cache: dict[str, Any] = {"ts": 0.0, "raw": None}
_fix_cache: dict[str, tuple[float, list[dict] | None]] = {
    "npl": (0.0, None), "uefa": (0.0, None),
}


def _onex_reset_caches() -> None:
    """Clear the live + fixture caches (tests only)."""
    _live_cache["ts"] = 0.0
    _live_cache["raw"] = None
    _fix_cache["npl"] = (0.0, None)
    _fix_cache["uefa"] = (0.0, None)


async def onex_live_raw() -> list[dict]:
    """Merged raw live entries (games1x2 + topGamesByStat, deduped
    by id). Served from the shared tick cache when it is younger
    than _ONEX_LIVE_TTL_S — the scoreboard's 1-second loop refreshes
    it, so /game and /watch usually cost zero HTTP calls. Otherwise
    both lists are fetched in parallel and the cache is refreshed.
    Callers must not mutate the returned list (it is the cache)."""
    now = time.monotonic()
    cached = _live_cache["raw"]
    if cached is not None and now - _live_cache["ts"] < _ONEX_LIVE_TTL_S:
        return cached

    async def _one(url: str, params: dict) -> list | None:
        try:
            data = await hc.get(url, _ONEX_HDR, dict(params),
                                cache_key="", provider="onexbet")
        except Exception as exc:
            log.warning("[onexbet] %s failed (%s)",
                        url.rsplit("/", 1)[-1], type(exc).__name__)
            return None
        return data if isinstance(data, list) else None

    data1, data2 = await asyncio.gather(
        _one(f"{_ONEX_FEED}/games1x2", _ONEX_GAMES1X2_PARAMS),
        _one(f"{_ONEX_FEED}/topGamesByStat", _ONEX_TOP_PARAMS),
    )
    out: list[dict] = []
    seen: set[str] = set()
    for data in (data1, data2):
        if not isinstance(data, list):
            continue
        for g in data:
            gid = str(g.get("id") or g.get("mainGameId") or "")
            if not gid or gid in seen:
                continue
            seen.add(gid)
            out.append(g)
    if out:
        _live_cache["ts"] = time.monotonic()
        _live_cache["raw"] = out
    return out


def onex_last_success_ts() -> float:
    """Monotonic timestamp of the last successful live-feed fetch
    (0.0 if never) — for the health monitor's data-freshness check."""
    return _live_cache["ts"]


async def onex_fetch_tick() -> tuple[list[dict], list[dict], list[dict]]:
    """One watch tick's data, from exactly two 1xBet calls:
    (npl_matches, featured_matches, raw_live_entries). The
    scoreboard and the game watches share this tick — no second
    1-second poller."""
    raw = await onex_live_raw()
    npl = _onex_npl_from_games1x2(raw)
    featured: list[dict] = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        m = _onex_norm_match(g)
        if m is not None:
            featured.append(m)
    return npl, featured, raw


# --- line feed: fixtures (upcoming games, 1xBet only) ---------------

async def _onex_line_menu(selected_ms: str, referer: str) -> list | None:
    params = {"country": 130, "fcountry": 130, "gr": 909, "lng": "en",
              "ref": 1, "selectedMs": selected_ms}
    try:
        data = await hc.get(f"{_ONEX_LINE_FEED}/leftMenuSports", _ONEX_HDR,
                            params, cache_key="", provider="onexbet")
    except Exception as exc:
        log.warning("[onexbet] leftMenuSports failed (%s)", type(exc).__name__)
        return None
    return data if isinstance(data, list) else None


def _onex_find_liga_games(menu: list | None, champ_id: int) -> dict | None:
    if not isinstance(menu, list):
        return None
    for section in menu:
        for sport in (section or {}).get("sports") or []:
            for liga in (sport or {}).get("ligas") or []:
                if liga and liga.get("id") == champ_id:
                    games = liga.get("games")
                    return {"name": liga.get("name") or "Unknown",
                            "games": games if isinstance(games, list) else []}
    return None


def _onex_norm_line_game(g: dict, league: str, default_format: str = "",
                         sport: str | None = None) -> dict | None:
    """A line-feed game (fixture, or a live game still listed):
    status upcoming/live/FT from its scores or kickoff time.
    The caller knows the sport (NPL is cricket, UEFA is
    football) — the line feed groups games by sport, so it is
    passed in rather than guessed from the scores."""
    o1, o2 = g.get("opponent1") or {}, g.get("opponent2") or {}
    home, away = o1.get("fullName") or "", o2.get("fullName") or ""
    gid = g.get("id") or g.get("mainGameId")
    if not home or not away or not gid:
        return None
    scores = g.get("scores") or None
    start_ts = g.get("startTs") if isinstance(g.get("startTs"), (int, float)) else None
    import time as _time
    now_sec = int(_time.time())
    status, clock, minute = "upcoming", "", None
    if scores:
        st, minute = _onex_status(scores)
        clock = scores.get("statusLineStr") or ""
        status = "FT" if st == "FT" else "live"
    elif start_ts and start_ts <= now_sec:
        status, clock = "live", "Live"
    ck = _onex_cricket_clock(scores) if scores else None
    if ck:
        clock = ck
    info = g.get("matchInfoObj") or {}
    hs = scores.get("scoreOpp1") if scores else None
    aws = scores.get("scoreOpp2") if scores else None
    detail: dict[str, Any] = {
        "id": f"onex_{gid}", "onex_id": gid,
        "home": home, "away": away,
        "home_norm": normalize(home), "away_norm": normalize(away),
        "home_score": hs, "away_score": aws,
        "status": status, "status_code": 0, "minute": minute,
        "league": league, "league_id": 0, "sofa_lid": 0,
        "date": "", "stadium": info.get("location") or "",
        "referee": "", "provider": "onexbet",
        "finished": status == "FT",
        "sport": sport,
        "clock": clock, "start_ts": start_ts,
        "format": info.get("matchFormat") or default_format,
        "stats": _onex_tabular_stats(scores) if scores else None,
        "timer_sec": None,
        "cricket": None,
    }
    if scores and (scores.get("extendedScore") or {}).get("cricketScore"):
        _onex_apply_cricket(detail, scores)
        detail["cricket"].update({"target": None, "need": None,
                                 "balls_left": None})
    return detail


async def _onex_safe_fixtures(label: str, coro) -> list[dict]:
    """One fixture list, [] on failure — search/grounding carry on."""
    try:
        return await coro
    except Exception as exc:
        log.warning("[onexbet] %s fixtures failed (%s)", label,
                    type(exc).__name__)
        return []


async def onex_npl_fixtures() -> list[dict]:
    """NPL fixtures from the 1xBet line feed (champ 2791573).
    1xBet only — no official-API merge here. Cached ~60s: fixtures
    change slowly, and /game + /watch ask for them on every
    command."""
    now = time.monotonic()
    ts, cached = _fix_cache["npl"]
    if cached is not None and now - ts < _ONEX_FIXTURES_TTL_S:
        return cached
    menu = await _onex_line_menu("1.66.2791573,2.66.2791573,10.66.2791573",
                                 "/en/line/cricket/2791573-nepal-premier-league")
    found = _onex_find_liga_games(menu, _ONEX_NPL_CHAMP_ID)
    if not found:
        return []
    out = []
    for g in found["games"]:
        if (m := _onex_norm_line_game(g, "Nepal Premier League", "T20",
                                      "Cricket")) is not None:
            out.append(m)
    if out:
        _fix_cache["npl"] = (time.monotonic(), out)
    return out


async def onex_uefa_fixtures() -> list[dict]:
    """UEFA fixtures from the line feed, for the champs that have
    live games right now (champ ids discovered from games1x2 —
    served from the shared tick cache, not a fresh call).
    The line feed has no general search, so upcoming coverage is
    NPL + UEFA only. Cached ~60s, like the NPL fixtures."""
    now = time.monotonic()
    ts, cached = _fix_cache["uefa"]
    if cached is not None and now - ts < _ONEX_FIXTURES_TTL_S:
        return cached
    raw = await onex_live_raw()
    champ_ids: list[int] = []
    for g in raw:
        liga = g.get("liga") or {}
        if "uefa" in str(liga.get("name") or "").lower():
            cid = liga.get("id")
            if isinstance(cid, int) and cid not in champ_ids:
                champ_ids.append(cid)
    if not champ_ids:
        return []
    menus = await asyncio.gather(*[
        _onex_line_menu(f"1.1.{cid},2.1.{cid},10.1.{cid}",
                        "/en/line/football")
        for cid in champ_ids[:_ONEX_UEFA_MAX_CHAMPS]
    ])
    out: list[dict] = []
    for cid, menu in zip(champ_ids[:_ONEX_UEFA_MAX_CHAMPS], menus):
        found = _onex_find_liga_games(menu, cid)
        if not found:
            continue
        for g in found["games"]:
            if (m := _onex_norm_line_game(g, found["name"],
                                          sport="Football")) is not None:
                if not any(x["onex_id"] == m["onex_id"] for x in out):
                    out.append(m)
    if out:
        _fix_cache["uefa"] = (time.monotonic(), out)
    return out


async def onex_search(query: str) -> dict:
    """Find a game by 1xBet ID or by team/league name (1xBet only).
    Returns {"games": [...], "detail": {...} | None}.
    Numeric q: live list by id, alias resolved via gameInfo
    mainGameId, else a score-less gameInfo detail. Text q: live
    games plus NPL + UEFA fixtures, cap 10, deduped."""
    q = str(query or "").strip()
    if not q:
        return {"games": [], "detail": None}
    raw = await onex_live_raw()
    is_id = q.isdigit() and len(q) >= 5
    if is_id:
        for g in raw:
            if str(g.get("id") or g.get("mainGameId") or "") == q:
                return {"games": [], "detail": await onex_game_detail(g)}
        info = await onex_game_info(q)
        if info:
            main_id = info.get("main_game_id")
            if main_id and main_id != q:
                for g in raw:
                    if str(g.get("id") or g.get("mainGameId") or "") == main_id:
                        return {"games": [], "detail": await onex_game_detail(g)}
            detail: dict[str, Any] = {
                "id": f"onex_{q}", "onex_id": int(q),
                "home": "", "away": "", "home_norm": "", "away_norm": "",
                "home_score": None, "away_score": None,
                "status": "?", "status_code": 0, "minute": None,
                "league": "Unknown", "league_id": 0, "sofa_lid": 0,
                "date": "", "stadium": "", "referee": "",
                "provider": "onexbet", "finished": False,
                "clock": "Score not available right now",
                "start_ts": info.get("start_ts"),
                "format": info.get("format") or "",
                "venue": info.get("venue") or "",
                "weather": info.get("weather"),
                "const_id": info.get("const_id"),
                "sport": info.get("sport"),
                "has_lineups": info.get("has_lineups", False),
                "has_stadium": info.get("has_stadium", False),
                "video": info.get("video", False),
                "stats": None, "timer_sec": None, "cricket": None,
            }
            return {"games": [], "detail": detail}
        return {"games": [], "detail": None}
    needle = normalize(q)
    out: list[dict] = []

    def _push(m: dict | None) -> None:
        if m is None:
            return
        hay = " ".join([m.get("home") or "", m.get("away") or "",
                        m.get("league") or ""])
        if needle not in normalize(hay):
            return
        if any(x["onex_id"] == m["onex_id"] for x in out):
            return
        out.append(m)

    for g in raw:
        _push(_onex_detail_from_raw(g))
    if len(out) < _ONEX_SEARCH_MAX:
        # Both fixture lists at once; each is cached ~60s, so this
        # is usually free. A failed list is [] — search carries on.
        npl, uefa = await asyncio.gather(
            _onex_safe_fixtures("NPL", onex_npl_fixtures()),
            _onex_safe_fixtures("UEFA", onex_uefa_fixtures()),
        )
        for m in npl + uefa:
            _push(m)
    return {"games": out[:_ONEX_SEARCH_MAX], "detail": None}


async def onex_grounding() -> list[dict]:
    """Compact list of games the AI may resolve a watch to:
    live games + NPL/UEFA fixtures. The AI must pick an id from
    this list — never invent one."""
    raw = await onex_live_raw()
    out: list[dict] = []
    seen: set[str] = set()
    for g in raw:
        m = _onex_detail_from_raw(g)
        if m is None:
            continue
        gid = str(m["onex_id"])
        if gid in seen:
            continue
        seen.add(gid)
        out.append({"id": gid, "home": m["home"], "away": m["away"],
                    "league": m["league"], "sport": m.get("sport"),
                    "status": m["status"]})
    npl, uefa = await asyncio.gather(
        _onex_safe_fixtures("NPL", onex_npl_fixtures()),
        _onex_safe_fixtures("UEFA", onex_uefa_fixtures()),
    )
    for m in npl + uefa:
        gid = str(m["onex_id"])
        if gid in seen:
            continue
        seen.add(gid)
        out.append({"id": gid, "home": m["home"], "away": m["away"],
                    "league": m["league"], "sport": m.get("sport"),
                    "status": m["status"]})
    return out[:40]


# Statuses that mean "live right now" in _onex_status /
# currentPeriodName terms. FT and "?" are never live.
_ONEX_LIVE_STATUSES = ("1H", "2H", "ET", "PEN", "HT", "Half-time",
                       "1st innings", "2nd innings")


async def onex_top_live(limit: int = 5,
                        sport: str | None = None) -> list[dict]:
    """Top live games with score + clock, for pick lists and
    "did you mean" suggestions. Served from the shared tick
    cache via onex_live_raw — no extra 1xBet calls. With a
    sport, only games of that sport (1xBet's own name, e.g.
    "Football") — games whose sport is unknown are left out
    rather than guessed."""
    raw = await onex_live_raw()
    live = {s.lower() for s in _ONEX_LIVE_STATUSES}
    want = (sport or "").strip().lower() or None
    out: list[dict] = []
    seen: set[str] = set()
    for g in raw:
        m = _onex_detail_from_raw(g)
        if m is None:
            continue
        if str(m.get("status") or "").lower() not in live:
            continue
        if want and (str(m.get("sport") or "").strip().lower() != want):
            continue
        gid = str(m.get("onex_id"))
        if gid in seen:
            continue
        seen.add(gid)
        out.append(m)
        if len(out) >= limit:
            break
    return out
