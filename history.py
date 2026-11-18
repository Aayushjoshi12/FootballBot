"""Match history, event dedup, stat snapshots + momentum (Features 1, 4, 6).

Thin logic over db.py: event keys are the restart-safe dedup
guard for alerts, matches/notification_log are the history the
user can browse, stat_snapshots feed the momentum estimate.

Momentum is an ESTIMATE from real stored snapshots only — the
difference in attacks, dangerous attacks and shots between the
last 10 minutes and the 10 minutes before that. When fewer than
two usable snapshots exist it returns None and the caller shows
current statistics instead. Nothing is ever fabricated.
"""
import logging
import time

import db

log = logging.getLogger("history")

# Momentum windows: compare the last 10 minutes of snapshots with
# the 10 minutes before them.
MOMENTUM_WINDOW_S = 600
SNAPSHOT_KEEP_S = 2 * 3600  # snapshots older than this are pruned

# Stats that move momentum, in priority order.
_MOMENTUM_STATS = ("Attacks", "Dangerous attacks", "Shots on target",
                   "Shots off target")


def _num(value) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _stat(stats: dict, name: str, side: int) -> float:
    entry = (stats or {}).get(name)
    if not isinstance(entry, (list, tuple)) or len(entry) <= side:
        return 0.0
    return _num(entry[side])


async def event_seen(event_key: str) -> bool:
    try:
        return await db.event_seen(event_key)
    except Exception as exc:
        log.warning("[history] event_seen failed (%s)", type(exc).__name__)
        return False


async def record_sent_event(event_key: str, game_id: str, channel_id: int,
                            kind: str, text: str) -> bool:
    """True when this event was newly recorded (send it); False
    when the key already exists (a duplicate — do not send)."""
    try:
        return await db.record_event(event_key, game_id, channel_id,
                                     kind, text)
    except Exception as exc:
        log.warning("[history] record_event failed (%s)", type(exc).__name__)
        return True


async def record_match_result(game_id: str, home: str, away: str,
                              league: str, sport: str,
                              final_home, final_away, status: str,
                              start_ts: int | None = None) -> None:
    try:
        await db.record_match(game_id, home, away, league, sport,
                              final_home, final_away, status, start_ts)
    except Exception as exc:
        log.warning("[history] record_match failed (%s)", type(exc).__name__)


async def record_notification(game_id: str, channel_id: int, kind: str,
                              text: str, delivered: bool = True) -> None:
    try:
        await db.record_notification(game_id, channel_id, kind, text,
                                     delivered)
    except Exception as exc:
        log.warning("[history] record_notification failed (%s)",
                    type(exc).__name__)


async def recent_matches(limit: int = 10) -> list[dict]:
    try:
        return await db.get_recent_matches(limit)
    except Exception as exc:
        log.warning("[history] recent_matches failed (%s)", type(exc).__name__)
        return []


async def recent_notifications(limit: int = 10) -> list[dict]:
    try:
        return await db.get_recent_notifications(limit)
    except Exception as exc:
        log.warning("[history] recent_notifications failed (%s)",
                    type(exc).__name__)
        return []


async def record_stat_snapshot(game_id: str, stats: dict) -> None:
    """Store one stat sample (stats as {name: (home, away)})."""
    if not stats:
        return
    try:
        await db.record_snapshot(game_id, int(time.time()), stats)
    except Exception as exc:
        log.warning("[history] record_snapshot failed (%s)",
                    type(exc).__name__)


async def momentum_summary(game_id: str) -> str | None:
    """Momentum estimate from real snapshots, or None when there
    is not enough history. Compares the latest sample with the
    oldest sample in the last 10-minute window."""
    now = int(time.time())
    try:
        snaps = await db.get_snapshots(game_id, now - MOMENTUM_WINDOW_S)
    except Exception as exc:
        log.warning("[history] momentum read failed (%s)", type(exc).__name__)
        return None
    if len(snaps) < 2:
        return None
    first, last = snaps[0]["stats"], snaps[-1]["stats"]
    home_delta = sum(_stat(last, n, 0) - _stat(first, n, 0)
                     for n in _MOMENTUM_STATS)
    away_delta = sum(_stat(last, n, 1) - _stat(first, n, 1)
                     for n in _MOMENTUM_STATS)
    if home_delta == 0 and away_delta == 0:
        return None
    poss = _stat(last, "Possession %", 0)
    side = "home" if home_delta > away_delta else "away"
    gap = abs(home_delta - away_delta)
    minutes = max(1, round((snaps[-1]["ts"] - snaps[0]["ts"]) / 60))
    line = (f"Momentum (estimate, last ~{minutes} min): "
            f"{'home' if side == 'home' else 'away'} side "
            f"+{gap:.0f} attacks/shots vs the other side")
    if poss:
        line += f" · possession now {poss:.0f}%–{100 - poss:.0f}%"
    return line


async def cleanup_old_history(keep_days: int = 30) -> dict:
    """Retention cleanup. Never touches watches, prefs or team
    follows — only finished history older than keep_days."""
    try:
        return await db.cleanup_history(keep_days)
    except Exception as exc:
        log.warning("[history] cleanup failed (%s)", type(exc).__name__)
        return {}
