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
    return {
        "home_score": "" if d.get("home_score") is None else str(d["home_score"]),
        "away_score": "" if d.get("away_score") is None else str(d["away_score"]),
        "status": str(d.get("status") or ""),
        "minute": d.get("minute") if isinstance(d.get("minute"), int) else None,
        "clock": str(d.get("clock") or ""),
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
{"game_id": "<id from the list, or null>", "action": "follow|unfollow|detail", "detail": "simple|advanced", "note": "<one short line>"}
Rules:
- game_id MUST be one of the ids in the games list. If no game matches the request, use null.
- action is "follow" when the user wants alerts/updates for a game, "unfollow" when they want to stop following it, "detail" when they only want to see it now.
- detail is "advanced" only when the user asks for full/advanced/detailed stats; otherwise "simple".
- note is one short line saying what you understood, e.g. "Lumbini vs Sudurpaschim, NPL".

Games (id | home vs away | league | status):
__GAMES__

User request: "__TEXT__"
"""


async def parse_watch_ai(text: str, grounding: list[dict],
                         model) -> dict | None:
    """Ask Gemini to resolve free text to a game. model is the
    configured Gemini model (or None). Returns the validated dict
    or None — a game_id outside the grounding list is rejected."""
    if model is None:
        return None
    games = "\n".join(
        f"{g['id']} | {g['home']} vs {g['away']} | {g['league']} | {g['status']}"
        for g in grounding)
    prompt = _AI_PROMPT.replace("__GAMES__", games).replace("__TEXT__", text)
    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        raw = (getattr(response, "text", "") or "").strip()
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
    if action not in ("follow", "unfollow", "detail"):
        action = "follow"
    detail = data.get("detail")
    if detail not in (DETAIL_SIMPLE, DETAIL_ADVANCED):
        detail = DETAIL_SIMPLE
    return {"game_id": gid, "action": action, "detail": detail,
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


async def resolve_watch(text: str, model) -> dict:
    """Resolve free text to a game: game ID first, then exact/
    unique team-name match, then Gemini on the grounding list.
    Returns {"game_id"|None, "action", "detail", "candidates",
    "entry"|None, "source"}. Never invents an id."""
    q = (text or "").strip()
    if not q:
        return {"game_id": None, "action": "follow", "detail": DETAIL_SIMPLE,
                "candidates": [], "entry": None, "source": "none"}
    if q.isdigit() and len(q) >= 5:
        return {"game_id": q, "action": "follow", "detail": DETAIL_SIMPLE,
                "candidates": [], "entry": None, "source": "id"}
    grounding = await prov.onex_grounding()
    exact = [g for g in grounding
             if _norm(g["home"]) == _norm(q) or _norm(g["away"]) == _norm(q)]
    if len(exact) == 1:
        return {"game_id": str(exact[0]["id"]), "action": "follow",
                "detail": DETAIL_SIMPLE, "candidates": [], "entry": exact[0],
                "source": "name"}
    matches = _match_entries(q, grounding)
    if len(matches) == 1:
        return {"game_id": str(matches[0]["id"]), "action": "follow",
                "detail": DETAIL_SIMPLE, "candidates": [], "entry": matches[0],
                "source": "name"}
    ai = await parse_watch_ai(q, grounding, model)
    if ai and ai.get("game_id"):
        entry = next((g for g in grounding
                      if str(g["id"]) == str(ai["game_id"])), None)
        return {"game_id": str(ai["game_id"]), "action": ai["action"],
                "detail": ai["detail"], "candidates": [],
                "entry": entry, "source": "ai", "note": ai.get("note", "")}
    return {"game_id": None, "action": "follow", "detail": DETAIL_SIMPLE,
            "candidates": matches[:5], "entry": None, "source": "none"}


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


async def _send_to_channel(bot: discord.Client, channel_id: int,
                           text: str) -> bool:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await asyncio.wait_for(bot.fetch_channel(channel_id), 20)
        except Exception as exc:
            log.warning("[watch] channel %s unavailable (%s)", channel_id,
                        type(exc).__name__)
            return False
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
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
                        f"🏁 **{watch['home']} vs {watch['away']}** is no longer live "
                        f"on 1xBet — the watch has ended.")
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
            if watch.get("detail") == DETAIL_ADVANCED:
                table = format_stats_table(detail.get("stats"))
                if table:
                    text += "\n\n**Match stats**\n" + "\n".join(table)
            if await _send_to_channel(bot, channel_id, text):
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
