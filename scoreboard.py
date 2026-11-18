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
    The edit itself goes through the shared per-channel scheduler
    (channel_edits): the scoreboard shares the channel's 1 edit/s
    budget with the live panels instead of editing outside their
    timeline — on 18 Nov 2026 the uncoordinated scoreboard edits
    were half the Discord 429s in the test channel and each one
    stalled the panels ~4.2s. A 429 now pauses the whole channel
    (the scheduler owns the block), never just the scoreboard.

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

import channel_edits
import db
import history
import maintenance
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

# ── Test channel ONLY (incident 18 Nov 2026) ─────────────────────────
# The scoreboard may post in the test channel and nowhere else — never
# in #general or any other channel. On 18 Nov a /scoreboard run in
# #general registered that channel and the background loop kept
# editing its message there even though the command had reported
# failure. Every entry point below enforces this allowlist: the
# command, the first-post registration, the boot-resume load, and
# the loop's channel set. The Barcelona reminder and auto-watch are
# a separate system and are still allowed in #general.
TEST_CHANNEL_ID = 1572145824530563185
ALLOWED_SCOREBOARD_CHANNELS = frozenset({TEST_CHANNEL_ID})


def is_scoreboard_channel(channel_id: int) -> bool:
    """True only for the test channel — the one place the scoreboard
    is allowed to post."""
    return int(channel_id) in ALLOWED_SCOREBOARD_CHANNELS

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

# Serialises first starts so two concurrent /scoreboard runs in the
# same channel cannot both post a fresh message (one would be
# orphaned, never edited again).
_start_lock = asyncio.Lock()


# Set by _load_state when it drops a non-test channel from the
# saved file; ensure_running() persists the cleaned state at boot.
_purged_disallowed = False

# Channels explicitly stopped from Discord (/stopscoreboard).
# A stopped channel is NEVER re-posted — not by the loop's
# env-channel auto-add, not by a 404 re-post, not by a tick
# already in flight when the stop landed (18 Nov 2026: stop
# confirmed 'stopped' and a new Live Scores message appeared
# right after, because the loop re-added the env channel on
# its next tick and refresh_channel's setdefault resurrected
# the state). Persisted in scoreboard.json so a restart does
# not silently undo a stop either; /scoreboard clears it.
_stopped: set[int] = set()


def _load_state() -> dict[int, dict]:
    global _purged_disallowed
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
    for cid in data.get("stopped") or []:
        try:
            _stopped.add(int(cid))
        except (TypeError, ValueError):
            continue
    out: dict[int, dict] = {}
    for key, value in data.items():
        if key == "stopped":
            continue
        try:
            cid = int(key)
        except (TypeError, ValueError):
            continue
        if not is_scoreboard_channel(cid):
            # A channel that must never get a scoreboard (e.g. #general
            # from the 18 Nov 2026 incident) is dropped on load, so a
            # restart can never resume posting there.
            log.warning("[scoreboard] dropping non-test channel %s from "
                        "saved state; scoreboard is test-channel only", cid)
            _purged_disallowed = True
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
    payload = {"stopped": sorted(_stopped)}
    payload.update({str(k): v for k, v in _state.items()})
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


async def _post_scoreboard(bot: discord.Client, channel_id: int,
                           text: str) -> object | None:
    """Send a fresh scoreboard message (first post, or the stored
    one was deleted). A send, not an edit — sends do not go
    through the edit scheduler. Returns the message, or None."""
    channel = await _get_channel(bot, channel_id)
    if channel is None:
        return None
    try:
        return await channel.send(
            text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        log.warning("[scoreboard] post failed in %s (%s)",
                    channel_id, exc)
        return None


async def refresh_channel(bot: discord.Client, channel_id: int,
                          text: str) -> bool:
    """Post or edit the scoreboard in one channel, per the edit rule.
    Returns True when a send/edit happened. Never raises for Discord
    errors — a 429 backs off, a deleted message is re-posted once.
    Never posts in a channel that was stopped: a stop is checked
    here, not just in the loop, so a tick already in flight when
    the stop lands cannot resurrect the scoreboard.

    The edit goes through the shared per-channel scheduler
    (channel_edits) with a timeout that covers one rate-limit
    pause: the scheduler's raw PATCH returns a 429 at once and
    retries the job once after retry_after (~3–5s in
    production), so a 5s timeout lets the scoreboard's edit
    land after a 429 episode instead of being thrown away — the
    old 2.0s timeout was shorter than ONE discord.py retry
    sleep, so every scoreboard edit since a restart timed out
    and was discarded while resubmitting every tick. The
    scoreboard must still never stall the 1s loop for long:
    5s is the cap, and while the channel is paused the
    scheduler refuses at once instead of queueing. (The entry's
    own "blocked_until" field is retained only so old state
    files still load; the live 429 pause lives in the
    scheduler.)"""
    if channel_id in _stopped:
        return False
    now = time.time()
    body, _ = _split_body(text)
    entry = _state.setdefault(channel_id, _new_entry())
    if entry["message_id"] is not None and not should_edit(
            entry["body"], body, entry["last_edit"], now):
        return False
    if (channel_id in _stopped
            or _state.get(channel_id) is not entry):
        # Stopped while this tick was fetching, or the entry was
        # replaced by a fresh start — do not touch it.
        return False
    if entry["message_id"] is None:
        message = await _post_scoreboard(bot, channel_id, text)
        if message is None:
            return False
        entry["message_id"] = message.id
        entry["body"] = body
        entry["last_edit"] = now
        _save_state()
        return True
    status, _landed = await channel_edits.edit_message(
        bot, channel_id, entry["message_id"], content=text,
        timeout=5.0)
    if status == "gone":
        # Deleted between ticks — post a fresh one once, unless
        # the channel was stopped in the meantime.
        if (channel_id in _stopped
                or _state.get(channel_id) is not entry):
            return False
        entry["message_id"] = None
        message = await _post_scoreboard(bot, channel_id, text)
        if message is None:
            return False
        entry["message_id"] = message.id
        entry["body"] = body
        entry["last_edit"] = now
        _save_state()
        return True
    if status != "ok":
        # Failed, superseded, or the channel is paused after a
        # 429 — the next tick retries with fresh text.
        return False
    entry["body"] = body
    entry["last_edit"] = now
    _save_state()
    return True


async def _fetch_all() -> tuple[list[dict], list[dict], list[dict] | None]:
    """One watch tick: NPL + featured games + the raw live entries,
    cache bypassed, from exactly two 1xBet calls (onex_fetch_tick).
    A failed fetch is ([], [], None) — raw None, not []: an empty
    list would read as "nothing is live", while None says "the
    fetch failed, we know nothing". The loop never dies on provider
    errors, and the watches (watch.on_tick) must never count a
    failed tick as their game vanishing. The raw entries are shared
    with the game watches (watch.on_tick) so there is no second
    1-second poller."""
    try:
        npl, games, raw, ok = await onex_fetch_tick()
    except Exception as exc:
        log.warning("[scoreboard] tick fetch failed (%s)", type(exc).__name__)
        return [], [], None
    if not ok:
        # Both live lists failed — npl/games/raw are empty because
        # the fetch failed, not because nothing is live.
        return [], [], None
    return npl, games, raw


async def scoreboard_loop(bot: discord.Client) -> None:
    """Watch 1xBet every second; edit only when the score changes.

    Strict 1s schedule: each tick STARTS exactly `interval`
    after the previous tick STARTED — not `interval` after it
    finished. The old shape (fetch, work, then sleep 1s) drifted
    by the fetch latency on every tick, so its period was ~1.3s
    and a single live panel edited ~46 times a minute instead
    of ~60 — /game did not refresh at 1 second. On an overrun
    the next tick starts at once (no catch-up burst); the panel
    workers still space edits 1s apart per channel, so a fast
    tick can never produce two identical whole-second footers
    in a row."""
    global last_tick_ts, last_good_tick_ts, _stale_episode, _last_cleanup_ts
    log.info("[scoreboard] watch started (1s poll, edits only on change)")
    await bot.wait_until_ready()
    interval = WATCH_SECONDS
    empty_ticks = 0
    loop = asyncio.get_running_loop()
    next_tick = loop.time()
    while not bot.is_closed():
        await asyncio.sleep(max(0.0, next_tick - loop.time()))
        tick_start = loop.time()
        try:
            if await maintenance.is_on():
                # Silent: no fetch, no edits, no watches. The loop
                # keeps its cadence so a resume picks up at once;
                # the tick timestamp still moves so /health does not
                # report a false stale episode after a resume.
                last_tick_ts = time.monotonic()
                next_tick = tick_start + interval
                continue
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
            channels = {cid for cid in _state
                        if is_scoreboard_channel(cid)}
            if (SCOREBOARD_CHANNEL_ID
                    and is_scoreboard_channel(SCOREBOARD_CHANNEL_ID)
                    and SCOREBOARD_CHANNEL_ID not in _stopped):
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
        next_tick = tick_start + interval


def ensure_running(bot: discord.Client) -> None:
    global _purged_disallowed
    if _purged_disallowed:
        # A non-test channel was dropped from the saved state at load
        # — persist the cleaned state at once so the file can never
        # bring it back.
        _purged_disallowed = False
        _save_state()
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


def is_running(channel_id: int) -> bool:
    """True when the scoreboard is currently registered in a channel."""
    return int(channel_id) in _state


async def start_for_channel(bot: discord.Client, channel_id: int) -> bool:
    """Register a channel and post/resume its scoreboard immediately.

    Returns True when the scoreboard is running in the channel
    after this call — including when it was ALREADY running and
    no edit was needed. The old code returned refresh_channel's
    result in that case, and refresh_channel returns False when
    the text has not changed (or the 5s edit gap has not passed),
    so /scoreboard reported 'Could not post' for a scoreboard
    that was already live (18 Nov 2026: three times in the test
    channel). A start that finds a running scoreboard is a
    success, not a failure.
    The channel is registered ONLY after a first successful post, so
    a failed start can never leave a live scoreboard behind. The old
    code registered + saved the channel first and then posted — and
    it also unpacked _fetch_all()'s 3-tuple as 2 values, so it raised
    ValueError on EVERY run. On 18 Nov 2026 that is exactly what
    happened in #general: the command reported failure while the
    background loop picked the registered channel up on its next
    tick and posted there anyway.
    """
    if not is_scoreboard_channel(channel_id):
        log.warning("[scoreboard] refused start in non-test channel %s "
                    "(scoreboard is test-channel only)", channel_id)
        return False
    async with _start_lock:
        npl, games, _raw = await _fetch_all()
        text = render_scoreboard(npl, games)
        if channel_id in _state:
            # Already running here: resume/edit via the normal path.
            # A no-edit-needed result is still a running scoreboard.
            try:
                await refresh_channel(bot, channel_id, text)
            except Exception as exc:
                # refresh_channel only catches NotFound/HTTPException,
                # so a transient transport error (aiohttp/timeout) can
                # still escape — log it, but the scoreboard is still
                # running (the loop keeps editing it).
                log.warning("[scoreboard] start edit failed in %s (%s)",
                            channel_id, type(exc).__name__)
            ensure_running(bot)
            return True
        # First start here: post ONCE without registering. Only a
        # successful send creates the state entry, so the loop cannot
        # pick this channel up before the command has succeeded.
        channel = await _get_channel(bot, channel_id)
        if channel is None:
            return False
        try:
            message = await channel.send(
                text, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            # Broad catch on purpose (see above): a failed first post
            # simply reports False and registers nothing.
            log.warning("[scoreboard] first post failed in %s (%s)",
                        channel_id, type(exc).__name__)
            return False
        _state[channel_id] = {
            "message_id": message.id,
            "body": _split_body(text)[0],
            "last_edit": time.time(),
            "blocked_until": 0.0,
        }
        # A successful start clears any earlier stop (persisted
        # with the state, so a restart cannot bring it back).
        _stopped.discard(channel_id)
        _save_state()
        ensure_running(bot)
        return True


async def stop_for_channel(bot: discord.Client, channel_id: int) -> bool:
    """Stop the scoreboard in one channel, from Discord (/stopscoreboard).

    Removes the channel from state, so the loop stops editing there
    on its next tick, and best-effort deletes the posted message so
    nothing frozen is left behind. True when a scoreboard was
    actually running in that channel. The loop itself keeps running —
    it also drives the game watches — only this channel is removed.
    Deletion failures are ignored: state removal is what stops the
    edits, and the message is the bot's own so no special permission
    is needed to delete it.
    The channel is marked stopped FIRST (under the lock, before
    the state is removed), so a tick already in flight — or the
    loop's env-channel auto-add on its next tick — cannot post a
    fresh message after the stop. The stop is persisted, so a
    restart does not silently undo it; only /scoreboard clears it.
    """
    entry = None
    async with _start_lock:
        _stopped.add(channel_id)
        entry = _state.get(channel_id)
        if entry is not None:
            del _state[channel_id]
        _save_state()
    if entry is None:
        return False
    msg_id = entry.get("message_id")
    if msg_id is not None:
        try:
            channel = await _get_channel(bot, channel_id)
            if channel is not None:
                message = await channel.fetch_message(msg_id)
                await message.delete()
        except Exception:
            pass
    if entry is not None:
        log.info("[scoreboard] stopped in channel %s", channel_id)
    return entry is not None
