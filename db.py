"""
SQLite persistence via aiosqlite.
Stores: guild settings, user favorites, game watches.
Survives bot restarts.
"""
import logging
from typing import Any

import aiosqlite

log = logging.getLogger("db")
_DB = "football_bot.db"
_conn: aiosqlite.Connection | None = None

# Whitelist of valid column names for guild_settings to prevent SQL injection
_ALLOWED_SETTINGS = frozenset({"timezone", "language"})


def _require_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("Database not initialised — call db.init() first.")
    return _conn


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(_DB)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            timezone TEXT    DEFAULT 'UTC',
            language TEXT    DEFAULT 'en'
        );
        -- Bot-wide settings (key/value): maintenance mode lives
        -- here so it survives restarts.
        CREATE TABLE IF NOT EXISTS bot_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            team_name TEXT  NOT NULL,
            PRIMARY KEY (user_id, team_id)
        );
        CREATE TABLE IF NOT EXISTS watches (
            channel_id INTEGER NOT NULL,
            game_id    TEXT    NOT NULL,
            home       TEXT    NOT NULL,
            away       TEXT    NOT NULL,
            league     TEXT    NOT NULL DEFAULT '',
            detail     TEXT    NOT NULL DEFAULT 'simple',
            fmt        TEXT    NOT NULL DEFAULT '',
            snapshot   TEXT    NOT NULL DEFAULT '{}',
            panel_message_id INTEGER NOT NULL DEFAULT 0,
            -- notify: 1 = event alerts on (/watch); 0 = view-only
            -- live panel (/game) — Follow flips this, it never
            -- removes the panel. panel_kind: 'compact' (the /watch
            -- panel), 'detail' (the /game detail card) or
            -- 'analytics' (the stats-table view).
            notify     INTEGER NOT NULL DEFAULT 1,
            panel_kind TEXT    NOT NULL DEFAULT 'compact',
            PRIMARY KEY (channel_id, game_id)
        );
        -- One live board per channel (the single message that
        -- carries every watched game's ticking row, edited once
        -- a second). The board message id survives restarts, so
        -- the tick resumes editing the SAME message instead of
        -- posting a new one.
        CREATE TABLE IF NOT EXISTS live_boards (
            channel_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL DEFAULT 0
        );
        -- Sent-event dedup keys (Feature 1): one row per alert
        -- actually sent, so a restart never re-sends an old event.
        CREATE TABLE IF NOT EXISTS match_events (
            event_key  TEXT PRIMARY KEY,
            game_id    TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            text       TEXT NOT NULL,
            created_ts INTEGER NOT NULL
        );
        -- Finished matches (Feature 6): final scores + status.
        CREATE TABLE IF NOT EXISTS matches (
            game_id    TEXT PRIMARY KEY,
            home       TEXT NOT NULL,
            away       TEXT NOT NULL,
            league     TEXT NOT NULL DEFAULT '',
            sport      TEXT NOT NULL DEFAULT '',
            start_ts   INTEGER,
            final_home TEXT,
            final_away TEXT,
            status     TEXT NOT NULL DEFAULT '',
            ended_ts   INTEGER NOT NULL
        );
        -- Delivery records (Feature 6): every notification sent.
        CREATE TABLE IF NOT EXISTS notification_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id    TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            text       TEXT NOT NULL,
            sent_ts    INTEGER NOT NULL,
            delivered  INTEGER NOT NULL DEFAULT 1
        );
        -- Per-watch notification preferences (Feature 5).
        -- events is a JSON list of kinds; [] means "use defaults"
        -- (all kinds). paused stops sending (snapshots still update).
        CREATE TABLE IF NOT EXISTS notify_prefs (
            channel_id INTEGER NOT NULL,
            game_id    TEXT NOT NULL,
            events     TEXT NOT NULL DEFAULT '[]',
            paused     INTEGER NOT NULL DEFAULT 0,
            detail     TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (channel_id, game_id)
        );
        -- Team follows (Feature 5): a user asks to follow a team's
        -- games; a live game involving the team starts a watch.
        CREATE TABLE IF NOT EXISTS team_follows (
            user_id    INTEGER NOT NULL,
            team_norm  TEXT NOT NULL,
            team_name  TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            detail     TEXT NOT NULL DEFAULT 'simple',
            PRIMARY KEY (user_id, team_norm, channel_id)
        );
        -- Stat snapshots for momentum (Feature 4): one row per
        -- game per sample time; pruned to a short window.
        CREATE TABLE IF NOT EXISTS stat_snapshots (
            game_id TEXT NOT NULL,
            ts      INTEGER NOT NULL,
            stats   TEXT NOT NULL,
            PRIMARY KEY (game_id, ts)
        );
        -- Health/recovery log (Feature 7): worker restarts, stale
        -- data detections, backoff recoveries.
        CREATE TABLE IF NOT EXISTS health_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        INTEGER NOT NULL,
            component TEXT NOT NULL,
            kind      TEXT NOT NULL,
            detail    TEXT NOT NULL DEFAULT ''
        );
    """)
    await _conn.commit()
    # Migration (18 Nov 2026): the live watch panel's message id, so
    # a restart resumes editing the same panel instead of posting
    # a new one. Old databases gain the column in place; no data
    # is lost and no row is touched.
    cur = await _conn.execute("PRAGMA table_info(watches)")
    _cols = {row[1] for row in await cur.fetchall()}
    if "panel_message_id" not in _cols:
        await _conn.execute(
            "ALTER TABLE watches ADD COLUMN "
            "panel_message_id INTEGER NOT NULL DEFAULT 0")
        await _conn.commit()
    # Migration (18 Nov 2026): /game live panels — notify flips
    # alerts on/off without removing the panel, panel_kind says
    # which render the tick uses. Old rows gain both in place.
    if "notify" not in _cols:
        await _conn.execute(
            "ALTER TABLE watches ADD COLUMN "
            "notify INTEGER NOT NULL DEFAULT 1")
        await _conn.commit()
    if "panel_kind" not in _cols:
        await _conn.execute(
            "ALTER TABLE watches ADD COLUMN "
            "panel_kind TEXT NOT NULL DEFAULT 'compact'")
        await _conn.commit()
    log.info("[db] Initialized.")


async def close() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


# ── Bot settings (key/value) ───────────────────────────────────────────────

async def get_setting(key: str) -> str | None:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    conn = _require_conn()
    await conn.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value))
    await conn.commit()


# ── Guild settings ─────────────────────────────────────────────────────────

async def set_guild_setting(guild_id: int, key: str, value: str) -> None:
    if key not in _ALLOWED_SETTINGS:
        raise ValueError(f"Invalid setting key: {key!r}. Allowed: {_ALLOWED_SETTINGS}")
    conn = _require_conn()
    # key is whitelisted above so interpolation is safe here
    await conn.execute(
        f"INSERT INTO guild_settings(guild_id,{key}) VALUES(?,?) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {key}=excluded.{key}",
        (guild_id, value),
    )
    await conn.commit()


async def get_guild_setting(guild_id: int, key: str, default: str = "") -> str:
    if key not in _ALLOWED_SETTINGS:
        raise ValueError(f"Invalid setting key: {key!r}. Allowed: {_ALLOWED_SETTINGS}")
    conn = _require_conn()
    cur = await conn.execute(
        f"SELECT {key} FROM guild_settings WHERE guild_id=?", (guild_id,)
    )
    row = await cur.fetchone()
    return row[key] if row else default


# ── Favorites ──────────────────────────────────────────────────────────────

async def add_favorite(user_id: int, team_id: int, team_name: str) -> None:
    conn = _require_conn()
    await conn.execute(
        "INSERT OR REPLACE INTO favorites VALUES (?,?,?)",
        (user_id, team_id, team_name),
    )
    await conn.commit()


async def remove_favorite(user_id: int, team_id: int) -> None:
    conn = _require_conn()
    await conn.execute(
        "DELETE FROM favorites WHERE user_id=? AND team_id=?", (user_id, team_id)
    )
    await conn.commit()


async def get_favorites(user_id: int) -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT team_id, team_name FROM favorites WHERE user_id=?", (user_id,)
    )
    rows = await cur.fetchall()
    return [{"team_id": r["team_id"], "team_name": r["team_name"]} for r in rows]


# ── Game watches ─────────────────────────────────────────────────────────
# One row per (channel, 1xBet game). snapshot is the last alerted
# score/stats state as JSON, so a restart resumes diffing instead
# of re-alerting everything.

async def add_watch(channel_id: int, game_id: str, home: str, away: str,
                    league: str, detail: str, fmt: str,
                    snapshot: dict, notify: bool = True,
                    panel_kind: str = "compact") -> None:
    import json as _json
    conn = _require_conn()
    # ON CONFLICT, not REPLACE: re-adding a game (e.g. Follow on
    # a /game panel) must never wipe the stored panel message id.
    await conn.execute(
        "INSERT INTO watches "
        "(channel_id, game_id, home, away, league, detail, fmt, "
        "snapshot, notify, panel_kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(channel_id, game_id) DO UPDATE SET "
        "home=excluded.home, away=excluded.away, "
        "league=excluded.league, detail=excluded.detail, "
        "fmt=excluded.fmt, snapshot=excluded.snapshot, "
        "notify=excluded.notify, panel_kind=excluded.panel_kind",
        (channel_id, str(game_id), home, away, league, detail, fmt,
         _json.dumps(snapshot), 1 if notify else 0, panel_kind),
    )
    await conn.commit()


async def set_watch_notify(channel_id: int, game_id: str,
                           notify: bool) -> None:
    """Flip event alerts for a watch without touching its panel —
    Follow / Unfollow only change this flag."""
    conn = _require_conn()
    await conn.execute(
        "UPDATE watches SET notify=? WHERE channel_id=? AND game_id=?",
        (1 if notify else 0, channel_id, str(game_id)),
    )
    await conn.commit()


async def remove_watch(channel_id: int, game_id: str) -> None:
    conn = _require_conn()
    await conn.execute(
        "DELETE FROM watches WHERE channel_id=? AND game_id=?",
        (channel_id, str(game_id)),
    )
    await conn.commit()


async def list_watches(channel_id: int) -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT channel_id, game_id, home, away, league, detail, fmt, "
        "snapshot, panel_message_id, notify, panel_kind "
        "FROM watches WHERE channel_id=?",
        (channel_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_all_watches() -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT channel_id, game_id, home, away, league, detail, fmt, "
        "snapshot, panel_message_id, notify, panel_kind "
        "FROM watches",
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_watch_panel(channel_id: int, game_id: str,
                          message_id: int | None) -> None:
    """Remember (or clear) the live panel message for a watch, so a
    restart resumes editing it instead of posting a new panel."""
    conn = _require_conn()
    await conn.execute(
        "UPDATE watches SET panel_message_id=? "
        "WHERE channel_id=? AND game_id=?",
        (int(message_id) if message_id else 0, channel_id, str(game_id)),
    )
    await conn.commit()


async def update_watch_snapshot(channel_id: int, game_id: str,
                                snapshot: dict) -> None:
    import json as _json
    conn = _require_conn()
    await conn.execute(
        "UPDATE watches SET snapshot=? WHERE channel_id=? AND game_id=?",
        (_json.dumps(snapshot), channel_id, str(game_id)),
    )
    await conn.commit()


# ── Live boards ───────────────────────────────────────────────────────
async def get_live_boards() -> list[dict]:
    """Every stored live-board message id, for resume on boot."""
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT channel_id, message_id FROM live_boards",
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_live_board(channel_id: int, message_id: int | None) -> None:
    """Remember (or clear) the live board message for a channel,
    so the 1s tick edits this message instead of posting a new one."""
    conn = _require_conn()
    if message_id:
        await conn.execute(
            "INSERT INTO live_boards (channel_id, message_id) "
            "VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id",
            (channel_id, int(message_id)),
        )
    else:
        await conn.execute(
            "DELETE FROM live_boards WHERE channel_id=?", (channel_id,),
        )
    await conn.commit()

# ══════════════════════════════════════════════════════════════════════════
# Match history, notification prefs, team follows, snapshots, health
# (Features 1, 4, 5, 6, 7). All helpers are no-ops-safe: callers
# check return values instead of assuming a write happened.
# ══════════════════════════════════════════════════════════════════════════

import json as _json
import time as _time


def _now() -> int:
    return int(_time.time())


# ── Sent-event dedup (Feature 1) ─────────────────────────────────────────

async def event_seen(event_key: str) -> bool:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT 1 FROM match_events WHERE event_key=?", (event_key,))
    return await cur.fetchone() is not None


async def record_event(event_key: str, game_id: str, channel_id: int,
                       kind: str, text: str) -> bool:
    """Insert the sent-event key. False when it already exists
    (a duplicate send) — the caller must not send it again."""
    conn = _require_conn()
    cur = await conn.execute(
        "INSERT OR IGNORE INTO match_events "
        "(event_key, game_id, channel_id, kind, text, created_ts) "
        "VALUES (?,?,?,?,?,?)",
        (event_key, str(game_id), channel_id, kind, text, _now()),
    )
    await conn.commit()
    return cur.rowcount == 1


# ── Finished matches (Feature 6) ─────────────────────────────────────────

async def record_match(game_id: str, home: str, away: str, league: str,
                       sport: str, final_home: str | None,
                       final_away: str | None, status: str,
                       start_ts: int | None = None) -> None:
    conn = _require_conn()
    await conn.execute(
        "INSERT OR REPLACE INTO matches "
        "(game_id, home, away, league, sport, start_ts, final_home, "
        " final_away, status, ended_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(game_id), home, away, league, sport, start_ts,
         None if final_home is None else str(final_home),
         None if final_away is None else str(final_away),
         status, _now()),
    )
    await conn.commit()


async def get_recent_matches(limit: int = 10) -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT game_id, home, away, league, sport, start_ts, "
        "final_home, final_away, status, ended_ts FROM matches "
        "ORDER BY ended_ts DESC LIMIT ?", (limit,))
    return [dict(r) for r in await cur.fetchall()]


# ── Notification delivery log (Feature 6) ────────────────────────────────

async def record_notification(game_id: str, channel_id: int, kind: str,
                              text: str, delivered: bool = True) -> None:
    conn = _require_conn()
    await conn.execute(
        "INSERT INTO notification_log "
        "(game_id, channel_id, kind, text, sent_ts, delivered) "
        "VALUES (?,?,?,?,?,?)",
        (str(game_id), channel_id, kind, text, _now(), 1 if delivered else 0),
    )
    await conn.commit()


async def get_recent_notifications(limit: int = 10) -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT game_id, channel_id, kind, text, sent_ts, delivered "
        "FROM notification_log ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in await cur.fetchall()]


# ── Notification preferences (Feature 5) ─────────────────────────────────

async def get_prefs(channel_id: int, game_id: str) -> dict | None:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT channel_id, game_id, events, paused, detail "
        "FROM notify_prefs WHERE channel_id=? AND game_id=?",
        (channel_id, str(game_id)))
    row = await cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["events"] = _json.loads(d.get("events") or "[]")
    except (_json.JSONDecodeError, TypeError):
        d["events"] = []
    return d


async def set_prefs(channel_id: int, game_id: str,
                    events: list[str] | None = None,
                    paused: bool | None = None,
                    detail: str | None = None) -> dict:
    """Upsert prefs, changing only the fields given. Returns the
    stored row (events as a list)."""
    current = await get_prefs(channel_id, game_id)
    new_events = (list(events) if events is not None
                  else (current["events"] if current else []))
    new_paused = (1 if paused else 0) if paused is not None \
        else (current["paused"] if current else 0)
    new_detail = (detail if detail is not None
                  else (current["detail"] if current else ""))
    conn = _require_conn()
    await conn.execute(
        "INSERT INTO notify_prefs (channel_id, game_id, events, paused, detail) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(channel_id, game_id) DO UPDATE SET "
        "events=excluded.events, paused=excluded.paused, detail=excluded.detail",
        (channel_id, str(game_id), _json.dumps(new_events), new_paused,
         new_detail),
    )
    await conn.commit()
    return {"channel_id": channel_id, "game_id": str(game_id),
            "events": new_events, "paused": new_paused, "detail": new_detail}


async def delete_prefs(channel_id: int, game_id: str) -> None:
    conn = _require_conn()
    await conn.execute(
        "DELETE FROM notify_prefs WHERE channel_id=? AND game_id=?",
        (channel_id, str(game_id)))
    await conn.commit()


async def list_prefs_for_channel(channel_id: int) -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT channel_id, game_id, events, paused, detail "
        "FROM notify_prefs WHERE channel_id=?", (channel_id,))
    out = []
    for r in await cur.fetchall():
        d = dict(r)
        try:
            d["events"] = _json.loads(d.get("events") or "[]")
        except (_json.JSONDecodeError, TypeError):
            d["events"] = []
        out.append(d)
    return out


# ── Team follows (Feature 5) ─────────────────────────────────────────────

async def add_team_follow(user_id: int, team_norm: str, team_name: str,
                          channel_id: int, detail: str = "simple") -> None:
    conn = _require_conn()
    await conn.execute(
        "INSERT OR REPLACE INTO team_follows "
        "(user_id, team_norm, team_name, channel_id, detail) "
        "VALUES (?,?,?,?,?)",
        (user_id, team_norm, team_name, channel_id, detail),
    )
    await conn.commit()


async def remove_team_follow(user_id: int, team_norm: str,
                             channel_id: int | None = None) -> int:
    conn = _require_conn()
    if channel_id is None:
        cur = await conn.execute(
            "DELETE FROM team_follows WHERE user_id=? AND team_norm=?",
            (user_id, team_norm))
    else:
        cur = await conn.execute(
            "DELETE FROM team_follows WHERE user_id=? AND team_norm=? "
            "AND channel_id=?", (user_id, team_norm, channel_id))
    await conn.commit()
    return cur.rowcount


async def list_team_follows(user_id: int) -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT user_id, team_norm, team_name, channel_id, detail "
        "FROM team_follows WHERE user_id=?", (user_id,))
    return [dict(r) for r in await cur.fetchall()]


async def get_all_team_follows() -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT user_id, team_norm, team_name, channel_id, detail "
        "FROM team_follows")
    return [dict(r) for r in await cur.fetchall()]


# ── Stat snapshots for momentum (Feature 4) ──────────────────────────────

async def record_snapshot(game_id: str, ts: int, stats: dict) -> None:
    conn = _require_conn()
    await conn.execute(
        "INSERT OR REPLACE INTO stat_snapshots (game_id, ts, stats) "
        "VALUES (?,?,?)",
        (str(game_id), int(ts), _json.dumps(stats)),
    )
    await conn.commit()


async def get_snapshots(game_id: str, since_ts: int) -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT ts, stats FROM stat_snapshots "
        "WHERE game_id=? AND ts>=? ORDER BY ts ASC",
        (str(game_id), int(since_ts)))
    out = []
    for r in await cur.fetchall():
        try:
            stats = _json.loads(r["stats"])
        except (_json.JSONDecodeError, TypeError):
            continue
        out.append({"ts": r["ts"], "stats": stats})
    return out


async def prune_snapshots(game_id: str, keep_since_ts: int) -> None:
    conn = _require_conn()
    await conn.execute(
        "DELETE FROM stat_snapshots WHERE game_id=? AND ts<?",
        (str(game_id), int(keep_since_ts)))
    await conn.commit()


# ── Health log (Feature 7) ───────────────────────────────────────────────

async def log_health(component: str, kind: str, detail: str = "") -> None:
    conn = _require_conn()
    await conn.execute(
        "INSERT INTO health_log (ts, component, kind, detail) "
        "VALUES (?,?,?,?)", (_now(), component, kind, detail),
    )
    await conn.commit()


async def get_recent_health(limit: int = 10) -> list[dict]:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT ts, component, kind, detail FROM health_log "
        "ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in await cur.fetchall()]


async def count_health(kind: str, since_ts: int) -> int:
    conn = _require_conn()
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM health_log WHERE kind=? AND ts>=?",
        (kind, int(since_ts)))
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


# ── Retention cleanup (Feature 6) ────────────────────────────────────────
# Never touches watches, notify_prefs or team_follows — only
# finished history older than keep_days.

async def cleanup_history(keep_days: int = 30) -> dict:
    cutoff = _now() - keep_days * 86400
    conn = _require_conn()
    removed = {}
    for table, col in (("match_events", "created_ts"),
                       ("notification_log", "sent_ts"),
                       ("matches", "ended_ts"),
                       ("stat_snapshots", "ts"),
                       ("health_log", "ts")):
        cur = await conn.execute(
            f"DELETE FROM {table} WHERE {col}<?", (cutoff,))
        removed[table] = cur.rowcount
    await conn.commit()
    return removed
