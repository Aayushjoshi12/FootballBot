"""Per-game watch: /game detail, /watch alerts, AI resolution.

1xBet ONLY — every game, score and stat here comes from 1xBet's
service-api (via providers.onex_*). No other provider is touched.

WATCH vs EDIT, same rule as the scoreboard: the bot WATCHES 1xBet
every second (shared with the scoreboard loop — on_tick() is called
from scoreboard_loop with that tick's raw live entries, so there is
NO second 1-second poller) but it SENDS a message only when a
watched game actually changes: a goal, a wicket, a card, an innings
break, full-time. A score that only ticks over in cricket (runs
with no wicket) sends nothing.

AI resolution: /watch takes free text. The text is resolved
deterministically first (game ID, exact team name, unique partial
name); only if that fails does Gemini see it — grounded on the
live + upcoming games list, asked for STRICT JSON, and the game_id
it returns MUST be one from that list. A made-up id is never used.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import db
import providers as prov

log = logging.getLogger("watch")
NEPAL = ZoneInfo("Asia/Kathmandu")

DETAIL_SIMPLE = "simple"
DETAIL_ADVANCED = "advanced"

# A watched game that vanishes from the live list for this many
# 1-second ticks is treated as finished/removed (it can flicker
# out of the 50-game list while still live, so one tick is not
# enough to end a watch).
MISSING_TICKS_TO_END = 10

# Gemini gets this long to resolve free text; on timeout the
# caller falls back to the candidate list instead of hanging the
# /watch reply. (wait_for cancels the wait, not the model call —
# the thread finishes on its own in the background.)
AI_TIMEOUT_S = 15


# ══════════════════════════════════════════════════════════════════════════
# RENDERING (pure — no Discord, no network)
# ══════════════════════════════════════════════════════════════════════════

def _score_str(d: dict) -> str:
    hs, aws = d.get("home_score"), d.get("away_score")
    if hs is None and aws is None:
        return "vs"
    return f"{hs if hs is not None else '?'} – {aws if aws is not None else '?'}"


def _clock_str(d: dict) -> str:
    if d.get("clock"):
        return str(d["clock"])
    minute = d.get("minute")
    if isinstance(minute, int):
        return f"{minute}'"
    status = str(d.get("status") or "")
    if status == "HT":
        return "Half-time"
    if status == "FT":
        return "Full-time"
    if status in ("1H", "2H", "ET", "PEN"):
        return status
    return "Live"


def format_kickoff(start_ts: int | float | None) -> str:
    if not start_ts:
        return ""
    dt = datetime.fromtimestamp(start_ts, NEPAL)
    today = datetime.now(NEPAL).date()
    day = "Today" if dt.date() == today else dt.strftime("%a %d %b")
    return f"{day}, {dt.strftime('%-I:%M %p').lower()}"


def _stat_value(stats: list[dict] | None, name: str) -> tuple[str, str] | None:
    for s in stats or []:
        if s.get("name") == name:
            return (str(s.get("home", "")), str(s.get("away", "")))
    return None


def format_stats_table(stats: list[dict] | None) -> list[str]:
    """Full stats table, one line per stat: home — name — away."""
    lines = []
    for s in stats or []:
        lines.append(f"{s.get('home', '')}  {s.get('name', '')}  {s.get('away', '')}")
    return lines


def render_detail(d: dict, level: str = DETAIL_SIMPLE) -> str:
    """One game's detail. Simple: score, clock, venue/weather/
    format/kickoff, game ID, goals + cards summary. Advanced:
    everything simple has + the full stats table and the cricket
    target/need/balls-left/run-rate."""
    lines = [f"**{d.get('home', '?')} {_score_str(d)} {d.get('away', '?')}**"]
    if d.get("league"):
        lines.append(str(d["league"]))
    lines.append("")
    lines.append(_clock_str(d))
    if d.get("sport"):
        lines.append(f"Sport: {d['sport']}")
    if d.get("venue"):
        lines.append(f"Venue: {d['venue']}")
    if d.get("format"):
        lines.append(f"Format: {d['format']}")
    if d.get("weather"):
        lines.append(f"Weather: {d['weather']}")
    kickoff = format_kickoff(d.get("start_ts"))
    if kickoff:
        lines.append(f"Kickoff: {kickoff}")
    gid = str(d.get("onex_id") or d.get("id") or "?")
    const = d.get("const_id")
    lines.append(f"Game ID: {gid}" + (f" · {const}" if const else ""))
    if d.get("video"):
        lines.append("Video: Live stream available")
    if d.get("has_lineups"):
        lines.append("Line-ups: Available")
    stats = d.get("stats")
    yellow = _stat_value(stats, "Yellow cards")
    red = _stat_value(stats, "Red card")
    if yellow or red:
        cards = []
        if yellow:
            cards.append(f"🟨 {yellow[0]}–{yellow[1]}")
        if red:
            cards.append(f"🟥 {red[0]}–{red[1]}")
        lines.append("Cards: " + "  ".join(cards))
    if level == DETAIL_ADVANCED:
        cricket = d.get("cricket") or {}
        if cricket.get("target") is not None:
            lines.append(f"Target: {cricket['target']}")
            need = cricket.get("need")
            balls = cricket.get("balls_left")
            if need is not None:
                lines.append(
                    f"Need: {need} off {balls} balls" if balls is not None
                    else f"Need: {need} runs")
        if cricket.get("run_rate"):
            lines.append(f"Run rate: {cricket['run_rate']}")
        table = format_stats_table(stats)
        if table:
            lines.append("")
            lines.append("**Match stats**")
            lines.extend(table)
    return "\n".join(lines)


def render_match_list(games: list[dict]) -> str:
    """Numbered pick list when a name matches several games."""
    lines = ["Found more than one game — pick one by its ID:\n"]
    for i, g in enumerate(games, 1):
        clock = _clock_str(g)
        lines.append(
            f"{i}. **{g.get('home', '?')} {_score_str(g)} {g.get('away', '?')}**"
            f" · {clock} · {g.get('league', '')} · ID `{g.get('onex_id')}`")
    lines.append("\nUse `/game <ID>` for the full detail, or `/watch <ID>`.")
    return "\n".join(lines)


def render_top_games(games: list[dict],
                     header: str = "⚽ **Live games right now — pick one:**") -> str:
    """Top live games as a pick list — when the user asks to see
    games, or as suggestions after a search found nothing.
    Same line format as render_match_list (score, clock, league,
    game ID); pass header=\"\" when the caller's own sentence is
    already the header."""
    lines = ([header] if header else [])
    for i, g in enumerate(games, 1):
        clock = _clock_str(g)
        lines.append(
            f"{i}. **{g.get('home', '?')} {_score_str(g)} {g.get('away', '?')}**"
            f" · {clock} · {g.get('league', '')} · ID `{g.get('onex_id')}`")
    lines.append("\nUse `/game <ID>` for the full detail, or `/watch <ID>` to follow one.")
    return "\n".join(lines)


async def top_live_games(limit: int = 5,
                         sport: str | None = None) -> list[dict]:
    """Top live games with score + clock, for pick lists and
    suggestions. Never raises — a failed fetch is an empty list.
    With a sport, only that sport's games."""
    try:
        return await prov.onex_top_live(limit, sport=sport)
    except Exception as exc:
        log.warning("[watch] top live games failed (%s)", type(exc).__name__)
        return []


# ══════════════════════════════════════════════════════════════════════════
# SNAPSHOT + CHANGE DETECTION (pure)
# ══════════════════════════════════════════════════════════════════════════

def _wickets(score: object) -> int | None:
    if score is None:
        return None
    parts = str(score).split("/")
    if len(parts) != 2:
        return 0 if str(score).strip() != "" else None
    try:
        return int(parts[1])
    except ValueError:
        return None


def snapshot_from_detail(d: dict) -> dict:
    stats = {}
    for s in d.get("stats") or []:
        stats[str(s.get("name"))] = (str(s.get("home", "")),
                                     str(s.get("away", "")))
    cricket = d.get("cricket") or {}
    clock = str(d.get("clock") or "")
    timer_sec = d.get("timer_sec")
    if not clock and isinstance(timer_sec, (int, float)) \
            and str(d.get("status") or "") in ("1H", "2H", "ET", "PEN"):
        # Football detail with a timer but no clock string — the
        # event lines need MM:SS, not a bare minute.
        clock = prov.format_mmss(timer_sec) or ""
    return {
        "home_score": "" if d.get("home_score") is None else str(d["home_score"]),
        "away_score": "" if d.get("away_score") is None else str(d["away_score"]),
        "status": str(d.get("status") or ""),
        "minute": d.get("minute") if isinstance(d.get("minute"), int) else None,
        "clock": clock,
        "timer_sec": timer_sec if isinstance(timer_sec, (int, float)) else None,
        "is_break": bool(cricket.get("is_break")),
        "stats": stats,
        "cricket": {
            "team": cricket.get("team"),
            "over_str": cricket.get("over_str"),
            "target": cricket.get("target"),
            "need": cricket.get("need"),
            "balls_left": cricket.get("balls_left"),
            "run_rate": cricket.get("run_rate"),
        },
    }


def detect_changes(old: dict, new: dict, level: str = DETAIL_SIMPLE) -> list[str]:
    """Event lines for what changed between two snapshots.
    Nothing changed -> []. A cricket score that only moved in
    runs (no wicket) is NOT an event — it would spam every ball."""
    events: list[str] = []
    title = f"{new.get('_home', '?')} {new['home_score']}–{new['away_score']} {new.get('_away', '?')}"
    clock = new.get("clock") or ""
    suffix = f" ({clock})" if clock else ""

    old_status, new_status = old.get("status", ""), new.get("status", "")
    if old_status in ("upcoming", "", "?") and new_status == "live":
        events.append(f"▶ **Kickoff** — {title}{suffix}")
    if new_status == "HT" and old_status != "HT":
        events.append(f"⏸ **Half-time** — {title}{suffix}")
    if new_status == "FT" and old_status != "FT":
        events.append(f"⏱ **Full-time** — {title}")
        return events
    if new.get("is_break") and not old.get("is_break"):
        events.append(f"☕ **Innings break** — {title}{suffix}")
    if old.get("home_score") != new.get("home_score") or old.get("away_score") != new.get("away_score"):
        # First sighting ("" -> "0") is kickoff, not a goal.
        first = (old.get("home_score") in (None, "")
                 and old.get("away_score") in (None, ""))
        if first:
            return events
        old_wk = _wickets(old.get("home_score"))
        new_wk = _wickets(new.get("home_score"))
        old_wk2 = _wickets(old.get("away_score"))
        new_wk2 = _wickets(new.get("away_score"))
        if ((old_wk is not None and new_wk is not None and new_wk > old_wk)
                or (old_wk2 is not None and new_wk2 is not None and new_wk2 > old_wk2)):
            events.append(f"🏏 **WICKET** — {title}{suffix}")
        elif new.get("status") not in ("upcoming", "", "?"):
            # Football score change is a goal; cricket runs alone
            # (no wicket) are skipped above by the wicket check —
            # but a football score has no "/" so _wickets returns 0
            # for both sides and this branch fires correctly.
            if "/" not in (new.get("home_score") or "") and "/" not in (new.get("away_score") or ""):
                events.append(f"⚽ **GOAL** — {title}{suffix}")
    old_stats, new_stats = old.get("stats") or {}, new.get("stats") or {}
    for label, icon in (("Yellow cards", "🟨"), ("Red card", "🟥")):
        o = old_stats.get(label)
        n = new_stats.get(label)
        if o is None or n is None:
            continue
        try:
            oh, oa = int(o[0] or 0), int(o[1] or 0)
            nh, na = int(n[0] or 0), int(n[1] or 0)
        except ValueError:
            continue
        if nh > oh:
            events.append(f"{icon} **Yellow card** — {title}{suffix} (home {nh}, was {oh})" if label == "Yellow cards" else f"{icon} **RED CARD** — {title}{suffix} (home {nh}, was {oh})")
        if na > oa:
            events.append(f"{icon} **Yellow card** — {title}{suffix} (away {na}, was {oa})" if label == "Yellow cards" else f"{icon} **RED CARD** — {title}{suffix} (away {na}, was {oa})")
    if level == DETAIL_ADVANCED:
        for label, icon in (("Penalty", "🎯"), ("Substitutions", "🔄")):
            o = old_stats.get(label)
            n = new_stats.get(label)
            if o is None or n is None:
                continue
            try:
                oh, oa = int(o[0] or 0), int(o[1] or 0)
                nh, na = int(n[0] or 0), int(n[1] or 0)
            except ValueError:
                continue
            if nh > oh or na > oa:
                events.append(f"{icon} **{label}** — {title}{suffix} (now {nh}–{na}, was {oh}–{oa})")
    return events


# ══════════════════════════════════════════════════════════════════════════
# AI RESOLUTION
# ══════════════════════════════════════════════════════════════════════════

_AI_PROMPT = """You resolve a Discord user's request about a live sports game to ONE game from the list below.
Reply with STRICT JSON only — no other text, no markdown, no explanation:
{"game_id": "<id from the list, or null>", "action": "follow|unfollow|detail|list", "detail": "simple|advanced", "channel_id": "<id from the channels list, or null>", "sport": "<sport name or null>", "note": "<one short line>"}
Rules:
- game_id MUST be one of the ids in the games list. If no game matches the request, use null.
- action is "follow" when the user wants alerts/updates for a game, "unfollow" when they want to stop following it, "detail" when they only want to see it now.
- action is "list" when the user wants to browse/see the live games rather than follow one specific game (e.g. "show me a game", "what's live", "any game to test") — then game_id is null.
- sport is the sport the user names when they want to browse games of one sport (e.g. "show me football games" -> "Football", "any cricket live?" -> "Cricket") — use only a sport name from the games list, or null when the user names none. Never invent a sport name.
- detail is "advanced" only when the user asks for full/advanced/detailed stats; otherwise "simple".
- channel_id MUST be one of the ids in the channels list, or null when the user does not name a channel. If the user names a channel that is not in the list, use null.
- note is one short line saying what you understood, e.g. "Lumbini vs Sudurpaschim, NPL, alerts to #general".

Games (id | home vs away | league | sport | status):
__GAMES__

Channels (id | #name):
__CHANNELS__

User request: "__TEXT__"
"""


async def parse_watch_ai(text: str, grounding: list[dict],
                         model, channels: list[dict] | None = None) -> dict | None:
    """Ask Gemini to resolve free text to a game (and, when the
    user names one, a channel). model is the configured Gemini
    model (or None). Returns the validated dict or None — a
    game_id outside the grounding list, or a channel_id outside
    the channel list, is rejected."""
    if model is None:
        return None
    games = "\n".join(
        f"{g['id']} | {g['home']} vs {g['away']} | {g['league']} | "
        f"{g.get('sport') or '?'} | {g['status']}"
        for g in grounding)
    if channels:
        chans = "\n".join(
            f"{c['id']} | #{c.get('name', '')}" for c in channels)
    else:
        chans = "(none — use null)"
    prompt = (_AI_PROMPT.replace("__GAMES__", games)
              .replace("__CHANNELS__", chans).replace("__TEXT__", text))
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(model.generate_content, prompt),
            timeout=AI_TIMEOUT_S)
        raw = (getattr(response, "text", "") or "").strip()
    except asyncio.TimeoutError:
        log.warning("[watch] Gemini resolve timed out (%ss) — using candidates",
                    AI_TIMEOUT_S)
        return None
    except Exception as exc:
        log.warning("[watch] Gemini resolve failed (%s)", type(exc).__name__)
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    gid = data.get("game_id")
    if gid is not None:
        gid = str(gid)
        if gid not in {str(g["id"]) for g in grounding}:
            log.warning("[watch] Gemini returned an id outside the grounding list")
            return None
    action = data.get("action")
    if action not in ("follow", "unfollow", "detail", "list"):
        action = "follow"
    detail = data.get("detail")
    if detail not in (DETAIL_SIMPLE, DETAIL_ADVANCED):
        detail = DETAIL_SIMPLE
    cid = data.get("channel_id")
    channel_id = None
    if cid is not None:
        cid = str(cid)
        if cid in {str(c["id"]) for c in (channels or [])}:
            channel_id = cid
        else:
            log.warning("[watch] Gemini returned a channel id "
                        "outside the channel list")
    return {"game_id": gid, "action": action, "detail": detail,
            "channel_id": channel_id,
            "sport": _canonical_sport(data.get("sport")),
            "note": str(data.get("note") or "")}


def _norm(s: str) -> str:
    return prov.normalize(s or "")


def _match_entries(text: str, grounding: list[dict]) -> list[dict]:
    """Grounding entries whose home/away/league contains the text."""
    needle = _norm(text)
    if not needle:
        return []
    out = []
    for g in grounding:
        hay = _norm(f"{g['home']} {g['away']} {g['league']}")
        if needle in hay:
            out.append(g)
    return out


# Words that pick the detail level without the AI — checked
# before Gemini, so "advanced stats" never waits on a model call.
_ADVANCED_WORDS = ("advanced", "detailed", "full stats", "all stats",
                   "everything", "full detail", "detailed stats")
_SIMPLE_WORDS = ("simple", "basic", "just goals", "only goals",
                 "just the goals", "goals only")


def _detail_from_words(text: str) -> str | None:
    """Detail level the user's own words ask for, or None."""
    n = _norm(text or "")
    if any(w in n for w in _ADVANCED_WORDS):
        return DETAIL_ADVANCED
    if any(w in n for w in _SIMPLE_WORDS):
        return DETAIL_SIMPLE
    return None


# User words -> the sport name the live data uses. Multi-word
# phrases first (longest first) so "football" never steals
# "american football", and "hockey" means ice hockey — the data
# carries no field hockey. Sports the data never carries
# (baseball, rugby, American football as a real sport) still
# map — the filter then honestly finds nothing live instead of
# guessing a different sport.
_SPORT_WORDS = (
    ("american football", "American Football"),
    ("beach volleyball", "Beach Volleyball"),
    ("basketball 3x3", "Basketball 3x3"),
    ("ice hockey", "Ice Hockey"),
    ("table tennis", "Table Tennis"),
    ("field hockey", "Field Hockey"),
    ("ping pong", "Table Tennis"),
    ("football", "Football"),
    ("soccer", "Football"),
    ("cricket", "Cricket"),
    ("basketball", "Basketball"),
    ("hockey", "Ice Hockey"),
    ("tennis", "Tennis"),
    ("volleyball", "Volleyball"),
    ("handball", "Handball"),
    ("badminton", "Badminton"),
    ("baseball", "Baseball"),
    ("rugby", "Rugby"),
    ("futsal", "Futsal"),
    ("golf", "Golf"),
    ("boxing", "Boxing"),
    ("snooker", "Snooker"),
    ("darts", "Darts"),
    ("floorball", "Floorball"),
    ("billiards", "Billiards"),
    ("athletics", "Athletics"),
    ("fifa", "FIFA"),
    ("esports", "Esports"),
)

_SPORT_EMOJI = {
    "Football": "⚽", "Cricket": "🏏", "Basketball": "🏀",
    "Tennis": "🎾", "Ice Hockey": "🏒", "Volleyball": "🏐",
    "Beach Volleyball": "🏐", "Table Tennis": "🏓",
    "Badminton": "🏸", "Handball": "🤾", "Baseball": "⚾",
    "Rugby": "🏉", "American Football": "🏈", "Golf": "⛳",
    "Boxing": "🥊", "Snooker": "🎱", "Billiards": "🎱",
    "Futsal": "⚽", "Athletics": "🏃", "Esports": "🎮",
    "Basketball 3x3": "🏀", "Field Hockey": "🏑", "FIFA": "🎮",
    "Darts": "🎯", "Floorball": "🏑",
}


def _sport_from_words(text: str) -> str | None:
    """The sport the user's words name, in the live data's own
    words — or None. Whole-word match, so "football" never
    matches inside "American football" (checked first) or a
    team name."""
    n = _norm(text or "")
    if not n:
        return None
    for phrase, sport in _SPORT_WORDS:
        if re.search(r"\b" + re.escape(phrase) + r"\b", n):
            return sport
    return None


def _canonical_sport(name: object) -> str | None:
    """An AI-returned sport name -> the canonical name the live
    data uses, or None when it is not a sport we know (never
    invented — an unknown sport means "no filter")."""
    if not isinstance(name, str) or not name.strip():
        return None
    n = _norm(name)
    for phrase, sport in _SPORT_WORDS:
        if n == _norm(phrase) or n == _norm(sport):
            return sport
    return None


def sport_list_header(sport: str | None) -> str:
    """Pick-list header: "⚽ Live football right now — pick one:"
    for a sport, the plain header when none."""
    if not sport:
        return "⚽ **Live games right now — pick one:**"
    return (f"{_SPORT_EMOJI.get(sport, '⚽')} **Live "
            f"{sport.lower()} right now — pick one:**")


def no_live_sport_message(sport: str) -> str:
    """Plain answer when no game of that sport is live — no
    other sport is offered in its place."""
    return f"No {sport.lower()} games live right now."


_SPORT_QUERY_FILLER = {"live", "games", "game", "matches", "match",
                       "any", "show", "list", "me", "the", "please",
                       "right", "now", "on", "today", "tonight"}


def _is_sport_only_query(text: str, sport: str) -> bool:
    """The query is just the sport (plus filler like "live
    games") — e.g. /game "football" or "show any cricket game",
    not "football team X"."""
    n = _norm(text or "")
    if not n or not sport:
        return False
    for phrase, _name in _SPORT_WORDS:
        n = re.sub(r"\b" + re.escape(phrase) + r"\b", " ", n)
    return not [w for w in n.split() if w not in _SPORT_QUERY_FILLER]


def _channel_mentions(text: str) -> list[str]:
    """Channel names the user typed: #name tokens and
    "in/to/for <name> channel" phrases."""
    mentions: list[str] = []
    for m in re.finditer(r"#([\w-]+)", text or ""):
        mentions.append(m.group(1))
    for m in re.finditer(r"\b(?:in|to|for)\s+([\w-]+)\s+channel\b",
                         text or "", re.I):
        mentions.append(m.group(1))
    return mentions


def _channel_from_words(text: str,
                        channels: list[dict] | None) -> tuple[dict | None,
                                                              str | None]:
    """Deterministic channel match: a #name or "in <name> channel"
    that equals a real channel name (case-insensitive).
    Returns (channel|None, named|None) — named is the name the
    user typed when it matches NO channel, so the caller can say
    so instead of silently watching elsewhere."""
    mentions = _channel_mentions(text)
    named = mentions[0] if mentions else None
    for name in mentions:
        for ch in channels or []:
            if _norm(name) == _norm(str(ch.get("name", ""))):
                return ch, None
    return None, named


# Words that mean "show me the games" rather than "follow this
# one game" — checked when no game matched, so "show amy new
# game to test" (a typo for "any") still lists games instead of
# dead-ending. Runs AFTER the AI attempt, so a request naming
# a specific team ("show me the Lumbini game") still resolves
# to that game first.
_SHOW_WORDS = ("show", "list", "see", "browse", "display", "any",
               "whats", "what's", "what")
_GAME_WORDS = ("game", "games", "match", "matches", "live", "playing",
               "on now", "test")


def _wants_list(text: str) -> bool:
    """The user wants to browse games, not follow one. A show
    word plus a game word ("show any game") — or plus a sport
    word ("show me cricket", "show football") — means browse."""
    n = _norm(text or "")
    if not any(w in n for w in _SHOW_WORDS):
        return False
    return (any(w in n for w in _GAME_WORDS)
            or _sport_from_words(text) is not None)


async def resolve_watch(text: str, model, channels: list[dict] | None = None) -> dict:
    """Resolve free text to a game — and, when the words name
    one, to a channel and a detail level. Game: game ID first,
    then exact/unique team-name match, then Gemini on the
    grounding list. Channel: #name / "in <name> channel" first,
    then Gemini on the channel list. Detail: the words' own
    keywords first, then Gemini's parsed detail.
    Returns {"game_id"|None, "action", "detail"|None,
    "channel"|None, "channel_id"|None, "channel_named"|None,
    "candidates", "entry"|None, "source", "games", "suggestions",
    "sport"|None}.
    "games" holds the top live games when action is "list";
    "suggestions" holds them when nothing matched at all, so the
    caller can offer them instead of a bare dead end. "sport"
    is the sport the words name — list and suggestion games are
    filtered to it, and an empty filtered list means "no game of
    that sport is live", never a list of other sports. Never
    invents an id or a channel id. "detail" is None when the words
    say nothing about it — the /watch dropdown always wins, and a
    hardcoded "simple" here used to discard the user's Advanced choice."""
    q = (text or "").strip()
    words_detail = _detail_from_words(q)
    word_channel, channel_named = _channel_from_words(q, channels)

    def _out(game_id, action, detail, candidates, entry, source,
             channel=None, note="", games=None, suggestions=None,
             sport=None):
        channel = channel if channel is not None else word_channel
        return {"game_id": game_id, "action": action, "detail": detail,
                "candidates": candidates, "entry": entry, "source": source,
                "note": note, "channel": channel,
                "channel_id": (str(channel["id"]) if channel else None),
                "channel_named": channel_named,
                "games": games or [], "suggestions": suggestions or [],
                "sport": sport}

    if not q:
        return _out(None, "follow", words_detail, [], None, "none")
    if q.isdigit() and len(q) >= 5:
        return _out(q, "follow", words_detail, [], None, "id")
    grounding = await prov.onex_grounding()
    exact = [g for g in grounding
             if _norm(g["home"]) == _norm(q) or _norm(g["away"]) == _norm(q)]
    if len(exact) == 1:
        return _out(str(exact[0]["id"]), "follow", words_detail, [],
                    exact[0], "name")
    matches = _match_entries(q, grounding)
    if len(matches) == 1:
        return _out(str(matches[0]["id"]), "follow", words_detail, [],
                    matches[0], "name")
    ai = await parse_watch_ai(q, grounding, model, channels)
    if ai and ai.get("action") == "list":
        # The AI's sport wins; the words back it up when the
        # model did not name one — "show any football game"
        # must not fall back to mixed sports.
        sport = ai.get("sport") or _sport_from_words(q)
        top = await top_live_games(sport=sport)
        return _out(None, "list", words_detail, [], None, "ai",
                    games=top, note=ai.get("note", ""), sport=sport)
    if ai and ai.get("game_id"):
        entry = next((g for g in grounding
                      if str(g["id"]) == str(ai["game_id"])), None)
        ai_channel = None
        if word_channel is None and ai.get("channel_id"):
            ai_channel = next(
                (c for c in (channels or [])
                 if str(c["id"]) == str(ai["channel_id"])), None)
        detail = (words_detail if words_detail is not None
                  else ai["detail"])
        return _out(str(ai["game_id"]), ai["action"], detail, [],
                    entry, "ai", channel=ai_channel,
                    note=ai.get("note", ""))
    if _wants_list(q):
        sport = _sport_from_words(q)
        top = await top_live_games(sport=sport)
        return _out(None, "list", words_detail, [], None, "list",
                    games=top, sport=sport)
    sport = _sport_from_words(q)
    suggestions = await top_live_games(sport=sport)
    return _out(None, "follow", words_detail, matches[:5], None, "none",
                suggestions=suggestions, sport=sport)


# ══════════════════════════════════════════════════════════════════════════
# WATCH REGISTRY + TICK (shared with the scoreboard's 1s loop)
# ══════════════════════════════════════════════════════════════════════════

# (channel_id, game_id) -> {home, away, league, detail, fmt,
#                           snapshot, missing}
_watches: dict[tuple[int, str], dict] = {}
_loaded = False


async def load_watches() -> None:
    """Load persisted watches into memory (called once at startup)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        rows = await db.get_all_watches()
    except Exception as exc:
        log.warning("[watch] could not load watches (%s)", type(exc).__name__)
        return
    for row in rows:
        try:
            snapshot = json.loads(row.get("snapshot") or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        _watches[(row["channel_id"], str(row["game_id"]))] = {
            "home": row.get("home", ""), "away": row.get("away", ""),
            "league": row.get("league", ""), "detail": row.get("detail") or DETAIL_SIMPLE,
            "fmt": row.get("fmt") or "", "snapshot": snapshot, "missing": 0,
        }
    if _watches:
        log.info("[watch] loaded %d watches", len(_watches))


async def start_watch(channel_id: int, entry: dict, level: str) -> dict | None:
    """Start watching one game in a channel. entry is a grounding
    dict ({id, home, away, league, status}); the detail is fetched
    fresh so the snapshot starts from live data."""
    game_id = str(entry["id"])
    detail = None
    try:
        result = await prov.onex_search(game_id)
        detail = result.get("detail")
    except Exception as exc:
        log.warning("[watch] detail fetch failed for %s (%s)", game_id,
                    type(exc).__name__)
    if detail is not None:
        # onex_search resolves alias ids to the live game — watch
        # under the live id or the tick lookup never matches.
        game_id = str(detail.get("onex_id") or game_id)
    if detail is None:
        detail = {
            "onex_id": game_id, "home": entry.get("home", ""),
            "away": entry.get("away", ""), "league": entry.get("league", ""),
            "home_score": None, "away_score": None,
            "status": entry.get("status", ""), "minute": None, "clock": "",
            "stats": None, "cricket": None, "format": "",
        }
    snapshot = snapshot_from_detail(detail)
    snapshot["_home"], snapshot["_away"] = detail.get("home", ""), detail.get("away", "")
    fmt = str(detail.get("format") or "")
    await db.add_watch(channel_id, game_id, detail.get("home", ""),
                       detail.get("away", ""), detail.get("league", ""),
                       level, fmt, snapshot)
    _watches[(channel_id, game_id)] = {
        "home": detail.get("home", ""), "away": detail.get("away", ""),
        "league": detail.get("league", ""), "detail": level, "fmt": fmt,
        "snapshot": snapshot, "missing": 0,
    }
    return {"game_id": game_id, "home": detail.get("home", ""),
            "away": detail.get("away", ""), "league": detail.get("league", ""),
            "status": detail.get("status", ""), "detail": level}


async def remove_watch(channel_id: int, game_id: str) -> dict | None:
    entry = _watches.pop((channel_id, str(game_id)), None)
    await db.remove_watch(channel_id, str(game_id))
    return entry


async def list_channel_watches(channel_id: int) -> list[dict]:
    rows = await db.list_watches(channel_id)
    out = []
    for row in rows:
        try:
            snapshot = json.loads(row.get("snapshot") or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        out.append({
            "game_id": str(row["game_id"]), "home": row.get("home", ""),
            "away": row.get("away", ""), "league": row.get("league", ""),
            "detail": row.get("detail") or DETAIL_SIMPLE,
            "snapshot": snapshot,
        })
    return out


def live_line(d: dict) -> str:
    """One short live line for the 🔄 Live button: teams, score,
    and the MM:SS clock for football, overs for cricket."""
    title = f"{d.get('home', '?')} {_score_str(d)} {d.get('away', '?')}"
    cricket = d.get("cricket") or {}
    if cricket.get("is_break"):
        return f"{title} · Innings break"
    if cricket.get("over_str") is not None:
        return f"{title} · {cricket['over_str']} ov"
    clock = _clock_str(d)
    return f"{title} · {clock}" if clock else title


async def _fetch_live_detail(game_id: str) -> dict | None:
    """Current detail for a game via onex_search (tick cache, fast).
    None when the game is gone from the live list — a score-less
    gameInfo-only detail is not live data either."""
    try:
        result = await prov.onex_search(str(game_id))
    except Exception as exc:
        log.warning("[watch] live detail fetch failed for %s (%s)",
                    game_id, type(exc).__name__)
        return None
    detail = (result or {}).get("detail")
    if detail is None:
        return None
    if detail.get("home_score") is None and detail.get("away_score") is None \
            and str(detail.get("status") or "") in ("?", "upcoming", ""):
        return None
    return detail


_GAME_ID_RE = re.compile(r"Game ID:\s*(\d+)")


def _resolve_game_id(interaction: discord.Interaction) -> str | None:
    """Game id for a 📊 Detail / 🔄 Live button click. First the
    per-game custom_id ("watch:detail:<id>" / "watch:live:<id>")
    when the button was built with one; otherwise the "Game ID:
    <id>" line in the message the button sits on — every detail
    reply and every alert prints one. None when neither is there."""
    custom_id = ""
    try:
        custom_id = str((interaction.data or {}).get("custom_id") or "")
    except Exception:
        custom_id = ""
    parts = custom_id.split(":")
    if len(parts) >= 3 and parts[2].isdigit():
        return parts[2]
    message = getattr(interaction, "message", None)
    content = getattr(message, "content", "") or ""
    match = _GAME_ID_RE.search(content)
    if match:
        return match.group(1)
    return None


class WatchAlertView(discord.ui.View):
    """Buttons on a watch alert or a detail reply. Persistent
    (timeout=None) with STATIC custom_ids ("watch:detail" /
    "watch:live") — the game id is resolved from the message the
    button sits on, so one view registered in setup_hook covers
    every button on every message, even after a restart. A
    per-game custom_id ("watch:detail:<id>") still works as a
    fallback when the view was built with a game id."""

    def __init__(self, game_id: str | None = None):
        super().__init__(timeout=None)
        self.game_id = str(game_id) if game_id else None
        detail_id = (f"watch:detail:{self.game_id}" if self.game_id
                     else "watch:detail")
        live_id = (f"watch:live:{self.game_id}" if self.game_id
                   else "watch:live")
        detail_btn = discord.ui.Button(
            label="📊 Detail", style=discord.ButtonStyle.primary,
            custom_id=detail_id)
        detail_btn.callback = self._on_detail
        self.add_item(detail_btn)
        live_btn = discord.ui.Button(
            label="🔄 Live", style=discord.ButtonStyle.secondary,
            custom_id=live_id)
        live_btn.callback = self._on_live
        self.add_item(live_btn)

    async def _on_detail(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            await interaction.followup.send(
                "Could not tell which game this is.", ephemeral=True)
            return
        detail = await _fetch_live_detail(game_id)
        if detail is None:
            await interaction.followup.send(
                "That game is no longer live.", ephemeral=True)
            return
        await interaction.followup.send(
            render_detail(detail, DETAIL_ADVANCED), ephemeral=True)

    async def _on_live(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            await interaction.followup.send(
                "Could not tell which game this is.", ephemeral=True)
            return
        detail = await _fetch_live_detail(game_id)
        if detail is None:
            await interaction.followup.send(
                "That game is no longer live.", ephemeral=True)
            return
        await interaction.followup.send(live_line(detail), ephemeral=True)


async def _send_to_channel(bot: discord.Client, channel_id: int,
                           text: str, view: discord.ui.View | None = None) -> bool:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await asyncio.wait_for(bot.fetch_channel(channel_id), 20)
        except Exception as exc:
            log.warning("[watch] channel %s unavailable (%s)", channel_id,
                        type(exc).__name__)
            return False
    try:
        await channel.send(text, view=view,
                           allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.Forbidden:
        log.warning("[watch] no permission to send to channel %s", channel_id)
    except discord.HTTPException as exc:
        log.warning("[watch] send failed to %s (%s)", channel_id, exc)
    return False


def _detail_for_tick(raw: dict, fmt: str) -> dict | None:
    """Rich detail from one raw live entry, with the stored format
    so the cricket target can be computed without a gameInfo call
    on every tick."""
    detail = prov._onex_detail_from_raw(raw)
    if detail is None:
        return None
    if fmt:
        detail["format"] = fmt
        cricket = detail.get("cricket")
        if cricket:
            cricket.update(prov._onex_cricket_target_fmt(
                fmt, detail.get("home_score"), detail.get("away_score"),
                cricket.get("team"), cricket.get("over_str")))
    return detail


async def on_tick(bot: discord.Client, raw_games: list[dict]) -> None:
    """One shared tick: diff every watched game against its stored
    snapshot and send ONLY on a real change. Called from
    scoreboard_loop — never raises, never sends duplicates."""
    if not _watches:
        return
    by_id: dict[str, dict] = {}
    for g in raw_games or []:
        gid = str(g.get("id") or g.get("mainGameId") or "")
        if gid:
            by_id[gid] = g
    for key, watch in list(_watches.items()):
        channel_id, game_id = key
        try:
            raw = by_id.get(game_id)
            if raw is None:
                watch["missing"] = watch.get("missing", 0) + 1
                if watch["missing"] >= MISSING_TICKS_TO_END:
                    await _send_to_channel(
                        bot, channel_id,
                        f"🏁 **{watch['home']} vs {watch['away']}** is no longer live — "
                        f"the watch has ended.")
                    await remove_watch(channel_id, game_id)
                    log.info("[watch] ended %s vs %s (gone from live list)",
                             watch["home"], watch["away"])
                continue
            watch["missing"] = 0
            detail = _detail_for_tick(raw, watch.get("fmt") or "")
            if detail is None:
                continue
            snapshot = snapshot_from_detail(detail)
            snapshot["_home"], snapshot["_away"] = detail.get("home", ""), detail.get("away", "")
            old = watch.get("snapshot") or {}
            old = dict(old)
            old.setdefault("_home", watch.get("home", ""))
            old.setdefault("_away", watch.get("away", ""))
            events = detect_changes(old, snapshot, watch.get("detail") or DETAIL_SIMPLE)
            if not events:
                continue
            text = "\n".join(events)
            # Automatic alert stays exactly as before: event line(s)
            # PLUS the full stats table for advanced watches. The
            # buttons below are only for on-demand detail / live.
            if watch.get("detail") == DETAIL_ADVANCED:
                table = format_stats_table(detail.get("stats"))
                if table:
                    text += "\n\n**Match stats**\n" + "\n".join(table)
            # The buttons resolve the game from the message, so the
            # alert carries its id like the detail card does.
            text += f"\nGame ID: {game_id}"
            if await _send_to_channel(bot, channel_id, text,
                                      view=WatchAlertView()):
                log.info("[watch] alert for %s vs %s: %s",
                         watch["home"], watch["away"], events[0][:80])
            watch["snapshot"] = snapshot
            await db.update_watch_snapshot(channel_id, game_id, snapshot)
            if snapshot.get("status") == "FT":
                # The FT event above already carries the result;
                # just end the watch quietly.
                await remove_watch(channel_id, game_id)
                log.info("[watch] ended %s vs %s (full-time)",
                         watch["home"], watch["away"])
        except Exception as exc:
            log.warning("[watch] tick failed for %s (%s)", game_id,
                        type(exc).__name__)
