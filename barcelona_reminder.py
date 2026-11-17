"""Barcelona match reminders for #general."""
import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import discord
from providers import free_fixtures_for_day

log = logging.getLogger("barcelona_reminder")
NEPAL = ZoneInfo("Asia/Kathmandu")
STATE_PATH = Path(__file__).resolve().with_name("barcelona_reminders.json")
POLL_SECONDS = 300
LOOKAHEAD_DAYS = 8
GENERAL_CHANNEL_ID = int(os.getenv("BARCELONA_REMINDER_CHANNEL_ID", "0"))
last_poll_at: datetime | None = None
last_poll_result: dict | None = None
last_failure_alert_at: datetime | None = None

def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"sent": [], "pending": {}}
    if isinstance(data, list):
        data = {"sent": data, "pending": {}}
    if (not isinstance(data, dict) or not isinstance(data.get("sent"), list)
            or not all(isinstance(x, str) for x in data["sent"])
            or not isinstance(data.get("pending"), dict)):
        raise ValueError("Invalid reminder ledger; refusing to discard receipts")
    return data

def _load_sent() -> set[str]:
    return set(_load_state()["sent"])

def _save_state(state: dict) -> None:
    """Durably replace the ledger without leaving a partial JSON file."""
    fd, name = tempfile.mkstemp(prefix=".barcelona-", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATE_PATH)
        if os.name != "nt":
            directory_fd = os.open(STATE_PATH.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def _kickoff(match: dict) -> datetime | None:
    value = match.get("kickoff") or match.get("date")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

def _is_barcelona(match: dict) -> bool:
    teams = f"{match.get('home', '')} {match.get('away', '')}".lower()
    return "barcelona" in teams and "barcelona b" not in teams

def _send_at(kickoff: datetime) -> datetime:
    local = kickoff.astimezone(NEPAL)
    evening_before = datetime.combine(local.date() - timedelta(days=1), time(21, 30), tzinfo=NEPAL)
    if local.time() <= time(10, 0):
        return evening_before
    return local - timedelta(hours=12)

def _key(match: dict, kickoff: datetime) -> str:
    return str(match.get("id") or f"{match.get('home')}|{match.get('away')}|{kickoff.isoformat()}")

def _stable_key(match: dict, kickoff: datetime) -> str:
    value = f"{str(match.get('home', '')).lower()}|{str(match.get('away', '')).lower()}|{kickoff.astimezone(timezone.utc).isoformat()}"
    return "barca-" + hashlib.sha256(value.encode()).hexdigest()[:24]

async def _general_channels(bot: discord.Client) -> list[discord.abc.Messageable]:
    channel = bot.get_channel(GENERAL_CHANNEL_ID) if GENERAL_CHANNEL_ID else None
    if channel is None and GENERAL_CHANNEL_ID:
        channel = await asyncio.wait_for(bot.fetch_channel(GENERAL_CHANNEL_ID), 20)
    if isinstance(channel, discord.TextChannel) and channel.guild.me and channel.permissions_for(channel.guild.me).send_messages:
        return [channel]
    log.warning('[barcelona] configured reminder channel is unavailable or not writable')
    return []

async def _deliver(bot, match, kickoff, now) -> str:
    state = _load_state()
    marker = _stable_key(match, kickoff)
    if marker in state["sent"] or _key(match, kickoff) in state["sent"]:
        return "already_sent"
    channels = await _general_channels(bot)
    if not channels:
        raise RuntimeError("Reminder channel unavailable")
    channel = channels[0]
    if marker in state["pending"]:
        # Resolve a crash/timeout after Discord may have accepted the message.
        after = datetime.fromisoformat(state["pending"][marker]) - timedelta(seconds=10)
        async def find_receipt():
            async for message in channel.history(limit=None, after=after):
                if message.author.id == bot.user.id and marker in message.content:
                    return True
            return False
        if await asyncio.wait_for(find_receipt(), 30):
            state["sent"].append(marker)
            state["pending"].pop(marker, None)
            _save_state(state)
            return "reconciled"
    else:
        # Record intent before sending so restart can resolve an uncertain send.
        state["pending"][marker] = now.isoformat()
        _save_state(state)
    local = kickoff.astimezone(NEPAL)
    message = (
        "[Barcelona match reminder]\n"
        f"**{match.get('home', 'TBD')} vs {match.get('away', 'TBD')}**\n"
        f"Kickoff: <t:{int(kickoff.timestamp())}:F> "
        f"({local.strftime('%I:%M %p')} Nepal time)\n"
        f"-# Reminder reference: {marker}"
    )
    await asyncio.wait_for(channel.send(message, allowed_mentions=discord.AllowedMentions.none()), 30)
    state["sent"].append(marker)
    state["pending"].pop(marker, None)
    _save_state(state)
    return "sent"

async def poll_once(bot, now=None) -> dict:
    fixed_now = now
    now = now or datetime.now(NEPAL)
    result = {"fixtures": 0, "due": 0, "sent": 0, "already_sent": 0, "reconciled": 0, "errors": 0}
    # UTC provider dates can be yesterday for an early-morning Nepal kickoff.
    days = [(now.date() + timedelta(days=i)).isoformat() for i in range(-1, LOOKAHEAD_DAYS)]
    async def fetch(day):
        try:
            return await asyncio.wait_for(free_fixtures_for_day(140, day), 45)
        except Exception as exc:
            result["errors"] += 1
            log.warning("[barcelona] fixture lookup failed (%s)", type(exc).__name__)
            return []
    batches = await asyncio.gather(*(fetch(day) for day in days))
    seen = set()
    for fixtures in batches:
        for match in fixtures:
            if not _is_barcelona(match):
                continue
            kickoff = _kickoff(match)
            if kickoff is None or _stable_key(match, kickoff) in seen:
                continue
            seen.add(_stable_key(match, kickoff))
            result["fixtures"] += 1
            check_time = fixed_now or datetime.now(NEPAL)
            if not (_send_at(kickoff) <= check_time < kickoff):
                continue
            if str(match.get("status", "")).lower() in {"ft", "finished", "cancelled", "canc", "postponed", "pst"}:
                continue
            result["due"] += 1
            try:
                result[await _deliver(bot, match, kickoff, check_time)] += 1
            except Exception as exc:
                result["errors"] += 1
                log.error("[barcelona] delivery failed (%s); will retry", type(exc).__name__)
    global last_poll_at, last_poll_result
    last_poll_at = datetime.now(NEPAL)
    last_poll_result = dict(result)
    log.info("[barcelona] poll complete %s", result)
    return result

def health_snapshot(bot) -> dict:
    task = getattr(bot, "barcelona_task", None)
    return {
        "worker": "running" if task is not None and not task.done() else "stopped",
        "last_poll": last_poll_at.isoformat() if last_poll_at else "not completed yet",
        "last_result": last_poll_result or {},
        "channel_configured": bool(GENERAL_CHANNEL_ID),
    }

async def _alert_failure(bot, result: dict) -> None:
    """Keep diagnostics in logs; fixture outages must not spam Discord channels."""
    if not result.get("errors"):
        return
    log.warning(
        "[barcelona] reminder lookup had %d temporary error(s); "
        "retrying automatically without posting a channel alert",
        result["errors"],
    )
async def poll_loop(bot: discord.Client) -> None:
    log.info("[barcelona] reminder worker started; waiting for Discord")
    while not bot.is_closed():
        try:
            await bot.wait_until_ready()
            bot.barcelona_wake.clear()
            result = await poll_once(bot)
            if result is not None:
                await _alert_failure(bot, result)
            try:
                await asyncio.wait_for(bot.barcelona_wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[barcelona] worker failure (%s); retrying", type(exc).__name__)
            await asyncio.sleep(30)

def ensure_running(bot) -> None:
    if not hasattr(bot, "barcelona_wake"):
        bot.barcelona_wake = asyncio.Event()
    task = getattr(bot, "barcelona_task", None)
    if task is None or task.done():
        if task is not None and not task.cancelled() and task.exception() is not None:
            log.error("[barcelona] restarting failed worker")
        bot.barcelona_task = asyncio.create_task(poll_loop(bot), name="barcelona-reminders")
    bot.barcelona_wake.set()
