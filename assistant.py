"""AI Match Assistant — the /ask command (Feature 3).

One plain sentence does what /game, /watch, /unwatch and the
alert settings do separately: "follow the Gambia game",
"show me the highest scoring game live", "only tell me about
goals for Lumbini", "when do Barcelona play next".

Live data is the live-scores feed ONLY (via providers.onex_*).
Nothing is ever invented: a game id, team or channel the model
returns must exist in the live grounding list or the channel
list the coordinator passes in, or the request is refused
with a plain message. Fixtures are NOT invented either — the
only fixture answer is the next Barcelona match from the same
provider path /nextbarca already uses; any other team is told
plainly that its fixtures are not available.

Event kinds are exactly watch.EVENT_KINDS — there is NO
"second_yellow" or "var" kind in the engine (a second yellow
surfaces as the red-card count going up; VAR has no feed
signal), so those two are never offered or promised.

No secrets, no network beyond providers, no Discord sends
except the ephemeral /ask reply. The coordinator wires
handle_ask to the /ask slash command.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

import barcelona_reminder
import db
import providers as prov
import watch as wmod
from watch import DETAIL_ADVANCED, DETAIL_SIMPLE, EVENT_KINDS

log = logging.getLogger("assistant")

INTENTS = (
    "SEARCH_MATCH", "MATCH_DETAILS", "LIVE_MATCHES", "FOLLOW_MATCH",
    "UNFOLLOW_MATCH", "SET_ALERTS", "MATCH_TIMELINE", "TEAM_FIXTURES",
    "HELP",
)

# Gemini gets this long to parse one /ask sentence; on timeout the
# caller gets an error dict instead of a hanging reply. (wait_for
# cancels the wait, not the model call — the thread finishes on
# its own in the background.) Shorter than watch's 15s because
# /ask is interactive and the deterministic pre-checks already
# cover the common sentences.
AI_TIMEOUT_S = 3.0

# Statuses that mean "live right now" in the live feed's terms.
_LIVE_STATUSES = ("1h", "2h", "et", "pen", "ht", "half-time",
                  "1st innings", "2nd innings", "live")

# Friendly labels for the event kinds, for SET_ALERTS confirmations.
_EVENT_LABELS = {
    "goal": "goals", "wicket": "wickets", "yellow": "yellow cards",
    "red": "red cards", "penalty": "penalties", "sub": "substitutions",
    "kickoff": "kickoff", "ht": "half-time",
    "second_half": "second-half start", "ft": "full-time",
    "break": "innings break", "correction": "score corrections",
    "postponed": "postponements", "suspended": "suspensions",
    "abandoned": "abandonments", "cancelled": "cancellations",
}

_BARCA_NAMES = ("fc barcelona", "barcelona", "barça", "barca")


# ══════════════════════════════════════════════════════════════════════════
# PARSING
# ══════════════════════════════════════════════════════════════════════════

def _blank_parsed(error: str = "") -> dict:
    """The shape parse_intent always returns, with an error set
    when the request could not be understood."""
    return {"intent": None, "game_id": None, "team": None,
            "events": None, "detail": None, "channel": None,
            "note": "", "pick": None, "mass": False, "error": error}


def _norm(s: str) -> str:
    return prov.normalize(s or "")


_HELP_WORDS = ("help", "what can you do", "what can i ask", "commands",
               "how do i use", "how to use", "what does this do")
_LIVE_WORDS = ("what games are live", "show live matches",
               "what matches are live", "what is live right now",
               "whats live right now", "what's live right now",
               "show me live games", "show live games",
               "list live games", "live games right now",
               "what games are on", "show me whats live",
               "show me what's live", "show me all live",
               "live football", "live cricket")
_MOST_GOALS_WORDS = ("most goals", "highest scoring", "most goals so far",
                     "highest-scoring", "most goals today")
_MASS_WORDS = ("every live match", "all live matches",
               "every game live", "all the live games",
               "every match live", "follow all", "watch all",
               "every live game", "all live games")


def _precheck(text: str) -> dict | None:
    """Deterministic answers that need no model call. None when
    the sentence needs Gemini."""
    n = _norm(text or "")
    if not n:
        return _blank_parsed("Tell me what you want — a team, a game, "
                             "or 'help' to see what I can do.")
    if any(w in n for w in _HELP_WORDS):
        return {**_blank_parsed(), "intent": "HELP"}
    # "Follow every live match" must never mass-follow — the
    # handler answers with a pick list instead.
    if ("follow" in n or "watch" in n) and any(w in n for w in _MASS_WORDS):
        return {**_blank_parsed(), "intent": "FOLLOW_MATCH",
                "mass": True,
                "note": "Asked to follow every live match."}
    if any(w in n for w in _MOST_GOALS_WORDS):
        return {**_blank_parsed(), "intent": "MATCH_DETAILS",
                "pick": "highest_scoring",
                "note": "Highest scoring live game."}
    if not any(w in n for w in ("follow", "watch", "unfollow", "alert",
                                "detail", "timeline", "fixture")):
        if any(w in n for w in _LIVE_WORDS):
            return {**_blank_parsed(), "intent": "LIVE_MATCHES"}
    return None


_AI_PROMPT = """You parse a Discord user's request about a live sports game into STRICT JSON only — no other text, no markdown, no explanation:
{"intent": "<one of SEARCH_MATCH, MATCH_DETAILS, LIVE_MATCHES, FOLLOW_MATCH, UNFOLLOW_MATCH, SET_ALERTS, MATCH_TIMELINE, TEAM_FIXTURES, HELP>", "game_id": "<digits the user typed, or null>", "team": "<team name the user typed, or null>", "events": ["<event kind>", ...] or null, "detail": "simple|advanced" or null, "channel": "<channel name or id the user typed, or null>", "note": "<one short line>"}
Rules:
- intent is FOLLOW_MATCH when the user wants alerts/updates for a game, UNFOLLOW_MATCH when they want to stop following it, MATCH_DETAILS when they only want to see it now, SEARCH_MATCH when they want to find a game, LIVE_MATCHES when they want to browse what is live, SET_ALERTS when they want to change which alerts they get, MATCH_TIMELINE when they want the goal/card events so far, TEAM_FIXTURES when they ask when a team plays next, HELP when they ask what you can do.
- game_id is ONLY digits the user actually typed. Never invent an id — use null and put the team name in "team" instead.
- team is the team name as the user typed it, or null.
- events uses ONLY these kinds: goal, wicket, yellow, red, penalty, sub, kickoff, ht, second_half, ft, break, correction, postponed, suspended, abandoned, cancelled. There is no second-yellow or VAR kind — never use them. null when the user names no events.
- detail is "advanced" only when the user asks for full/advanced/detailed stats; "simple" when they ask for simple/basic/just goals; otherwise null.
- channel is the channel name or id the user typed, or null. If the user names a channel that is not in the channels list, still return the name as typed.
- note is one short line saying what you understood.

Channels (id | #name):
__CHANNELS__

User request: "__TEXT__"
"""


async def parse_intent(text: str, model, channels: list[dict] | None = None) -> dict:
    """Parse one /ask sentence. Deterministic pre-checks first
    (HELP, live list, highest-scoring pick, mass-follow) — those
    never call the model. Everything else goes to Gemini as
    STRICT JSON with a 3s timeout, same style as
    watch.resolve_watch. Never raises for bad input: an
    unparseable request returns a dict with an "error" key."""
    pre = _precheck(text)
    if pre is not None:
        return pre
    if model is None:
        return _blank_parsed(
            "I couldn't understand that without the assistant — "
            "try /game <team> or /watch <team> instead.")
    chans = ("\n".join(f"{c['id']} | #{c.get('name', '')}"
                       for c in (channels or []))
             if channels else "(none — use null)")
    prompt = (_AI_PROMPT.replace("__CHANNELS__", chans)
              .replace("__TEXT__", (text or "").strip()))
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(model.generate_content, prompt),
            timeout=AI_TIMEOUT_S)
        raw = (getattr(response, "text", "") or "").strip()
    except asyncio.TimeoutError:
        log.warning("[assistant] Gemini parse timed out (%ss)", AI_TIMEOUT_S)
        return _blank_parsed(
            "That took too long to understand — please try again, "
            "or use /game <team> instead.")
    except Exception as exc:
        log.warning("[assistant] Gemini parse failed (%s)", type(exc).__name__)
        return _blank_parsed(
            "I couldn't understand that right now — please try again, "
            "or use /game <team> instead.")
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return _blank_parsed(
            "I couldn't understand that — try naming a team, "
            "or say 'help' to see what I can do.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return _blank_parsed(
            "I couldn't understand that — try naming a team, "
            "or say 'help' to see what I can do.")
    if not isinstance(data, dict):
        return _blank_parsed("I couldn't understand that — try naming a team.")
    intent = data.get("intent")
    if intent not in INTENTS:
        return _blank_parsed(
            "I couldn't tell what you wanted — try 'help' to see "
            "what I can do.")
    detail = data.get("detail")
    if detail is not None and detail not in (DETAIL_SIMPLE, DETAIL_ADVANCED):
        detail = None
    events = data.get("events")
    if events is not None:
        if not isinstance(events, list):
            events = None
        else:
            events = [str(e) for e in events]
    gid = data.get("game_id")
    gid = str(gid) if gid is not None and str(gid).strip() else None
    team = data.get("team")
    team = str(team).strip() if isinstance(team, str) and team.strip() else None
    channel = data.get("channel")
    channel = (str(channel).strip() if channel is not None
               and str(channel).strip() else None)
    return {"intent": intent, "game_id": gid, "team": team,
            "events": events, "detail": detail, "channel": channel,
            "note": str(data.get("note") or ""), "pick": None,
            "mass": False, "error": ""}


# ══════════════════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════════════════

def _live_id_map(live_games: list[dict]) -> dict[str, dict]:
    """Live game id (str) -> entry. Accepts raw live-feed entries
    (id/mainGameId) and normalized matches (onex_id/id)."""
    out: dict[str, dict] = {}
    for g in live_games or []:
        if not isinstance(g, dict):
            continue
        raw_id = (g.get("onex_id") or g.get("id")
                  or g.get("mainGameId") or "")
        gid = str(raw_id).replace("onex_", "")
        if gid:
            out[gid] = g
    return out


def _team_names(entry: dict) -> tuple[str, str]:
    return str(entry.get("home") or ""), str(entry.get("away") or "")


def _match_team(team: str, live_games: list[dict]) -> list[dict]:
    """Live entries involving the named team: exact name first,
    then unique partial match — same rule as watch.resolve_watch."""
    needle = _norm(team)
    if not needle:
        return []
    exact, partial = [], []
    for g in live_games or []:
        home, away = _team_names(g)
        if _norm(home) == needle or _norm(away) == needle:
            exact.append(g)
        elif needle in _norm(f"{home} {away}"):
            partial.append(g)
    return exact or partial


def _is_barca_team(team: str | None) -> bool:
    return bool(team) and _norm(team) in _BARCA_NAMES


def _score_total(entry: dict) -> int | None:
    """Goals (football) in a live entry, or None when it has no
    numeric score. Cricket "120/2" counts runs, so callers that
    want goals filter to football first."""
    total = 0
    for key in ("home_score", "away_score"):
        value = entry.get(key)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            total += int(value)
        elif isinstance(value, str) and value.strip().isdigit():
            total += int(value.strip())
        else:
            return None
    return total


def _highest_scoring(live_games: list[dict]) -> dict | None:
    """The live game with the most goals. Football only when the
    entries carry a sport (cricket runs are not goals); ties go
    to the first listed. None when no live game has a score."""
    entries = [g for g in live_games or [] if isinstance(g, dict)]
    football = [g for g in entries
                if str((g.get("sport") or "")).strip().lower() == "football"]
    pool = football or entries
    best, best_total = None, -1
    for g in pool:
        total = _score_total(g)
        if total is None:
            continue
        if total > best_total:
            best, best_total = g, total
    return best


def _resolve_channel(name_or_id: str | None,
                     channels: list[dict] | None) -> dict | None:
    """A channel name ("general", "#general") or id -> the channel
    dict from the list, or None."""
    if not name_or_id:
        return None
    needle = str(name_or_id).strip().lstrip("#")
    for ch in channels or []:
        if str(ch.get("id")) == needle:
            return ch
        if _norm(str(ch.get("name", ""))) == _norm(needle):
            return ch
    return None


async def validate_intent(parsed: dict, live_games: list[dict],
                          channels: list[dict] | None = None) -> dict:
    """Strict validation against the live grounding list. Never
    invents a game id, score or fixture: an id outside the live
    list is checked once as an alias via gameInfo's mainGameId,
    and rejected when that fails too. Returns {"ok": True, ...}
    with the resolved game_id / channel dict, or
    {"ok": False, "error": "<plain message>"}. Never raises."""
    try:
        return await _validate_intent(parsed, live_games, channels)
    except Exception:
        # Full traceback, not just the type: a validation failure
        # in production is otherwise undiagnosable from the journal.
        log.exception("[assistant] validation failed")
        return {"ok": False, "error": "Something went wrong checking "
                                      "that — please try again."}


async def _validate_intent(parsed: dict, live_games: list[dict],
                           channels: list[dict] | None) -> dict:
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "I couldn't understand that — "
                                      "try naming a team."}
    if parsed.get("error"):
        return {"ok": False, "error": str(parsed["error"])}
    intent = parsed.get("intent")
    if intent not in INTENTS:
        return {"ok": False, "error": "I couldn't tell what you wanted — "
                                      "try 'help' to see what I can do."}
    if intent in ("HELP", "LIVE_MATCHES"):
        return {"ok": True, "intent": intent, "game_id": None,
                "team": None, "events": None,
                "detail": parsed.get("detail"), "channel": None,
                "note": parsed.get("note") or "", "pick": None,
                "mass": False}
    events = parsed.get("events")
    if events is not None:
        bad = [e for e in events if e not in EVENT_KINDS]
        if bad:
            return {"ok": False,
                    "error": f"I don't have an alert kind called "
                             f"'{bad[0]}'. I can alert on: "
                             f"{', '.join(_EVENT_LABELS.values())}."}
    detail = parsed.get("detail")
    if detail is not None and detail not in (DETAIL_SIMPLE, DETAIL_ADVANCED):
        return {"ok": False, "error": "Detail must be simple or advanced."}
    channel = None
    if parsed.get("channel"):
        channel = _resolve_channel(parsed["channel"], channels)
        if channel is None:
            return {"ok": False,
                    "error": f"I couldn't find a channel called "
                             f"'{parsed['channel']}'."}
    live_map = _live_id_map(live_games)
    team = parsed.get("team")
    game_id = parsed.get("game_id")
    entry = None

    # Highest-scoring pick: resolved from the live list here, so
    # the handler never has to guess which game it means.
    if parsed.get("pick") == "highest_scoring":
        entry = _highest_scoring(live_games)
        if entry is None:
            return {"ok": False, "error": "No live game has a score "
                                          "right now."}
        game_id = None
        for gid, g in live_map.items():
            if g is entry:
                game_id = gid
                break
        if game_id is None:
            raw = (entry.get("onex_id") or entry.get("id") or "")
            game_id = str(raw).replace("onex_", "")
        return {"ok": True, "intent": intent, "game_id": game_id,
                "team": team, "events": events, "detail": detail,
                "channel": channel, "note": parsed.get("note") or "",
                "pick": "highest_scoring", "mass": False,
                "entry": entry}

    if intent == "TEAM_FIXTURES":
        if not team:
            return {"ok": False, "error": "Tell me which team's fixtures "
                                          "you want — for example, "
                                          "'when do Barcelona play next'."}
        if _is_barca_team(team):
            return {"ok": True, "intent": intent, "game_id": None,
                    "team": team, "events": events, "detail": detail,
                    "channel": channel, "note": parsed.get("note") or "",
                    "pick": None, "mass": False}
        # Any other team: validation passes the name through so
        # the handler can say plainly its fixtures are not
        # available — never an invented fixture.
        return {"ok": True, "intent": intent, "game_id": None,
                "team": team, "events": events, "detail": detail,
                "channel": channel, "note": parsed.get("note") or "",
                "pick": None, "mass": False}

    # Mass-follow: valid as a request, but the handler answers
    # with a pick list and starts no watch.
    if parsed.get("mass") and intent == "FOLLOW_MATCH":
        return {"ok": True, "intent": intent, "game_id": None,
                "team": None, "events": events, "detail": detail,
                "channel": channel, "note": parsed.get("note") or "",
                "pick": None, "mass": True}

    if intent == "SET_ALERTS" and not events and not detail:
        return {"ok": False, "error": "Tell me which alerts to change — "
                                      "for example, 'only goals' or "
                                      "'advanced alerts'."}

    # Resolve the game: id first, then team name.
    if game_id:
        game_id = str(game_id)
        entry = live_map.get(game_id)
        if entry is None:
            # Alias id? gameInfo's mainGameId resolves it to the
            # live game — anything else is rejected, never used.
            try:
                info = await prov.onex_game_info(game_id)
            except Exception:
                info = {}
            main_id = (info or {}).get("main_game_id")
            if main_id and str(main_id) in live_map:
                game_id = str(main_id)
                entry = live_map[game_id]
            else:
                return {"ok": False,
                        "error": f"I couldn't find a live game with "
                                 f"ID {game_id}."}
    elif team:
        matches = _match_team(team, live_games)
        if not matches:
            if intent == "SEARCH_MATCH":
                # Not an error: the handler answers "no live match
                # for X — here are the live games".
                return {"ok": True, "intent": intent, "game_id": None,
                        "team": team, "events": events, "detail": detail,
                        "channel": channel,
                        "note": parsed.get("note") or "", "pick": None,
                        "mass": False, "entry": None, "matches": []}
            if _is_barca_team(team):
                # Barcelona named but not live: allowed through so
                # the handler can say so instead of dead-ending.
                return {"ok": True, "intent": intent, "game_id": None,
                        "team": team, "events": events, "detail": detail,
                        "channel": channel,
                        "note": parsed.get("note") or "", "pick": None,
                        "mass": False, "entry": None}
            return {"ok": False,
                    "error": f"No live game for '{team}' right now."}
        if len(matches) > 1:
            if intent == "SEARCH_MATCH":
                # A search is allowed to match several games — the
                # handler shows them as a pick list.
                return {"ok": True, "intent": intent, "game_id": None,
                        "team": team, "events": events, "detail": detail,
                        "channel": channel,
                        "note": parsed.get("note") or "", "pick": None,
                        "mass": False, "entry": None, "matches": matches}
            names = ", ".join(
                f"{m.get('home')} vs {m.get('away')}" for m in matches[:5])
            return {"ok": False,
                    "error": f"More than one live game matches '{team}': "
                             f"{names}. Pick one by its game ID."}
        entry = matches[0]
        for gid, g in live_map.items():
            if g is entry:
                game_id = gid
                break
        if game_id is None:
            raw = (entry.get("onex_id") or entry.get("id") or "")
            game_id = str(raw).replace("onex_", "")
    else:
        return {"ok": False,
                "error": "Tell me which game — a team name or a game ID."}

    if intent in ("SEARCH_MATCH",) and game_id is None:
        return {"ok": False, "error": "Tell me which game to search for."}
    return {"ok": True, "intent": intent, "game_id": game_id,
            "team": team, "events": events, "detail": detail,
            "channel": channel, "note": parsed.get("note") or "",
            "pick": parsed.get("pick"), "mass": False, "entry": entry}


# ══════════════════════════════════════════════════════════════════════════
# GROUNDING + EXECUTION
# ══════════════════════════════════════════════════════════════════════════

def _grounding_entries(raw: list[dict]) -> list[dict]:
    """Raw live entries -> normalized matches (id, teams, league,
    sport, status, score, minute). Live games only. One malformed
    entry is skipped, never allowed to kill the whole reply
    (18 Nov 2026: /ask failed twice with 'Something went wrong'
    on a live list — a single bad entry must not do that)."""
    out: list[dict] = []
    seen: set[str] = set()
    for g in raw or []:
        try:
            m = prov._onex_detail_from_raw(g)
            if m is None:
                continue
            gid = str(m["onex_id"])
        except Exception:
            # One malformed entry — including one whose normalized
            # form is missing its id — is skipped, never allowed
            # to kill the whole reply.
            log.warning("[assistant] skipping malformed live entry",
                        exc_info=True)
            continue
        if gid in seen:
            continue
        seen.add(gid)
        out.append(m)
    return out


def _live_pool(text: str | None, live_now: list[dict]) -> tuple[list[dict], str | None]:
    """The live games the question actually asks about. A sport
    word in the question ("show me all live football") narrows
    the list to that sport instead of showing every sport —
    that is also what keeps the reply short."""
    sport = wmod._sport_from_words(text or "")
    if not sport:
        return live_now, None
    pool = [g for g in live_now
            if str(g.get("sport") or "").strip().lower() == sport.lower()]
    return pool, sport


def _is_live(m: dict) -> bool:
    return str(m.get("status") or "").lower() in _LIVE_STATUSES


def _live_line_list(games: list[dict], limit: int = 15,
                    budget: int = 1750) -> str:
    """Live games as a numbered list, capped so the whole reply
    stays under Discord's 2000-character message limit: lines
    are added until the budget runs out, then '…and N more'.
    A 15-game list of long cricket names can pass 2000 chars on
    its own — that is what made /ask fail twice on 18 Nov 2026
    ('Something went wrong' on retry, same live data)."""
    lines = []
    total = 0
    shown = 0
    for i, g in enumerate(games[:limit], 1):
        line = (f"{i}. {wmod.live_line(g)} · {g.get('league', '')} "
                f"· ID `{g.get('onex_id')}`")
        if lines and total + 1 + len(line) > budget:
            break
        lines.append(line)
        total += len(line) + 1
        shown += 1
    rest = len(games) - shown
    if rest > 0:
        lines.append(f"…and {rest} more live games.")
    return "\n".join(lines)


def _pick_list(games: list[dict], header: str) -> str:
    return (header + "\n"
            + wmod.render_top_games(games, header=""))


async def _next_barca_fixture() -> dict | None:
    """Barcelona's next match from the same provider path
    /nextbarca uses (free fixtures for LaLiga, league 140).
    None when the provider has no upcoming Barcelona fixture —
    the caller says so instead of inventing one."""
    now = datetime.now(barcelona_reminder.NEPAL)
    candidates = []
    for offset in range(9):
        day = (now.date() + timedelta(days=offset)).isoformat()
        try:
            fixtures = await prov.free_fixtures_for_day(140, day)
        except Exception as exc:
            log.warning("[assistant] Barca fixture lookup failed (%s)",
                        type(exc).__name__)
            return None
        for match in fixtures or []:
            if not barcelona_reminder._is_barcelona(match):
                continue
            kickoff = barcelona_reminder._kickoff(match)
            if kickoff and kickoff > now:
                candidates.append((kickoff, match))
    if not candidates:
        return None
    kickoff, match = min(candidates, key=lambda item: item[0])
    return {"kickoff": kickoff, "match": match}


_HELP_TEXT = (
    "Here's what you can ask me:\n"
    "• **Find a game** — “show me the Gambia game”\n"
    "• **See a game now** — “show me the Lumbini game in detail”\n"
    "• **What's live** — “what games are live?”\n"
    "• **Follow a game** — “follow the Gambia game, only goals”\n"
    "• **Stop following** — “stop watching the Gambia game”\n"
    "• **Change alerts** — “only tell me about goals and red cards for the Gambia game”\n"
    "• **Timeline** — “show the timeline for the Gambia game”\n"
    "• **Fixtures** — “when do Barcelona play next?”\n"
    "• **Highest scoring** — “show me the highest scoring game live”\n"
    "I only know live games from the live scores feed, and I only "
    "have fixtures for Barcelona — I won't guess a game or a fixture."
)


async def handle_ask(interaction, text: str, model,
                     channels: list[dict] | None = None) -> None:
    """The /ask command handler (the coordinator wires it to the
    slash command). Defers ephemerally, builds grounding from the
    live feed, parses + validates, then executes. Every reply is
    ephemeral. Never raises — a failure is a short error reply."""
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass
    try:
        await _handle_ask(interaction, text, model, channels)
    except Exception:
        # Log the full traceback: three production failures of this
        # handler (18 Nov 2026) left only an exception type in the
        # journal, which was not enough to find the cause. The
        # user-facing reply stays exactly the same.
        log.exception("[assistant] handle_ask failed")
        try:
            await interaction.followup.send(
                "Something went wrong — please try again.", ephemeral=True)
        except Exception:
            pass


def _split_message(text: str, limit: int = 1900) -> list[str]:
    """Split a reply into Discord-sized chunks (2000 chars max)
    on line boundaries; a single over-long line is cut hard.
    A reply that is too long raises on send and the whole answer
    is lost — split instead."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    out: list[str] = []
    for part in parts:
        while len(part) > limit:
            out.append(part[:limit])
            part = part[limit:]
        if part:
            out.append(part)
    return out or [""]


async def _send(interaction, content: str, view=None) -> None:
    parts = _split_message(content)
    for i, part in enumerate(parts):
        # The buttons belong with the last chunk — and real
        # discord.py (Webhook.send, used by followup.send) rejects
        # an explicit view=None with TypeError, so the argument is
        # omitted entirely when there is no view to attach. That
        # is what failed every /ask live-list question in
        # production on 18 Nov 2026 while the permissive fakes
        # passed.
        kwargs: dict = {"ephemeral": True}
        if i == len(parts) - 1 and view is not None:
            kwargs["view"] = view
        await interaction.followup.send(part, **kwargs)


async def _handle_ask(interaction, text: str, model,
                      channels: list[dict] | None) -> None:
    try:
        raw = await prov.onex_live_raw()
    except Exception as exc:
        log.warning("[assistant] live grounding failed (%s)",
                    type(exc).__name__)
        raw = []
    live_games = _grounding_entries(raw)
    live_now = [g for g in live_games if _is_live(g)]

    parsed = await parse_intent(text, model, channels)
    valid = await validate_intent(parsed, live_games, channels)
    if not valid.get("ok"):
        await _send(interaction, str(valid.get("error") or
                                     "I couldn't understand that."))
        return

    intent = valid["intent"]
    game_id = valid.get("game_id")
    entry = valid.get("entry")
    detail_level = valid.get("detail") or DETAIL_SIMPLE
    user_id = getattr(getattr(interaction, "user", None), "id", 0)
    own_channel_id = getattr(interaction, "channel_id", None)

    def _target_channel() -> int | None:
        if valid.get("channel"):
            return int(valid["channel"]["id"])
        return own_channel_id

    if intent == "HELP":
        await _send(interaction, _HELP_TEXT)
        return

    if intent == "LIVE_MATCHES":
        pool, sport = _live_pool(text, live_now)
        if sport and not pool:
            await _send(interaction,
                        f"No live {sport.lower()} right now.")
            return
        if not pool:
            await _send(interaction, "No games are live right now.")
            return
        await _send(interaction,
                    "⚽ **Live games right now — pick one:**\n"
                    + _live_line_list(pool, 15)
                    + "\n\nUse `/game <ID>` for the full detail, "
                      "or ask me to follow one.")
        return

    if intent == "SEARCH_MATCH":
        if valid.get("matches") is not None:
            matches = valid["matches"]
            if matches:
                await _send(interaction, _pick_list(
                    matches, f"Found {len(matches)} live game(s) "
                             f"for '{valid.get('team')}':"))
                return
            if live_now:
                pool, sport = _live_pool(text, live_now)
                if sport and not pool:
                    await _send(interaction,
                                f"No live match for '{valid.get('team')}' "
                                f"and no live {sport.lower()} right now.")
                    return
                await _send(interaction,
                            f"No live match for '{valid.get('team')}' "
                            f"— here are the live games right now:\n"
                            + _live_line_list(pool, 15))
                return
            await _send(interaction,
                        f"No live match for '{valid.get('team')}' "
                        f"and no games are live right now.")
            return
        needle = _norm(valid.get("team") or "")
        matches = []
        if game_id:
            matches = [g for g in live_games
                       if str(g.get("onex_id")) == str(game_id)]
        elif needle:
            matches = [g for g in live_games
                       if needle in _norm(f"{g.get('home', '')} "
                                          f"{g.get('away', '')} "
                                          f"{g.get('league', '')}")]
        if matches:
            await _send(interaction, _pick_list(
                matches, f"Found {len(matches)} live game(s) "
                         f"for '{valid.get('team') or game_id}':"))
            return
        if live_now:
            pool, sport = _live_pool(text, live_now)
            if sport and not pool:
                await _send(interaction,
                            f"No live match for '{valid.get('team') or game_id}' "
                            f"and no live {sport.lower()} right now.")
                return
            await _send(interaction,
                        f"No live match for '{valid.get('team') or game_id}' "
                        f"— here are the live games right now:\n"
                        + _live_line_list(pool, 15))
            return
        await _send(interaction,
                    f"No live match for '{valid.get('team') or game_id}' "
                    f"and no games are live right now.")
        return

    if intent == "MATCH_DETAILS":
        if not game_id:
            if _is_barca_team(valid.get("team")):
                await _send(interaction, "Barcelona are not live right now.")
                return
            await _send(interaction, "I couldn't find a live game for that.")
            return
        try:
            result = await prov.onex_search(str(game_id))
        except Exception as exc:
            log.warning("[assistant] detail fetch failed (%s)",
                        type(exc).__name__)
            result = {"detail": None}
        detail = (result or {}).get("detail")
        if detail is None:
            await _send(interaction, "That game is no longer live.")
            return
        await _send(interaction, wmod.render_detail(detail, detail_level),
                    view=wmod.WatchAlertView(str(game_id)))
        return

    if intent == "FOLLOW_MATCH":
        if valid.get("mass"):
            # Never mass-follow: offer the pick list instead.
            if not live_now:
                await _send(interaction, "No games are live right now, "
                                         "so there is nothing to follow.")
                return
            await _send(interaction,
                        "I don't follow every game at once — pick one "
                        "to follow:\n"
                        + _live_line_list(live_now, 15))
            return
        if not game_id or entry is None:
            if _is_barca_team(valid.get("team")):
                await _send(interaction, "Barcelona are not live right now, "
                                         "so there is nothing to follow.")
                return
            await _send(interaction, "I couldn't find a live game for that.")
            return
        channel_id = _target_channel()
        if channel_id is None:
            await _send(interaction, "I couldn't tell which channel to "
                                     "send alerts to.")
            return
        watch_entry = {"id": str(game_id), "home": entry.get("home", ""),
                       "away": entry.get("away", ""),
                       "league": entry.get("league", ""),
                       "status": entry.get("status", "")}
        started = await wmod.start_watch(channel_id, watch_entry,
                                         detail_level)
        if not started:
            await _send(interaction, "I couldn't start watching that game.")
            return
        where = (f" in <#{channel_id}>" if channel_id != own_channel_id
                 else "")
        await _send(interaction,
                    f"👀 Watching **{started['home']} vs {started['away']}**"
                    f"{where} — {detail_level} alerts, only when the game "
                    f"changes.")
        return

    if intent == "UNFOLLOW_MATCH":
        if not game_id:
            await _send(interaction, "I couldn't find a live game for that.")
            return
        channel_id = _target_channel()
        if channel_id is None:
            await _send(interaction, "I couldn't tell which channel that "
                                     "watch is in.")
            return
        # Unfollow stops the alerts; the live panel keeps ticking
        # in place while the game is live (same rule as the 🔕
        # button — not /unwatch, which fully removes the game).
        entry = await wmod.set_notify(channel_id, str(game_id), False)
        if entry:
            await _send(interaction,
                        f"🔕 Notifications off for **{entry['home']} vs "
                        f"{entry['away']}** — the live panel keeps ticking "
                        f"here while the game is live.")
        else:
            await _send(interaction,
                        "That game is not watched in this channel.")
        return

    if intent == "SET_ALERTS":
        if not game_id:
            await _send(interaction, "I couldn't find a live game for that.")
            return
        channel_id = _target_channel()
        if channel_id is None:
            await _send(interaction, "I couldn't tell which channel that "
                                     "watch is in.")
            return
        events = valid.get("events")
        detail = valid.get("detail")
        try:
            stored = await db.set_prefs(channel_id, str(game_id),
                                        events=events, detail=detail)
        except Exception as exc:
            log.warning("[assistant] set_prefs failed (%s)",
                        type(exc).__name__)
            await _send(interaction, "I couldn't save those alert settings.")
            return
        label = (f"{entry.get('home', '')} vs {entry.get('away', '')}"
                 if entry else f"game {game_id}")
        bits = []
        stored_events = stored.get("events") or []
        if stored_events:
            bits.append("alerts for " + ", ".join(
                _EVENT_LABELS.get(e, e) for e in stored_events))
        stored_detail = stored.get("detail") or ""
        if stored_detail:
            bits.append(f"{stored_detail} detail")
        if stored.get("paused"):
            bits.append("paused")
        changed = "; ".join(bits) if bits else "default alerts"
        await _send(interaction,
                    f"✅ Alerts updated for **{label}**: {changed}.")
        return

    if intent == "MATCH_TIMELINE":
        if not game_id or entry is None:
            await _send(interaction, "I couldn't find a live game for that.")
            return
        try:
            gid = int(game_id)
        except (TypeError, ValueError):
            await _send(interaction, "I couldn't read this game's timeline.")
            return
        try:
            events = await prov.onex_events(
                gid, entry.get("home", ""), entry.get("away", ""))
        except Exception as exc:
            log.warning("[assistant] timeline fetch failed (%s)",
                        type(exc).__name__)
            events = []
        if not events:
            await _send(interaction, "No timeline events yet for this game.")
            return
        lines = [f"**{entry.get('home', '?')} "
                 f"{wmod._score_str(entry)} {entry.get('away', '?')}** "
                 f"— timeline\n"]
        for ev in sorted(events, key=lambda e: e.get("minute_int") or 0):
            lines.append(f"{ev.get('icon', '')} **{ev.get('minute', '?')}'** "
                         f"{ev.get('detail', '')} — {ev.get('player', '')} "
                         f"({ev.get('team', '')})")
        await _send(interaction, "\n".join(lines)[:2000])
        return

    if intent == "TEAM_FIXTURES":
        team = valid.get("team") or ""
        if not _is_barca_team(team):
            await _send(interaction,
                        f"I don't have fixtures for {team} — I only have "
                        f"the next Barcelona fixture. Ask 'when do "
                        f"Barcelona play next?' for that.")
            return
        nxt = await _next_barca_fixture()
        if nxt is None:
            await _send(interaction,
                        "No upcoming Barcelona fixture is available from "
                        "the provider yet.")
            return
        kickoff, match = nxt["kickoff"], nxt["match"]
        local = kickoff.astimezone(barcelona_reminder.NEPAL)
        await _send(interaction,
                    f"🔵🔴 Next Barcelona match: "
                    f"**{match.get('home', 'TBD')} vs "
                    f"{match.get('away', 'TBD')}**\n"
                    f"Kickoff: <t:{int(kickoff.timestamp())}:F> "
                    f"({local.strftime('%I:%M %p')} Nepal time)")
        return

    await _send(interaction, "I couldn't understand that — try 'help' to "
                             "see what I can do.")
