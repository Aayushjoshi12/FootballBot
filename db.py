"""
SQLite persistence via aiosqlite.
Stores: subscriptions, guild settings, user favorites.
Survives bot restarts.
"""
import asyncio
import logging
from typing import Any

import aiosqlite

log = logging.getLogger("db")
_DB = "football_bot.db"
_conn: aiosqlite.Connection | None = None


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(_DB)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            guild_id   INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            league_id  INTEGER NOT NULL,
            PRIMARY KEY (channel_id, league_id)
        );
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            timezone TEXT    DEFAULT 'UTC',
            language TEXT    DEFAULT 'en'
        );
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            team_name TEXT  NOT NULL,
            PRIMARY KEY (user_id, team_id)
        );
    """)
    await _conn.commit()
    log.info("[db] Initialized.")


async def close() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


# ── Subscriptions ──────────────────────────────────────────────────────────

async def add_subscription(guild_id: int, channel_id: int, league_id: int) -> None:
    await _conn.execute(
        "INSERT OR REPLACE INTO subscriptions VALUES (?,?,?)",
        (guild_id, channel_id, league_id),
    )
    await _conn.commit()


async def remove_subscriptions(channel_id: int) -> None:
    await _conn.execute("DELETE FROM subscriptions WHERE channel_id=?", (channel_id,))
    await _conn.commit()


async def get_all_subscriptions() -> dict[int, set[int]]:
    """Returns {channel_id: set(league_ids)}"""
    cur = await _conn.execute("SELECT channel_id, league_id FROM subscriptions")
    rows = await cur.fetchall()
    result: dict[int, set[int]] = {}
    for row in rows:
        result.setdefault(row["channel_id"], set()).add(row["league_id"])
    return result


async def get_channel_leagues(channel_id: int) -> set[int]:
    cur = await _conn.execute(
        "SELECT league_id FROM subscriptions WHERE channel_id=?", (channel_id,)
    )
    rows = await cur.fetchall()
    return {row["league_id"] for row in rows}


# ── Guild settings ─────────────────────────────────────────────────────────

async def set_guild_setting(guild_id: int, key: str, value: str) -> None:
    await _conn.execute(
        f"INSERT INTO guild_settings(guild_id,{key}) VALUES(?,?) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {key}=excluded.{key}",
        (guild_id, value),
    )
    await _conn.commit()


async def get_guild_setting(guild_id: int, key: str, default: str = "") -> str:
    cur = await _conn.execute(
        f"SELECT {key} FROM guild_settings WHERE guild_id=?", (guild_id,)
    )
    row = await cur.fetchone()
    return row[key] if row else default


# ── Favorites ──────────────────────────────────────────────────────────────

async def add_favorite(user_id: int, team_id: int, team_name: str) -> None:
    await _conn.execute(
        "INSERT OR REPLACE INTO favorites VALUES (?,?,?)",
        (user_id, team_id, team_name),
    )
    await _conn.commit()


async def remove_favorite(user_id: int, team_id: int) -> None:
    await _conn.execute(
        "DELETE FROM favorites WHERE user_id=? AND team_id=?", (user_id, team_id)
    )
    await _conn.commit()


async def get_favorites(user_id: int) -> list[dict]:
    cur = await _conn.execute(
        "SELECT team_id, team_name FROM favorites WHERE user_id=?", (user_id,)
    )
    rows = await cur.fetchall()
    return [{"team_id": r["team_id"], "team_name": r["team_name"]} for r in rows]
