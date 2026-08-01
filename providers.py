"""
Three free data providers, polled concurrently:
  - SofaScore    (unofficial, no key — session cookies via cloudscraper)
  - API-Football (official, needs APIFOOTBALL_API_KEY)
  - ESPN         (unofficial, no key, bundles events with the scoreboard —
                  usually the fastest since it needs only one HTTP call)
All public functions return normalized dicts with consistent field names.
Event fingerprinting uses a cross-provider semantic key (team + minute +
category) so the same real event from multiple providers collapses to one
notification — see canonical_fingerprint().
"""
import hashlib
import logging
import time
from typing import Any

import http_client as hc
import sofa_session
from config import APIFOOTBALL_API_KEY, CURRENT_SEASON, LEAGUE_MAP, LEAGUE_MAP_INV
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


def _sofa_norm_match(e: dict) -> Match:
    code   = e.get("status", {}).get("code", 0)
    stype  = e.get("status", {}).get("type", "")
    minute = None
    if stype == "inprogress":
        ts = (e.get("time") or {}).get("currentPeriodStartTimestamp")
        ini = (e.get("time") or {}).get("initial", 0)
        if ts:
            minute = min(int((time.time() - ts) / 60) + ini, 120)
    sofa_lid = e.get("tournament", {}).get("uniqueTournament", {}).get("id", 0)
    apf_lid  = LEAGUE_MAP_INV.get(sofa_lid, 0)
    return {
        "id":          f"sofa_{e['id']}",
        "sofa_id":     e["id"],
        "apf_id":      None,
        "home":        e["homeTeam"]["name"],
        "away":        e["awayTeam"]["name"],
        "home_norm":   normalize(e["homeTeam"]["name"]),
        "away_norm":   normalize(e["awayTeam"]["name"]),
        "home_score":  (e.get("homeScore") or {}).get("current"),
        "away_score":  (e.get("awayScore") or {}).get("current"),
        "home_logo":   f"https://api.sofascore.com/api/v1/team/{e['homeTeam']['id']}/image",
        "away_logo":   f"https://api.sofascore.com/api/v1/team/{e['awayTeam']['id']}/image",
        "status":      _SOFA_STATUS.get(code, e.get("status", {}).get("description", "?")),
        "status_code": code,
        "minute":      minute,
        "league":      e.get("tournament", {}).get("name", "Unknown"),
        "league_id":   apf_lid,
        "sofa_lid":    sofa_lid,
        "date":        "",
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
    return [_sofa_norm_match(e) for e in (data or {}).get("events", [])]


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
                results.append(_sofa_norm_match(e))
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
    return [_sofa_norm_match(e) for e in events[:10]]


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
    minute    = e["time"]["elapsed"]
    added     = e["time"].get("extra")
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


async def apf_lineups(fixture_id: int) -> list:
    data = await hc.get(f"{_APF}/fixtures/lineups", _APF_HDR,
                        {"fixture": fixture_id}, provider="apifootball")
    return (data or {}).get("response", [])


async def apf_standings(league_id: int) -> list:
    from cache import get as cget, set as cset
    from config import TTL_STANDINGS
    key = f"apf_stand_{league_id}"
    hit = cget(key)
    if hit is not None:
        return hit
    data = await hc.get(f"{_APF}/standings", _APF_HDR,
                        {"league": league_id, "season": CURRENT_SEASON},
                        provider="apifootball")
    result = (data or {}).get("response", [])
    cset(key, result, TTL_STANDINGS)
    return result


async def apf_team_fixtures(team_id: int, last: int = 5) -> list[Match]:
    data = await hc.get(f"{_APF}/fixtures", _APF_HDR,
                        {"team": team_id, "last": last}, provider="apifootball")
    return [_apf_norm(f) for f in (data or {}).get("response", [])]


async def apf_search_team(name: str) -> list:
    from cache import get as cget, set as cset
    from config import TTL_TEAM
    key = f"apf_team_{normalize(name)}"
    hit = cget(key)
    if hit is not None:
        return hit
    data = await hc.get(f"{_APF}/teams", _APF_HDR, {"search": name}, provider="apifootball")
    result = (data or {}).get("response", [])
    cset(key, result, TTL_TEAM)
    return result


async def apf_topscorers(league_id: int) -> list:
    from cache import get as cget, set as cset
    from config import TTL_TOPSCORER
    key = f"apf_top_{league_id}"
    hit = cget(key)
    if hit is not None:
        return hit
    data = await hc.get(f"{_APF}/players/topscorers", _APF_HDR,
                        {"league": league_id, "season": CURRENT_SEASON}, provider="apifootball")
    result = (data or {}).get("response", [])
    cset(key, result, TTL_TOPSCORER)
    return result


async def apf_injuries(team_id: int) -> list:
    from cache import get as cget, set as cset
    from config import TTL_INJURIES
    key = f"apf_inj_{team_id}"
    hit = cget(key)
    if hit is not None:
        return hit
    data = await hc.get(f"{_APF}/injuries", _APF_HDR,
                        {"team": team_id, "season": CURRENT_SEASON}, provider="apifootball")
    result = (data or {}).get("response", [])
    cset(key, result, TTL_INJURIES)
    return result


async def apf_h2h(t1: int, t2: int) -> list[Match]:
    from cache import get as cget, set as cset
    from config import TTL_H2H
    key = f"apf_h2h_{t1}_{t2}"
    hit = cget(key)
    if hit is not None:
        return hit
    data = await hc.get(f"{_APF}/fixtures/headtohead", _APF_HDR,
                        {"h2h": f"{t1}-{t2}", "last": 10}, provider="apifootball")
    result = [_apf_norm(f) for f in (data or {}).get("response", [])]
    cset(key, result, TTL_H2H)
    return result


async def apf_prediction(fixture_id: int) -> dict:
    data = await hc.get(f"{_APF}/predictions", _APF_HDR,
                        {"fixture": fixture_id}, provider="apifootball")
    resp = (data or {}).get("response", [])
    return resp[0] if resp else {}


async def apf_transfers(team_id: int) -> list:
    data = await hc.get(f"{_APF}/transfers", _APF_HDR,
                        {"team": team_id}, provider="apifootball")
    return (data or {}).get("response", [])


async def apf_player(player_id: int) -> dict:
    from cache import get as cget, set as cset
    from config import TTL_PLAYER
    key = f"apf_plyr_{player_id}"
    hit = cget(key)
    if hit is not None:
        return hit
    data = await hc.get(f"{_APF}/players", _APF_HDR,
                        {"id": player_id, "season": CURRENT_SEASON}, provider="apifootball")
    resp = (data or {}).get("response", [])
    result = resp[0] if resp else {}
    cset(key, result, TTL_PLAYER)
    return result


async def apf_today(league_id: int) -> list[Match]:
    from datetime import date
    data = await hc.get(f"{_APF}/fixtures", _APF_HDR,
                        {"league": league_id, "season": CURRENT_SEASON,
                         "date": date.today().isoformat()}, provider="apifootball")
    return [_apf_norm(f) for f in (data or {}).get("response", [])]


async def apf_tomorrow(league_id: int) -> list[Match]:
    from datetime import date, timedelta
    data = await hc.get(f"{_APF}/fixtures", _APF_HDR,
                        {"league": league_id, "season": CURRENT_SEASON,
                         "date": (date.today() + timedelta(days=1)).isoformat()}, provider="apifootball")
    return [_apf_norm(f) for f in (data or {}).get("response", [])]


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
        "league":      ev.get("shortName", ""),
        "league_id":   apf_lid,
        "sofa_lid":    0,
        "date":        (comp.get("date") or "")[:10],
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
