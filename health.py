"""Health monitor & self-healing (Feature 7).

snapshot() builds the one markdown health summary the
coordinator wires to /health; log_recovery() records a recovery
attempt for the coordinator to call from bot.py's ensure_running
worker-restart paths; stale_check() gives /health a short
provider-staleness warning.

Every section is guarded: snapshot() never raises, and a failed
section prints "unknown" instead of taking the whole summary
down. No provider name appears in any visible string.
"""
import json
import logging
import math
import time
from datetime import datetime, timezone

import db
import providers
import scoreboard
import watch

log = logging.getLogger("health")

# Thresholds (seconds) for the overall status.
TICK_YELLOW_S = 10.0
TICK_RED_S = 30.0
PROVIDER_YELLOW_S = 60.0
PROVIDER_RED_S = 120.0
# Recovery counts look back this far.
RECOVERY_WINDOW_S = 86400  # last 24h


def _fmt_age(age_s: float | None) -> str:
    if age_s is None:
        return "unknown"
    if age_s < 1:
        return "just now"
    if age_s < 60:
        return f"{age_s:.0f}s ago"
    return f"{age_s / 60:.1f} min ago"


def _uptime_text(bot) -> str:
    """Age of bot.start_time when the coordinator sets it."""
    start = getattr(bot, "start_time", None)
    if start is None:
        return "unknown — started before this check was added"
    try:
        if isinstance(start, datetime):
            now = datetime.now(timezone.utc)
            began = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            delta = now - began
            if delta.total_seconds() < 0:
                return "unknown"
            days, rem = divmod(int(delta.total_seconds()), 86400)
            hours, rem = divmod(rem, 3600)
            minutes, _secs = divmod(rem, 60)
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            parts.append(f"{minutes}m")
            return " ".join(parts)
        if isinstance(start, (int, float)):
            return _fmt_age(time.monotonic() - float(start)) \
                if float(start) < 1e9 else _fmt_age(time.time() - float(start))
    except Exception:
        pass
    return str(start)


def _latency_text(bot) -> tuple[str, float | None]:
    """(display text, latency seconds or None when not connected)."""
    try:
        latency = bot.latency
    except Exception:
        return "unknown", None
    if latency is None:
        return "not connected", None
    try:
        if isinstance(latency, float) and math.isnan(latency):
            return "not connected", None
        return f"{round(float(latency) * 1000)} ms", float(latency)
    except (TypeError, ValueError):
        return "unknown", None


def _barca_text(bot) -> str:
    """Barça reminder worker state, or "unknown" when the module
    is not importable."""
    try:
        import barcelona_reminder
        snap = barcelona_reminder.health_snapshot(bot)
    except Exception:
        return "unknown"
    try:
        return (f"worker {snap.get('worker', 'unknown')} · "
                f"last poll {snap.get('last_poll', 'unknown')} · "
                f"channel {'configured' if snap.get('channel_configured') else 'missing'}")
    except Exception:
        return "unknown"


async def _recovery_counts() -> str:
    """Warning / error / recovery rows in the last 24h."""
    try:
        since = int(time.time()) - RECOVERY_WINDOW_S
        warnings = await db.count_health("warning", since)
        errors = await db.count_health("error", since)
        recoveries = await db.count_health("recovery", since)
        return (f"{warnings} warning(s), {errors} error(s), "
                f"{recoveries} recoveries in the last 24h")
    except Exception:
        return "unknown"


async def snapshot(bot) -> str:
    """One markdown health summary. Never raises: each section
    is wrapped, and a failed section prints "unknown"."""
    now_mono = time.monotonic()
    lines: list[str] = []

    # ── raw signals (each guarded) ──────────────────────────
    latency_text, latency = "unknown", None
    try:
        latency_text, latency = _latency_text(bot)
    except Exception:
        pass
    tick_age: float | None = None
    tick_text = "unknown"
    try:
        last_tick = float(getattr(scoreboard, "last_tick_ts", 0.0) or 0.0)
        if last_tick == 0.0:
            tick_text = "scoreboard has not ticked yet"
            tick_age = float("inf")
        else:
            tick_age = now_mono - last_tick
            tick_text = f"last tick {_fmt_age(tick_age)}"
    except Exception:
        tick_text = "unknown"
    provider_age: float | None = None
    provider_text = "unknown"
    try:
        last_ok = float(providers.onex_last_success_ts() or 0.0)
        if last_ok == 0.0:
            provider_text = "never"
            provider_age = float("inf")
        else:
            provider_age = now_mono - last_ok
            provider_text = f"last update {_fmt_age(provider_age)}"
    except Exception:
        provider_text = "unknown"
    recent: list[dict] = []
    try:
        recent = await db.get_recent_health(limit=5)
    except Exception:
        recent = []
    has_warning = any((r.get("kind") == "warning") for r in recent)

    # ── overall status ──────────────────────────────────────
    if (latency is None
            or (tick_age is not None and tick_age > TICK_RED_S)
            or (provider_age is not None and provider_age > PROVIDER_RED_S)):
        status, status_word = "🔴", "unhealthy"
    elif ((tick_age is not None and tick_age > TICK_YELLOW_S)
          or (provider_age is not None and provider_age > PROVIDER_YELLOW_S)
          or has_warning):
        status, status_word = "🟡", "degraded"
    else:
        status, status_word = "🟢", "healthy"
    lines.append(f"{status} **Bot {status_word}**")
    lines.append("")

    # ── uptime ──────────────────────────────────────────────
    try:
        lines.append(f"**Uptime:** {_uptime_text(bot)}")
    except Exception:
        lines.append("**Uptime:** unknown")

    # ── Discord ─────────────────────────────────────────────
    try:
        lines.append(f"**Discord:** {latency_text}")
    except Exception:
        lines.append("**Discord:** unknown")

    # ── scoreboard worker ───────────────────────────────────
    try:
        task = getattr(bot, "scoreboard_task", None)
        worker = ("running" if task is not None and not task.done()
                  else "stopped")
        lines.append(f"**Scoreboard:** {worker} · {tick_text}")
    except Exception:
        lines.append("**Scoreboard:** unknown")

    # ── live-scores freshness ───────────────────────────────
    try:
        lines.append(f"**Live scores data:** {provider_text}")
    except Exception:
        lines.append("**Live scores data:** unknown")

    # ── watches ─────────────────────────────────────────────
    try:
        n_watches = len(watch._watches)
        try:
            follows = await db.get_all_team_follows()
            n_follows = len(follows)
        except Exception:
            n_follows = len(getattr(watch, "_team_follows", {}) or {})
        lines.append(f"**Watches:** {n_watches} game(s), "
                     f"{n_follows} team follow(s)")
    except Exception:
        lines.append("**Watches:** unknown")

    # ── Barça reminder ──────────────────────────────────────
    try:
        lines.append(f"**Barcelona reminder:** {_barca_text(bot)}")
    except Exception:
        lines.append("**Barcelona reminder:** unknown")

    # ── recent health log ───────────────────────────────────
    try:
        if recent:
            lines.append("**Recent health log:**")
            for row in recent:
                detail = str(row.get("detail") or "")
                lines.append(f"• {row.get('component', '?')} "
                             f"[{row.get('kind', '?')}] {detail}".rstrip())
        else:
            lines.append("**Recent health log:** no entries")
    except Exception:
        lines.append("**Recent health log:** unknown")

    # ── recovery attempts ────────────────────────────────────
    try:
        lines.append(f"**Recovery attempts:** {await _recovery_counts()}")
    except Exception:
        lines.append("**Recovery attempts:** unknown")

    return "\n".join(lines)


async def log_recovery(component: str, message: str,
                       details: dict | None = None) -> None:
    """Record a recovery attempt in the health log. The
    coordinator calls this from bot.py's ensure_running
    worker-restart paths (scoreboard / reminder workers) so a
    restart leaves a trace in /health. Never raises — a failed
    write is logged and swallowed.

    Note: db.log_health takes (component, kind, detail) with
    detail a plain string, so a details dict is JSON-encoded
    onto the message instead of a fourth argument."""
    try:
        detail = message
        if details:
            try:
                detail = f"{message} | {json.dumps(details, default=str)}"
            except Exception:
                detail = message
        await db.log_health(component, "recovery", detail)
    except Exception as exc:
        log.warning("[health] log_recovery failed (%s)", type(exc).__name__)


async def stale_check() -> str | None:
    """Short warning when the live-scores feed is stale (last
    successful fetch over 60s ago), else None. For the
    coordinator to use in /health. Never raises."""
    try:
        last_ok = float(providers.onex_last_success_ts() or 0.0)
    except Exception:
        return None
    if last_ok == 0.0:
        return "⚠️ Live scores feed has not returned data yet."
    age = time.monotonic() - last_ok
    if age > PROVIDER_YELLOW_S:
        return f"⚠️ Live scores data is stale (last update {age:.0f}s ago)."
    return None
