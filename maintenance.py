"""Maintenance mode — silence the bot from Discord.

/maintenance off silences ALL bot output: the scoreboard loop's
edits, the watch tick's panels and alerts, the Barcelona reminder
and auto-watch, and every slash command except /maintenance and
/shutdown (those answer with a short ephemeral notice instead).
/maintenance on resumes normal operation.

The flag lives in sqlite (bot_settings), so it survives restarts.
Owner-only: the guild owner, or MAINTENANCE_OWNER_ID when set
(an optional env var for when the requester is not the guild
owner — no user id is hardcoded here).
"""
import logging
import os

import db

log = logging.getLogger("maintenance")

MAINTENANCE_OWNER_ID = int(os.getenv("MAINTENANCE_OWNER_ID", "0") or 0)
NOTICE = ("🔧 The bot is in maintenance right now — "
          "please try again later.")
OWNER_NOTICE = "Only the server owner can do that."


async def is_on() -> bool:
    """True when maintenance mode is set. A db error fails open
    (bot stays live) — a broken read must not silence the bot."""
    try:
        return (await db.get_setting("maintenance")) == "1"
    except Exception:
        return False


async def set_on(on: bool) -> None:
    await db.set_setting("maintenance", "1" if on else "0")
    log.info("[maintenance] %s",
             "ON — bot silent" if on else "OFF — bot back")


def is_owner(interaction) -> bool:
    """Guild owner, or MAINTENANCE_OWNER_ID when set. DMs have no
    guild owner, so only the env id can act there."""
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if not user_id:
        return False
    if MAINTENANCE_OWNER_ID and int(user_id) == MAINTENANCE_OWNER_ID:
        return True
    guild = getattr(interaction, "guild", None)
    owner_id = getattr(guild, "owner_id", None) if guild is not None else None
    return owner_id is not None and int(user_id) == int(owner_id)


async def view_blocked(interaction) -> bool:
    """For button/menu views: when maintenance is on, answer the
    press with the short notice and report True (the caller then
    does nothing). False when the bot is live."""
    if not await is_on():
        return False
    try:
        await interaction.response.send_message(NOTICE, ephemeral=True)
    except Exception:
        pass
    return True
