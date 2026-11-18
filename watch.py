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

import barcelona_reminder
import db
import history
import providers as prov

log = logging.getLogger("watch")
NEPAL = ZoneInfo("Asia/Kathmandu")

DETAIL_SIMPLE = "simple"
DETAIL_ADVANCED = "advanced"

# Every event kind the engine can detect. Prefs (Feature 5)
# filter on these; history (Feature 6) records them.
EVENT_KINDS = (
    "goal", "wicket", "yellow", "red", "penalty", "sub",
    "kickoff", "ht", "second_half", "ft", "break", "correction",
    "postponed", "suspended", "abandoned", "cancelled",
)
# Statuses that end a match without a final whistle on the
# scoreboard: the feed may report them for a fixture that never
# starts or is stopped. Normalised lower-case -> (kind, icon, label).
TERMINAL_STATUSES: dict[str, tuple[str, str, str]] = {
    "postponed": ("postponed", "📅", "Match postponed"),
    "pst": ("postponed", "📅", "Match postponed"),
    "suspended": ("suspended", "⏸", "Match suspended"),
    "interrupted": ("suspended", "⏸", "Match suspended"),
    "abandoned": ("abandoned", "⚠️", "Match abandoned"),
    "abd": ("abandoned", "⚠️", "Match abandoned"),
    "cancelled": ("cancelled", "❌", "Match cancelled"),
    "canc": ("cancelled", "❌", "Match cancelled"),
}

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


# Preferred stat order for the analytics table (Feature 4);
# any stat the feed sends that is not listed here is appended
# after these, in the order it arrived. "Fouls" is not in the
# feed's stats, so it never appears — nothing is invented.
_ANALYTICS_ORDER = (
    "Possession %", "Shots on target", "Shots off target", "Corner",
    "Yellow cards", "Red card", "Attacks", "Dangerous attacks",
    "xG", "Saves", "Penalty", "Substitutions", "Key Passes",
    "Passing Accuracy %", "Crosses",
)
_ANALYTICS_LABELS = {"Possession %": "Possession", "Corner": "Corners"}


def render_analytics(d: dict, momentum: str | None = None) -> str:
    """Advanced analytics view: home/away comparison table from
    the stats the feed actually carries, plus cricket target/
    need/run-rate when present and a momentum estimate when one
    was computed from real snapshots. Ends with the Game ID line
    so the panel buttons on this message keep resolving."""
    lines = [f"**{d.get('home', '?')} {_score_str(d)} {d.get('away', '?')}**"]
    if d.get("league"):
        lines.append(str(d["league"]))
    lines.append("")
    lines.append(_clock_str(d))
    stats = d.get("stats") or []
    by_name = {str(s.get("name")): s for s in stats}
    ordered = [n for n in _ANALYTICS_ORDER if n in by_name]
    ordered += [n for n in by_name if n not in _ANALYTICS_ORDER]
    if ordered:
        lines.append("")
        lines.append("**Match statistics**")
        lines.append("| Statistic | Home | Away |")
        lines.append("|---|---:|---:|")
        for name in ordered:
            s = by_name[name]
            label = _ANALYTICS_LABELS.get(name, name)
            lines.append(f"| {label} | {s.get('home', '')} | {s.get('away', '')} |")
    else:
        lines.append("")
        lines.append("No match statistics available yet.")
    cricket = d.get("cricket") or {}
    if cricket.get("target") is not None:
        lines.append(f"Target: {cricket['target']}")
        need = cricket.get("need")
        balls = cricket.get("balls_left")
        if need is not None:
            lines.append(f"Need: {need} off {balls} balls" if balls is not None
                         else f"Need: {need} runs")
    if cricket.get("run_rate"):
        lines.append(f"Run rate: {cricket['run_rate']}")
    if momentum:
        lines.append("")
        lines.append(momentum)
    gid = str(d.get("onex_id") or d.get("id") or "?")
    const = d.get("const_id")
    lines.append(f"Game ID: {gid}" + (f" · {const}" if const else ""))
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


def _score_runs(score: object) -> int | None:
    """Numeric score: football "2" -> 2, cricket "120/2" -> 120
    (runs). None when there is no score yet."""
    if score is None:
        return None
    text = str(score).strip()
    if not text or text in ("?", "–"):
        return None
    try:
        return int(text.split("/")[0])
    except ValueError:
        return None


def detect_events(old: dict, new: dict, level: str = DETAIL_SIMPLE,
                  game_id: str = "") -> list[dict]:
    """Event dicts for what changed between two snapshots:
    {"kind", "key", "text"}. key is stable across restarts
    (game + kind + score + clock/counts) so the history table
    can dedup it — a restart never re-sends an old event.
    Nothing changed -> []. A cricket score that only moved in
    runs (no wicket) is NOT an event — it would spam every ball.
    A score that went DOWN is a correction, never a goal."""
    events: list[dict] = []
    title = f"{new.get('_home', '?')} {new['home_score']}–{new['away_score']} {new.get('_away', '?')}"
    clock = new.get("clock") or ""
    suffix = f" ({clock})" if clock else ""

    def _key(kind: str, extra: str = "") -> str:
        base = f"{game_id}|{kind}|{new.get('home_score')}–{new.get('away_score')}|{clock}"
        return base + (f"|{extra}" if extra else "")

    def _add(kind: str, text: str, extra: str = "") -> None:
        events.append({"kind": kind, "key": _key(kind, extra), "text": text})

    old_status, new_status = old.get("status", ""), new.get("status", "")
    if old_status in ("upcoming", "", "?") and new_status == "live":
        _add("kickoff", f"▶ **Kickoff** — {title}{suffix}")
    if new_status == "HT" and old_status != "HT":
        _add("ht", f"⏸ **Half-time** — {title}{suffix}")
    if old_status == "HT" and new_status == "2H":
        _add("second_half", f"▶ **Second-half start** — {title}{suffix}")
    if new_status == "FT" and old_status != "FT":
        _add("ft", f"⏱ **Full-time** — {title}")
        return events
    terminal = TERMINAL_STATUSES.get(str(new_status).strip().lower())
    if terminal and old_status != new_status:
        kind, icon, label = terminal
        _add(kind, f"{icon} **{label}** — {title}")
        return events
    if new.get("is_break") and not old.get("is_break"):
        _add("break", f"☕ **Innings break** — {title}{suffix}")
    if old.get("home_score") != new.get("home_score") or old.get("away_score") != new.get("away_score"):
        # First sighting ("" -> "0") is kickoff, not a goal.
        first = (old.get("home_score") in (None, "")
                 and old.get("away_score") in (None, ""))
        if first:
            return events
        old_h, old_a = _score_runs(old.get("home_score")), _score_runs(old.get("away_score"))
        new_h, new_a = _score_runs(new.get("home_score")), _score_runs(new.get("away_score"))
        if ((old_h is not None and new_h is not None and new_h < old_h)
                or (old_a is not None and new_a is not None and new_a < old_a)):
            # Score went DOWN: a correction (reversed goal), not a goal.
            _add("correction",
                 f"✏️ **Score corrected** — was {old.get('home_score')}–{old.get('away_score')}, "
                 f"now {title}{suffix}")
        else:
            old_wk = _wickets(old.get("home_score"))
            new_wk = _wickets(new.get("home_score"))
            old_wk2 = _wickets(old.get("away_score"))
            new_wk2 = _wickets(new.get("away_score"))
            if ((old_wk is not None and new_wk is not None and new_wk > old_wk)
                    or (old_wk2 is not None and new_wk2 is not None and new_wk2 > old_wk2)):
                _add("wicket", f"🏏 **WICKET** — {title}{suffix}")
            elif new.get("status") not in ("upcoming", "", "?"):
                # Football score change is a goal; cricket runs alone
                # (no wicket) are skipped above by the wicket check —
                # but a football score has no "/" so _wickets returns 0
                # for both sides and this branch fires correctly.
                if "/" not in (new.get("home_score") or "") and "/" not in (new.get("away_score") or ""):
                    _add("goal", f"⚽ **GOAL** — {title}{suffix}")
    old_stats, new_stats = old.get("stats") or {}, new.get("stats") or {}
    for label, icon, kind in (("Yellow cards", "🟨", "yellow"),
                              ("Red card", "🟥", "red")):
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
            _add(kind, f"{icon} **{'Yellow card' if kind == 'yellow' else 'RED CARD'}** — {title}{suffix} (home {nh}, was {oh})", f"home:{nh}")
        if na > oa:
            _add(kind, f"{icon} **{'Yellow card' if kind == 'yellow' else 'RED CARD'}** — {title}{suffix} (away {na}, was {oa})", f"away:{na}")
    # Penalty awarded: a major event at both levels (a scored
    # penalty also fires its own GOAL line above; a missed one
    # only shows here). Substitutions stay advanced-only — simple
    # watches chose minimal alerts.
    o = old_stats.get("Penalty")
    n = new_stats.get("Penalty")
    if o is not None and n is not None:
        try:
            oh, oa = int(o[0] or 0), int(o[1] or 0)
            nh, na = int(n[0] or 0), int(n[1] or 0)
        except ValueError:
            oh = oa = nh = na = 0
        if nh > oh or na > oa:
            _add("penalty", f"🎯 **Penalty** — {title}{suffix} (now {nh}–{na}, was {oh}–{oa})", f"{nh}–{na}")
    if level == DETAIL_ADVANCED:
        o = old_stats.get("Substitutions")
        n = new_stats.get("Substitutions")
        if o is not None and n is not None:
            try:
                oh, oa = int(o[0] or 0), int(o[1] or 0)
                nh, na = int(n[0] or 0), int(n[1] or 0)
            except ValueError:
                oh = oa = nh = na = 0
            if nh > oh or na > oa:
                _add("sub", f"🔄 **Substitutions** — {title}{suffix} (now {nh}–{na}, was {oh}–{oa})", f"{nh}–{na}")
    return events


def detect_changes(old: dict, new: dict, level: str = DETAIL_SIMPLE) -> list[str]:
    """Event lines for what changed between two snapshots.
    Nothing changed -> []. Kept as the plain-text wrapper around
    detect_events (same texts, same order) for callers that only
    need the lines."""
    return [e["text"] for e in detect_events(old, new, level)]


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


# ══════════════════════════════════════════════════════════════════════════
# BARÇA AUTO-WATCH — rides the shared 1s tick, no second poller.
#
# When an FC Barcelona match goes live, start a SIMPLE watch for it
# in #general so goal alerts post there with no one typing /watch.
# Silent by design: nothing is sent before kickoff or outside the
# match — no "now watching" notice, no pre-match chatter. The
# first message is whatever the normal on-change alerts send
# (kickoff, then goals/cards/half-time/full-time) while the game
# is live. The 12h reminder in barcelona_reminder.py is untouched.
# ══════════════════════════════════════════════════════════════════════════

# One auto-watch check per tick is too much (the search also reads
# the fixture lists) — the 1s tick is cheap only when the check is
# throttled, and a match only goes live once.
_BARCA_CHECK_EVERY_S = 30.0
_barca_last_check = 0.0

# Statuses that mean "live right now" for a searched game.
# "live" is the line-feed word for a fixture past kickoff; FT and
# "?" / upcoming are never live.
_BARCA_LIVE_STATUSES = ("live", "1h", "2h", "et", "pen", "ht",
                        "half-time", "1st innings", "2nd innings")


def _barca_already_watched(channel_id: int) -> bool:
    """A Barcelona game already watched in this channel — manual
    or auto — so the auto-watch never double-starts one."""
    for (cid, _gid), w in _watches.items():
        if cid != channel_id:
            continue
        if barcelona_reminder._is_barcelona(
                {"home": w.get("home", ""), "away": w.get("away", "")}):
            return True
    return False


async def maybe_auto_watch_barca() -> dict | None:
    """Start a SIMPLE watch for a live Barcelona game in #general.
    The game id comes from onex_search("Barcelona") against the
    live list — never invented; if Barcelona is not live, start
    nothing and stay silent. Returns the started game or None.
    Never raises — a failed search just skips this check."""
    global _barca_last_check
    channel_id = barcelona_reminder.GENERAL_CHANNEL_ID
    if not channel_id:
        return None
    if _barca_already_watched(channel_id):
        return None
    now = time.monotonic()
    if now - _barca_last_check < _BARCA_CHECK_EVERY_S:
        return None
    _barca_last_check = now
    try:
        result = await prov.onex_search("Barcelona")
    except Exception as exc:
        log.warning("[barca-watch] search failed (%s)", type(exc).__name__)
        return None
    live = [g for g in (result or {}).get("games") or []
            if str(g.get("status") or "").lower() in _BARCA_LIVE_STATUSES
            and barcelona_reminder._is_barcelona(g)]
    if not live:
        return None
    game = live[0]
    entry = {"id": str(game["onex_id"]), "home": game.get("home", ""),
             "away": game.get("away", ""), "league": game.get("league", ""),
             "status": game.get("status", "")}
    try:
        started = await start_watch(channel_id, entry, DETAIL_SIMPLE)
    except Exception as exc:
        log.warning("[barca-watch] start failed (%s)", type(exc).__name__)
        return None
    if not started:
        return None
    log.info("[barca-watch] auto-watch started %s vs %s (game %s) "
             "in channel %s", started["home"], started["away"],
             started["game_id"], channel_id)
    return started


async def _prefs_for(channel_id: int, game_id: str) -> tuple[set[str] | None, bool, str]:
    """Effective notification prefs for a watch: (allowed kinds
    or None for "all", paused, detail override or ""). A missing
    row or an empty events list means "everything, not paused".
    Never raises — a failed read means defaults."""
    try:
        row = await db.get_prefs(channel_id, game_id)
    except Exception as exc:
        log.warning("[watch] prefs read failed (%s)", type(exc).__name__)
        return None, False, ""
    if row is None:
        return None, False, ""
    events = row.get("events") or []
    allowed = set(events) if events else None
    return allowed, bool(row.get("paused")), str(row.get("detail") or "")


# Team follows (Feature 5): same throttled pattern as the Barça
# auto-watch — one check per 30s, reads the tick cache only.
_TEAM_CHECK_EVERY_S = 30.0
_team_last_check = 0.0


async def maybe_team_follow_check() -> None:
    """Start watches for live games involving a followed team.
    Silent: nothing is sent before kickoff or outside the match
    — the normal on-change alerts do the talking."""
    global _team_last_check
    now = time.monotonic()
    if now - _team_last_check < _TEAM_CHECK_EVERY_S:
        return
    _team_last_check = now
    try:
        follows = await db.get_all_team_follows()
    except Exception as exc:
        log.warning("[team-follow] read failed (%s)", type(exc).__name__)
        return
    for follow in follows:
        try:
            channel_id = int(follow["channel_id"])
            team_name = str(follow.get("team_name") or "")
            team_norm = str(follow.get("team_norm") or "")
            if not team_name or not channel_id:
                continue
            already = any(
                cid == channel_id
                and _norm(team_norm) in _norm(f"{w.get('home', '')} {w.get('away', '')}")
                for (cid, _gid), w in _watches.items())
            if already:
                continue
            result = await prov.onex_search(team_name)
            live = [g for g in (result or {}).get("games") or []
                    if str(g.get("status") or "").lower() in _BARCA_LIVE_STATUSES
                    and _norm(team_norm) in _norm(f"{g.get('home', '')} {g.get('away', '')}")]
            if not live:
                continue
            game = live[0]
            entry = {"id": str(game["onex_id"]), "home": game.get("home", ""),
                     "away": game.get("away", ""), "league": game.get("league", ""),
                     "status": game.get("status", "")}
            started = await start_watch(
                channel_id, entry, str(follow.get("detail") or DETAIL_SIMPLE))
            if started:
                log.info("[team-follow] auto-watch started %s vs %s "
                         "(game %s) in channel %s for team %s",
                         started["home"], started["away"],
                         started["game_id"], channel_id, team_name)
        except Exception as exc:
            log.warning("[team-follow] check failed (%s)", type(exc).__name__)


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


# Refresh cooldown: one manual refresh per user per game per 10s,
# so a button cannot hammer the provider.
_REFRESH_COOLDOWN_S = 10.0
_refresh_cooldown: dict[tuple[int, str], float] = {}


class WatchAlertView(discord.ui.View):
    """Buttons on a watch alert or a detail reply — the live match
    panel (Feature 2). Persistent (timeout=None) with STATIC
    custom_ids ("watch:detail" / "watch:live" / "panel:*") — the
    game id is resolved from the message the button sits on, so
    one view registered in setup_hook covers every button on
    every message, even after a restart. A per-game custom_id
    ("watch:detail:<id>", "panel:refresh:<id>", ...) still works
    as a fallback when the view was built with a game id.

    Detail / Live are the original two buttons. Refresh updates
    a detail card in place (or answers with a fresh live line);
    Timeline shows the chronoLine events; Simple / Advanced show
    that level of detail; Follow / Unfollow start or stop a
    watch for this game in this channel."""

    def __init__(self, game_id: str | None = None):
        super().__init__(timeout=None)
        self.game_id = str(game_id) if game_id else None

        def _cid(prefix: str) -> str:
            return f"{prefix}:{self.game_id}" if self.game_id else prefix

        specs = [
            ("📊 Detail", discord.ButtonStyle.primary, _cid("watch:detail"),
             self._on_detail),
            ("🔄 Live", discord.ButtonStyle.secondary, _cid("watch:live"),
             self._on_live),
            ("🔄 Refresh", discord.ButtonStyle.secondary, _cid("panel:refresh"),
             self._on_refresh),
            ("📜 Timeline", discord.ButtonStyle.secondary, _cid("panel:timeline"),
             self._on_timeline),
            ("🟢 Simple", discord.ButtonStyle.secondary, _cid("panel:simple"),
             self._on_simple),
            ("🔵 Advanced", discord.ButtonStyle.secondary, _cid("panel:advanced"),
             self._on_advanced),
            ("🔔 Follow", discord.ButtonStyle.success, _cid("panel:follow"),
             self._on_follow),
            ("🔕 Unfollow", discord.ButtonStyle.danger, _cid("panel:unfollow"),
             self._on_unfollow),
        ]
        for label, style, custom_id, callback in specs:
            btn = discord.ui.Button(label=label, style=style,
                                    custom_id=custom_id)
            btn.callback = callback
            self.add_item(btn)

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

    async def _on_refresh(self, interaction: discord.Interaction):
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            await interaction.response.send_message(
                "Could not tell which game this is.", ephemeral=True)
            return
        user_id = getattr(getattr(interaction, "user", None), "id", 0)
        now = time.monotonic()
        last = _refresh_cooldown.get((user_id, game_id), 0.0)
        if now - last < _REFRESH_COOLDOWN_S:
            await interaction.response.send_message(
                "Give it a few seconds before refreshing again.",
                ephemeral=True)
            return
        _refresh_cooldown[(user_id, game_id)] = now
        # Non-ephemeral defer: edit_original_response below edits
        # the message the button sits on.
        await interaction.response.defer()
        detail = await _fetch_live_detail(game_id)
        if detail is None:
            await interaction.followup.send(
                "That game is no longer live.", ephemeral=True)
            return
        content = getattr(getattr(interaction, "message", None),
                          "content", "") or ""
        if content.startswith("**") and "Game ID:" in content:
            level = (DETAIL_ADVANCED if "Match stats" in content
                     else DETAIL_SIMPLE)
            await interaction.edit_original_response(
                content=render_detail(detail, level),
                view=WatchAlertView(game_id))
        else:
            await interaction.followup.send(live_line(detail), ephemeral=True)

    async def _on_timeline(self, interaction: discord.Interaction):
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
        try:
            gid = int(game_id)
        except (TypeError, ValueError):
            await interaction.followup.send(
                "Could not read this game's timeline.", ephemeral=True)
            return
        events = await prov.onex_events(
            gid, detail.get("home", ""), detail.get("away", ""))
        if not events:
            await interaction.followup.send(
                "No timeline events yet for this game.", ephemeral=True)
            return
        lines = [f"**{detail.get('home', '?')} {_score_str(detail)} "
                 f"{detail.get('away', '?')}** — timeline\n"]
        for ev in sorted(events, key=lambda e: e.get("minute_int") or 0):
            lines.append(f"{ev.get('icon', '')} **{ev.get('minute', '?')}'** "
                         f"{ev.get('detail', '')} — {ev.get('player', '')} "
                         f"({ev.get('team', '')})")
        await interaction.followup.send("\n".join(lines)[:2000],
                                        ephemeral=True)

    async def _on_simple(self, interaction: discord.Interaction):
        await self._on_level(interaction, DETAIL_SIMPLE)

    async def _on_advanced(self, interaction: discord.Interaction):
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
        momentum = await history.momentum_summary(game_id)
        await interaction.followup.send(
            render_analytics(detail, momentum), ephemeral=True)

    async def _on_level(self, interaction: discord.Interaction, level: str):
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
            render_detail(detail, level), ephemeral=True)

    async def _on_follow(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            await interaction.followup.send(
                "Could not tell which game this is.", ephemeral=True)
            return
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            await interaction.followup.send(
                "Could not tell which channel this is.", ephemeral=True)
            return
        if (channel_id, game_id) in _watches:
            await interaction.followup.send(
                "That game is already watched in this channel.",
                ephemeral=True)
            return
        detail = await _fetch_live_detail(game_id)
        if detail is None:
            await interaction.followup.send(
                "That game is no longer live.", ephemeral=True)
            return
        entry = {"id": game_id, "home": detail.get("home", ""),
                 "away": detail.get("away", ""),
                 "league": detail.get("league", ""),
                 "status": detail.get("status", "")}
        started = await start_watch(channel_id, entry, DETAIL_SIMPLE)
        if not started:
            await interaction.followup.send(
                "Could not start watching that game.", ephemeral=True)
            return
        await interaction.followup.send(
            f"👀 Watching **{started['home']} vs {started['away']}** — "
            f"simple alerts, only when the game changes.", ephemeral=True)

    async def _on_unfollow(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            await interaction.followup.send(
                "Could not tell which game this is.", ephemeral=True)
            return
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            await interaction.followup.send(
                "Could not tell which channel this is.", ephemeral=True)
            return
        detail = await _fetch_live_detail(game_id)
        if detail is None:
            await interaction.followup.send(
                "That game is no longer live.", ephemeral=True)
            return
        removed = await remove_watch(channel_id, game_id)
        if removed:
            await interaction.followup.send(
                f"🔕 Stopped watching **{removed['home']} vs {removed['away']}**.",
                ephemeral=True)
        else:
            await interaction.followup.send(
                "That game is not watched in this channel.", ephemeral=True)


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


# One tick at a time: the scoreboard calls on_tick once per
# second, but a slow tick must never overlap the next one.
_tick_lock = asyncio.Lock()

# Momentum snapshots: one stat sample per watched game per 30s.
_MOMENTUM_EVERY_S = 30.0
_last_momentum_snapshot: dict[tuple[int, str], float] = {}


async def on_tick(bot: discord.Client, raw_games: list[dict]) -> None:
    """One shared tick: diff every watched game against its stored
    snapshot and send ONLY on a real change. Called from
    scoreboard_loop — never raises, never sends duplicates, and
    never re-sends an event recorded in the history table (so a
    restart cannot repeat an old alert)."""
    async with _tick_lock:
        await _on_tick_locked(bot, raw_games)


async def _on_tick_locked(bot: discord.Client, raw_games: list[dict]) -> None:
    # Barça auto-watch rides this same tick — no second poller.
    # It runs before the early return below so it also fires when
    # no game is watched yet.
    await maybe_auto_watch_barca()
    await maybe_team_follow_check()
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
                    await history.record_match_result(
                        game_id, watch["home"], watch["away"],
                        watch.get("league", ""), "",
                        (watch.get("snapshot") or {}).get("home_score"),
                        (watch.get("snapshot") or {}).get("away_score"),
                        "ended")
                    await remove_watch(channel_id, game_id)
                    log.info("[watch] ended %s vs %s (gone from live list)",
                             watch["home"], watch["away"])
                continue
            watch["missing"] = 0
            detail = _detail_for_tick(raw, watch.get("fmt") or "")
            if detail is None:
                continue
            # Momentum sample: real stat snapshots, one per 30s.
            if detail.get("sport") == "Football" and detail.get("stats"):
                last = _last_momentum_snapshot.get(key, 0.0)
                now = time.monotonic()
                if now - last >= _MOMENTUM_EVERY_S:
                    _last_momentum_snapshot[key] = now
                    await history.record_stat_snapshot(
                        game_id, {str(s.get("name")): (str(s.get("home", "")),
                                                       str(s.get("away", "")))
                                  for s in detail["stats"]})
            snapshot = snapshot_from_detail(detail)
            snapshot["_home"], snapshot["_away"] = detail.get("home", ""), detail.get("away", "")
            old = watch.get("snapshot") or {}
            old = dict(old)
            old.setdefault("_home", watch.get("home", ""))
            old.setdefault("_away", watch.get("away", ""))
            allowed, paused, detail_override = await _prefs_for(channel_id, game_id)
            level = detail_override or watch.get("detail") or DETAIL_SIMPLE
            events = detect_events(old, snapshot, level, game_id)
            status = str(snapshot.get("status") or "")
            terminal = TERMINAL_STATUSES.get(status.strip().lower())
            is_terminal = status == "FT" or terminal is not None
            # Prefs filter: paused stops everything; an event-kind
            # list drops the kinds the user turned off. Terminal
            # events (full-time, postponed...) always pass the
            # kind filter — the watch is ending and the user needs
            # to know — but a paused watch stays silent.
            sendable = []
            for ev in events:
                if paused:
                    continue
                if allowed is not None and ev["kind"] not in allowed \
                        and ev["kind"] not in ("ft", "postponed",
                                               "suspended", "abandoned",
                                               "cancelled"):
                    continue
                if await history.event_seen(ev["key"]):
                    continue
                sendable.append(ev)
            if sendable:
                text = "\n".join(ev["text"] for ev in sendable)
                # Automatic alert stays exactly as before: event line(s)
                # PLUS the full stats table for advanced watches. The
                # buttons below are only for on-demand detail / live.
                if level == DETAIL_ADVANCED:
                    table = format_stats_table(detail.get("stats"))
                    if table:
                        text += "\n\n**Match stats**\n" + "\n".join(table)
                # The buttons resolve the game from the message, so the
                # alert carries its id like the detail card does.
                text += f"\nGame ID: {game_id}"
                if await _send_to_channel(bot, channel_id, text,
                                          view=WatchAlertView()):
                    for ev in sendable:
                        await history.record_sent_event(
                            ev["key"], game_id, channel_id, ev["kind"], ev["text"])
                        await history.record_notification(
                            game_id, channel_id, ev["kind"], ev["text"], True)
                    log.info("[watch] alert for %s vs %s: %s",
                             watch["home"], watch["away"], sendable[0]["text"][:80])
                else:
                    # Send failed: do not record, do not advance the
                    # snapshot — the next tick retries the same events.
                    continue
            watch["snapshot"] = snapshot
            await db.update_watch_snapshot(channel_id, game_id, snapshot)
            if is_terminal:
                await history.record_match_result(
                    game_id, watch["home"], watch["away"],
                    watch.get("league", ""), str(detail.get("sport") or ""),
                    snapshot.get("home_score"), snapshot.get("away_score"),
                    "FT" if status == "FT" else status)
                await remove_watch(channel_id, game_id)
                log.info("[watch] ended %s vs %s (%s)",
                         watch["home"], watch["away"],
                         "full-time" if status == "FT" else status)
        except Exception as exc:
            log.warning("[watch] tick failed for %s (%s)", game_id,
                        type(exc).__name__)
