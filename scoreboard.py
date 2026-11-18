"""Live scoreboard message for Discord — NPL cricket + top football games.

WATCH vs EDIT — the two cadences are deliberately different:
  - WATCH: 1xBet is polled every 1 second (WATCH_SECONDS), like the
    website's scoreboard. Each poll reaches 1xBet — the scoreboard
    fetch skips http_client's cache (onex_live_matches(use_cache=False)
    and onex_npl_live's cache_key=""), so no cached body can make the
    watch stale.
  - EDIT: the Discord message is edited ONLY when the rendered score
    text changes, with at least MIN_EDIT_GAP (5s) between edits, plus
    a forced refresh every FORCE_REFRESH_SECONDS (5 min) so the
    "Updated HH:MM:SS" footer stays fresh. Discord rate-limits message
    edits; editing every second would get the bot blocked. Watching
    every second is fine — a score only changes on a goal or a ball.

If 1xBet stops answering with live data for several ticks in a row
(throttled, blocked, or genuinely no games live), the watch backs off
to BACKOFF_SECONDS (10s) and logs it, instead of hammering the
endpoint. Live data coming back restores the 1s watch at once.
http_client already retries 429s with backoff and logs them.

State: {channel_id: message_id} (+ last body / last edit) lives in
scoreboard.json next to this file, saved atomically like the Barcelona
reminder ledger. On restart the loop resumes editing the stored
message instead of posting a new one; if the message was deleted
(404) it posts a fresh one once and updates the stored id.
"""
import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord

import db
import history
import providers as prov
from providers import onex_fetch_tick
import watch

# Health-monitor state (Feature 7): monotonic timestamps the
# /health snapshot reads. last_tick_ts moves every loop iteration
# (even a failed one); last_good_tick_ts only when the tick
# actually returned live entries.
last_tick_ts: float = 0.0
last_good_tick_ts: float = 0.0
STALE_AFTER_S = 60.0
_stale_episode = False
# Feature 6: retention cleanup (30 days, history tables only)
# runs at most once an hour from the existing loop — no new task.
_last_cleanup_ts: float = 0.0
_CLEANUP_EVERY_S = 3600.0

log = logging.getLogger("scoreboard")
NEPAL = ZoneInfo("Asia/Kathmandu")
STATE_PATH = Path(__file__).resolve().with_name("scoreboard.json")

# Auto-start channel (env, like BARCELONA_REMINDER_CHANNEL_ID).
# 0 = off; the parent sets this to the test channel at deploy time.
SCOREBOARD_CHANNEL_ID = int(os.getenv("SCOREBOARD_CHANNEL_ID", "0"))

WATCH_SECONDS = 1            # 1xBet poll interval (the watch)
BACKOFF_SECONDS = 10         # watch interval while 1xBet gives no live data
MIN_EDIT_GAP = 5             # seconds between Discord message edits
FORCE_REFRESH_SECONDS = 300  # 5 min: refresh even with no score change
EMPTY_TICKS_BEFORE_BACKOFF = 3
MAX_TOP_GAMES = 5

FOOTER_PREFIX = "-# Updated "

# channel_id -> {"message_id": int | None, "body": str | None,
#                "last_edit": float (epoch), "blocked_until": float (epoch)}
_state: dict[int, dict] = {}


def _load_state() -> dict[int, dict]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("[scoreboard] unreadable state file (%s); starting fresh",
                    type(exc).__name__)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, dict] = {}
    for key, value in data.items():
        try:
            cid = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        msg_id = value.get("message_id")
        out[cid] = {
            "message_id": msg_id if isinstance(msg_id, int) else None,
            "body": value.get("body") if isinstance(value.get("body"), str) else None,
            "last_edit": float(value.get("last_edit") or 0),
            "blocked_until": float(value.get("blocked_until") or 0),
        }
    return out


def _save_state() -> None:
    """Durably replace the state file without leaving a partial JSON file."""
    payload = {str(k): v for k, v in _state.items()}
    fd, name = tempfile.mkstemp(prefix=".scoreboard-", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATE_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


_state = _load_state()


# ══════════════════════════════════════════════════════════════════════════
# RENDERING (pure — no Discord, no network)
# ══════════════════════════════════════════════════════════════════════════

def _score_str(m: dict) -> str:
    hs, aws = m.get("home_score"), m.get("away_score")
    if hs is None and aws is None:
        return "vs"
    return f"{hs if hs is not None else '?'}–{aws if aws is not None else '?'}"


def _clock_str(m: dict) -> str:
    """Overs for cricket, minute for football, status otherwise."""
    if m.get("clock"):
        return str(m["clock"])
    minute = m.get("minute")
    if isinstance(minute, int):
        return f"{minute}'"
    status = str(m.get("status") or "")
    if status == "HT":
        return "Half-time"
    if status == "FT":
        return "Full-time"
    if status in ("1H", "2H", "ET", "PEN"):
        return status
    return "Live"


def _match_line(m: dict, show_league: bool = False) -> str:
    line = f"**{m.get('home', '?')} {_score_str(m)} {m.get('away', '?')}** · {_clock_str(m)}"
    cricket = m.get("cricket") or {}
    if cricket.get("target") is not None and cricket.get("need") is not None:
        if cricket.get("balls_left") is not None:
            need = f"need {cricket['need']} off {cricket['balls_left']} balls"
        else:
            need = f"need {cricket['need']}"
        line += f" · Target {cricket['target']} ({need})"
    if show_league and m.get("league"):
        line += f" · {m['league']}"
    return line


def render_scoreboard(npl: list[dict] | None, games: list[dict] | None,
                      now: datetime | None = None) -> str:
    """One plain-text message (not an embed) so diffing is trivial."""
    now = now or datetime.now(NEPAL)
    lines = ["📊 **Live Scores**", ""]
    npl_live = [m for m in (npl or []) if not m.get("finished")]
    if npl_live:
        lines.append("🏏 **Nepal Premier League**")
        for m in npl_live:
            lines.append(_match_line(m))
        lines.append("")
    top = [m for m in (games or []) if not m.get("finished")]
    if top:
        lines.append("⚽ **Top games**")
        for m in top[:MAX_TOP_GAMES]:
            lines.append(_match_line(m, show_league=True))
        lines.append("")
    if not npl_live and not top:
        lines.append("No live matches right now.")
        lines.append("")
    lines.append(f"{FOOTER_PREFIX}{now.strftime('%H:%M:%S')} NPT · edits only when the score changes")
    return "\n".join(lines)


def _split_body(text: str) -> tuple[str, str]:
    """(score body, footer line). The footer timestamp changes on every
    render, so change detection compares the body only."""
    head, sep, tail = text.rpartition("\n")
    if sep and tail.startswith(FOOTER_PREFIX):
        return head, tail
    return text, ""


def should_edit(last_body: str | None, new_body: str,
                last_edit: float, now: float,
                blocked_until: float = 0) -> bool:
    """Edit when the score body changed (and the 5s gap has passed),
    or when the 5-minute forced refresh is due. Never on a 1s cadence."""
    if now < blocked_until:
        return False
    if last_body is None:
        return True
    gap = now - (last_edit or 0)
    if new_body != last_body and gap >= MIN_EDIT_GAP:
        return True
    if gap >= FORCE_REFRESH_SECONDS:
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════
# DISCORD DELIVERY
# ══════════════════════════════════════════════════════════════════════════

def _new_entry() -> dict:
    return {"message_id": None, "body": None, "last_edit": 0.0,
            "blocked_until": 0.0}


async def _get_channel(bot: discord.Client, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await asyncio.wait_for(bot.fetch_channel(channel_id), 20)
        except Exception as exc:
            log.warning("[scoreboard] channel %s unavailable (%s)",
                        channel_id, type(exc).__name__)
            return None
    return channel


async def refresh_channel(bot: discord.Client, channel_id: int,
                          text: str) -> bool:
    """Post or edit the scoreboard in one channel, per the edit rule.
    Returns True when a send/edit happened. Never raises for Discord
    errors — a 429 backs off, a deleted message is re-posted once."""
    now = time.time()
    body, _ = _split_body(text)
    entry = _state.setdefault(channel_id, _new_entry())
    if entry["message_id"] is not None and not should_edit(
            entry["body"], body, entry["last_edit"], now,
            entry.get("blocked_until") or 0):
        return False
    channel = await _get_channel(bot, channel_id)
    if channel is None:
        return False
    message = None
    if entry["message_id"] is not None:
        try:
            message = await channel.fetch_message(entry["message_id"])
        except discord.NotFound:
            log.info("[scoreboard] stored message gone in %s; posting fresh",
                     channel_id)
            entry["message_id"] = None
        except discord.HTTPException as exc:
            log.warning("[scoreboard] cannot fetch message in %s (%s)",
                        channel_id, exc)
            return False
    try:
        if message is not None:
            await message.edit(content=text,
                               allowed_mentions=discord.AllowedMentions.none())
        else:
            message = await channel.send(
                text, allowed_mentions=discord.AllowedMentions.none())
    except discord.NotFound:
        # Deleted between fetch and edit — post a fresh one once.
        try:
            message = await channel.send(
                text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("[scoreboard] re-post failed in %s (%s)", channel_id, exc)
            return False
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) == 429:
            retry_after = getattr(exc, "retry_after", None) or 60
            entry["blocked_until"] = now + float(retry_after)
            _save_state()
            log.warning("[scoreboard] Discord rate-limited edits in %s; "
                        "paused for %ss", channel_id, retry_after)
        else:
            log.warning("[scoreboard] edit/send failed in %s (%s)",
                        channel_id, exc)
        return False
    entry["message_id"] = message.id
    entry["body"] = body
    entry["last_edit"] = now
    entry["blocked_until"] = 0.0
    _save_state()
    return True


async def _fetch_all() -> tuple[list[dict], list[dict], list[dict]]:
    """One watch tick: NPL + featured games + the raw live entries,
    cache bypassed, from exactly two 1xBet calls (onex_fetch_tick).
    A failed fetch is [] — the loop never dies on provider errors.
    The raw entries are shared with the game watches (watch.on_tick)
    so there is no second 1-second poller."""
    try:
        npl, games, raw = await onex_fetch_tick()
    except Exception as exc:
        log.warning("[scoreboard] tick fetch failed (%s)", type(exc).__name__)
        npl, games, raw = [], [], []
    return npl, games, raw


async def scoreboard_loop(bot: discord.Client) -> None:
    """Watch 1xBet every second; edit only when the score changes."""
    global last_tick_ts, last_good_tick_ts, _stale_episode, _last_cleanup_ts
    log.info("[scoreboard] watch started (1s poll, edits only on change)")
    await bot.wait_until_ready()
    interval = WATCH_SECONDS
    empty_ticks = 0
    while not bot.is_closed():
        try:
            npl, games, raw = await _fetch_all()
            last_tick_ts = time.monotonic()
            if last_tick_ts - _last_cleanup_ts >= _CLEANUP_EVERY_S:
                _last_cleanup_ts = last_tick_ts
                asyncio.create_task(history.cleanup_old_history())
            if raw:
                last_good_tick_ts = last_tick_ts
                if _stale_episode:
                    _stale_episode = False
                    log.info("[scoreboard] live data again after stale "
                             "episode — tick cache recovered")
            elif (last_good_tick_ts
                    and last_tick_ts - last_good_tick_ts > STALE_AFTER_S
                    and not _stale_episode):
                # Stale data: no live entries for over a minute. Log
                # the recovery attempt and reset the tick cache once
                # per episode (never loop, never restart the bot).
                _stale_episode = True
                log.warning("[scoreboard] no live data for >%ds — "
                            "resetting tick cache once", STALE_AFTER_S)
                prov._onex_reset_caches()
                try:
                    await db.log_health(
                        "scoreboard", "warning",
                        "no live data for over 60s — tick cache reset "
                        f"(empty_ticks={empty_ticks})")
                except Exception:
                    pass
            text = render_scoreboard(npl, games)
            # Game watches share this tick's raw data.
            await watch.on_tick(bot, raw)
            if not npl and not games:
                empty_ticks += 1
                if (empty_ticks >= EMPTY_TICKS_BEFORE_BACKOFF
                        and interval != BACKOFF_SECONDS):
                    interval = BACKOFF_SECONDS
                    log.warning(
                        "[scoreboard] no live data for %d ticks — 1xBet may "
                        "be throttling/blocking, or no games are live; "
                        "watch backed off to %ds",
                        empty_ticks, BACKOFF_SECONDS)
            else:
                if interval != WATCH_SECONDS:
                    log.info("[scoreboard] live data again — watch back to %ds",
                             WATCH_SECONDS)
                interval = WATCH_SECONDS
                empty_ticks = 0
            channels = set(_state)
            if SCOREBOARD_CHANNEL_ID:
                channels.add(SCOREBOARD_CHANNEL_ID)
            for cid in list(channels):
                try:
                    await refresh_channel(bot, cid, text)
                except Exception as exc:
                    log.warning("[scoreboard] channel %s failed (%s)",
                                cid, type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[scoreboard] tick failed (%s); continuing",
                      type(exc).__name__)
        await asyncio.sleep(interval)


def ensure_running(bot: discord.Client) -> None:
    task = getattr(bot, "scoreboard_task", None)
    if task is None or task.done():
        if task is not None and not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            log.error("[scoreboard] restarting failed worker")
            # Feature 7: record the recovery attempt (never raise
            # here — a logging failure must not break the restart).
            # Local import: health imports scoreboard, so a top-level
            # import would be circular.
            try:
                import health
                asyncio.create_task(health.log_recovery(
                    "scoreboard", "scoreboard worker failed; restarted",
                    {"error": type(exc).__name__} if exc else None))
            except Exception:
                pass
        bot.scoreboard_task = asyncio.create_task(
            scoreboard_loop(bot), name="scoreboard")


async def start_for_channel(bot: discord.Client, channel_id: int) -> bool:
    """Register a channel and post/resume its scoreboard immediately."""
    _state.setdefault(channel_id, _new_entry())
    _save_state()
    ensure_running(bot)
    npl, games = await _fetch_all()
    return await refresh_channel(bot, channel_id,
                                 render_scoreboard(npl, games))
