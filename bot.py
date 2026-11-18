"""Football Discord Bot using verified public football-data sources."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

import discord
from discord import app_commands
import google.generativeai as genai

import assistant
import db
import health
import history
import http_client as hc
import barcelona_reminder
import maintenance
import notify_manager
import scoreboard
from config import (
    DISCORD_BOT_TOKEN,
    GEMINI_API_KEY, GEMINI_MODEL, ESPN_LEAGUE_MAP,
)
from embeds import (
    match_embed, MatchView,
)
from providers import (
    free_fixtures_for_day,
    sportsdb_live_matches,
    sofa_live_matches, apf_live, espn_live, onex_live_matches,
    onex_all_live_matches,
    onex_search,
)
import watch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("bot")


ai_model = None
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = genai.GenerativeModel(GEMINI_MODEL)
    log.info("[ai] Gemini enabled with model %s", GEMINI_MODEL)
else:
    log.warning("[ai] Gemini disabled: GEMINI_API_KEY is not set")


def _crosslink_apf(matches: list[dict], apf_matches: list[dict]) -> None:
    """Stamp apf_id onto SofaScore/ESPN matches by matching normalised team names."""
    apf_index = {
        f"{m['home_norm']}|{m['away_norm']}": m["apf_id"]
        for m in apf_matches
        if m.get("apf_id")
    }
    for m in matches:
        if m.get("apf_id") is None:
            key = f"{m.get('home_norm', '')}|{m.get('away_norm', '')}"
            apf_id = apf_index.get(key)
            if apf_id:
                m["apf_id"] = apf_id


async def _tree_interaction_check(interaction: discord.Interaction) -> bool:
    """Global command gate for maintenance mode. /maintenance and
    /shutdown always work (they are how the owner ends it);
    autocomplete has no message response, so it is refused
    silently. Every other command is answered with the short
    maintenance notice and does not run. NOTE: discord.py's tree
    silently drops the interaction when this returns False
    (no on_error call), so the notice MUST be sent here."""
    data = getattr(interaction, "data", None) or {}
    name = data.get("name") if isinstance(data, dict) else None
    if name in ("maintenance", "shutdown"):
        return True
    if interaction.type is not discord.InteractionType.application_command:
        return not await maintenance.is_on()
    if await maintenance.is_on():
        try:
            await interaction.response.send_message(
                maintenance.NOTICE, ephemeral=True)
        except Exception:
            pass
        return False
    return True


def _schedule_shutdown(client, delay: float = 2.0) -> None:
    """Stop the bot process cleanly, after letting the reply
    flush. A clean exit (code 0) is NOT restarted by the systemd
    unit (Restart=on-failure, RestartSec=10 — recorded from the
    17 Nov 2026 deploy), so the bot stays off until the service
    is started again. os._exit(0) is the backstop: even if a
    cleanup step stumbles, the process still exits 0."""
    import os

    async def _shutdown():
        await asyncio.sleep(delay)
        try:
            await client.close()
        finally:
            os._exit(0)

    asyncio.create_task(_shutdown())


class ShutdownConfirmView(discord.ui.View):
    """One-shot confirm for /shutdown. Not persistent: if the bot
    restarts, the buttons die and the owner just runs /shutdown
    again."""

    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Yes, shut down", style=discord.ButtonStyle.danger,
                       custom_id="shutdown:confirm")
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        if not maintenance.is_owner(interaction):
            await interaction.response.send_message(
                maintenance.OWNER_NOTICE, ephemeral=True)
            return
        await interaction.response.send_message(
            "🛑 Shutting down…", ephemeral=True)
        _schedule_shutdown(interaction.client)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary,
                       custom_id="shutdown:cancel")
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        await interaction.response.send_message(
            "Shutdown cancelled.", ephemeral=True)


class FootballBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        # Health monitor uptime (Feature 7).
        self.start_time = time.time()

    async def setup_hook(self):
        await db.init()
        await watch.load_watches()
        # Maintenance gate for every slash command (see
        # _tree_interaction_check).
        self.tree.interaction_check = _tree_interaction_check
        # Persistent alert buttons: one static view covers every
        # 📊 Detail / 🔄 Live button — the game id is resolved from
        # the message each button sits on, so old buttons keep
        # working after a restart.
        self.add_view(watch.WatchAlertView())
        await self.tree.sync()
        # Global updates can take up to an hour to appear.  This bot is used
        # in a small set of servers, so also perform an immediate guild sync.
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        barcelona_reminder.ensure_running(self)
        scoreboard.ensure_running(self)
        log.info("[bot] Setup complete.")

    async def on_ready(self):
        barcelona_reminder.ensure_running(self)
        log.info("[bot] Ready as %s", self.user)
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("[bot] Synced current commands to %s", guild.name)
        await self.change_presence(activity=discord.Game("⚽ Football Live"))

    async def on_resumed(self):
        barcelona_reminder.ensure_running(self)

    async def close(self):
        reminder_task = getattr(self, "barcelona_task", None)
        if reminder_task is not None:
            reminder_task.cancel()
            await asyncio.gather(reminder_task, return_exceptions=True)
        sb_task = getattr(self, "scoreboard_task", None)
        if sb_task is not None:
            sb_task.cancel()
            await asyncio.gather(sb_task, return_exceptions=True)
        await hc.close()
        await db.close()
        await super().close()


bot = FootballBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """Keep command failures private and useful without leaking internals."""
    original = getattr(error, "original", error)
    log.exception("[command] %s failed", interaction.command, exc_info=original)
    message = "Football data is temporarily unavailable. Please try again shortly."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.NotFound, discord.HTTPException):
        # Discord may have already expired or acknowledged the interaction.
        log.warning("[command] could not send error response for %s", interaction.command)


# ══════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════

async def _send_paginated(interaction: discord.Interaction, embeds: list[discord.Embed]):
    """Send multiple embeds via followup — safe in DMs and guild channels."""
    if not embeds:
        await interaction.followup.send("No data found.")
        return
    for emb in embeds:
        await interaction.followup.send(embed=emb)


async def _defer(interaction: discord.Interaction) -> bool:
    """Acknowledge promptly; never make a second error from an expired command."""
    try:
        await interaction.response.defer(thinking=True)
        return True
    except (discord.InteractionResponded, discord.NotFound, discord.HTTPException):
        log.warning("[command] acknowledgement unavailable for %s", interaction.command)
        return False




# ══════════════════════════════════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="help", description="Show all commands")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚽ Football Bot",
        description=(
            "Follow a game and its live panel ticks every second; "
            "goals and big moments send you a notification."
        ),
        color=0x00C851,
    )
    embed.add_field(name="📡 Live", value=(
        "`/live` — all live matches right now\n"
        "`/game` — detail for one game; the reply ticks live every second while the game is on\n"
        "`/scoreboard` — live scoreboard (test channel only)\n"
        "`/stopscoreboard` — stop the scoreboard in this channel"
    ), inline=False)
    embed.add_field(name="👀 Follow a game", value=(
        "`/watch` — follow a game; its panel ticks live, alerts on goals, wickets and cards\n"
        "`/unwatch` — remove a game (its live panel and alerts stop)\n"
        "`/watching` — games this channel follows\n"
        "`/notifications` — manage alerts for watched games\n"
        "`/history` — past matches and alerts that were sent"
    ), inline=False)
    embed.add_field(name="💬 Ask", value=(
        "`/ask` — plain words: follow a game, list live games, Barcelona's next match\n"
        "`/nextbarca` — Barcelona's next match and reminder time"
    ), inline=False)
    embed.add_field(name="🛠 Bot", value=(
        "`/health` — is the bot OK: connection, live data, watches\n"
        "`/maintenance` — silence or resume the bot (server owner)\n"
        "`/shutdown` — stop the bot (server owner)\n"
        "`/help` — this list"
    ), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="health", description="Check bot, providers, and Barcelona reminders")
async def cmd_health(interaction: discord.Interaction):
    from http_client import stats_summary
    reminder = barcelona_reminder.health_snapshot(bot)
    embed = discord.Embed(title="🩺 Football Bot Health", color=0x00C851)
    embed.add_field(name="Discord", value=f"Connected • {round(bot.latency * 1000)} ms", inline=True)
    embed.add_field(name="Reminder worker", value=reminder["worker"], inline=True)
    embed.add_field(name="Reminder channel", value="configured" if reminder["channel_configured"] else "missing", inline=True)
    embed.add_field(name="Last reminder poll", value=reminder["last_poll"], inline=False)
    if reminder["last_result"]:
        result = reminder["last_result"]
        embed.add_field(name="Last reminder result", value=(
            f"fixtures: {result.get('fixtures', 0)} • due: {result.get('due', 0)} • "
            f"sent: {result.get('sent', 0)} • errors: {result.get('errors', 0)}"
        ), inline=False)
    embed.add_field(name="Provider status", value=f"```\n{stats_summary()}\n```", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    # Feature 7: the full health snapshot (workers, data
    # freshness, recent recovery attempts) as a followup.
    try:
        summary = await health.snapshot(bot)
        stale = await health.stale_check()
        if stale:
            summary += f"\n\n⚠️ {stale}"
        await interaction.followup.send(summary, ephemeral=True)
    except Exception:
        log.warning("[health] snapshot followup failed", exc_info=True)


@bot.tree.command(name="nextbarca", description="Show Barcelona's next match and reminder time")
async def cmd_nextbarca(interaction: discord.Interaction):
    if not await _defer(interaction):
        return
    now = datetime.now(barcelona_reminder.NEPAL)
    candidates = []
    for offset in range(9):
        fixtures = await free_fixtures_for_day(140, (now.date() + timedelta(days=offset)).isoformat())
        for match in fixtures:
            if not barcelona_reminder._is_barcelona(match):
                continue
            kickoff = barcelona_reminder._kickoff(match)
            if kickoff and kickoff > now:
                candidates.append((kickoff, match))
    if not candidates:
        await interaction.followup.send("No upcoming Barcelona fixture is available from the provider yet.")
        return
    kickoff, match = min(candidates, key=lambda item: item[0])
    local = kickoff.astimezone(barcelona_reminder.NEPAL)
    due = barcelona_reminder._send_at(kickoff)
    embed = discord.Embed(title="🔵🔴 Next Barcelona Match", color=0xA50044)
    embed.description = f"**{match.get('home', 'TBD')} vs {match.get('away', 'TBD')}**"
    embed.add_field(name="Kickoff", value=f"<t:{int(kickoff.timestamp())}:F>\n{local.strftime('%I:%M %p')} Nepal time", inline=False)
    embed.add_field(name="Reminder", value=f"<t:{int(due.timestamp())}:F>\n{due.strftime('%I:%M %p')} Nepal time", inline=False)
    await interaction.followup.send(embed=embed)


# ── /maintenance + /shutdown ───────────────────────────────────────────────────
@bot.tree.command(name="maintenance", description="Silence or resume the bot (server owner only)")
@app_commands.describe(mode="off = silence the bot, on = resume the bot")
@app_commands.choices(mode=[
    app_commands.Choice(name="off — silence the bot", value="off"),
    app_commands.Choice(name="on — resume the bot", value="on"),
])
async def cmd_maintenance(interaction: discord.Interaction, mode: str):
    if not maintenance.is_owner(interaction):
        await interaction.response.send_message(
            maintenance.OWNER_NOTICE, ephemeral=True)
        return
    on = (mode == "off")
    await maintenance.set_on(on)
    if on:
        await interaction.response.send_message(
            "🔧 Maintenance mode ON — the bot is silent now: no "
            "scoreboard, no watch alerts, no reminders. Use "
            "/maintenance on to resume.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "✅ Maintenance mode OFF — the bot is back to normal.",
            ephemeral=True)


@bot.tree.command(name="shutdown", description="Stop the bot (server owner only)")
async def cmd_shutdown(interaction: discord.Interaction):
    if not maintenance.is_owner(interaction):
        await interaction.response.send_message(
            maintenance.OWNER_NOTICE, ephemeral=True)
        return
    await interaction.response.send_message(
        "🛑 Shut the bot down? It stays off until the service is "
        "started again.", view=ShutdownConfirmView(), ephemeral=True)


# ── /live ────────────────────────────────────────────────────────────────────────
LIVE_RENDER_CAP = 15  # most embeds one /live reply sends


def _live_has_score(m: dict) -> bool:
    """A game worth showing first: it has a score (0–0 counts) or a minute."""
    return (m.get("home_score") is not None
            or m.get("away_score") is not None
            or isinstance(m.get("minute"), int))


# Statuses that prove a match is in progress / not started yet.
# Providers use different vocabularies, so the gate is deliberately
# default-deny: a match with no live signal at all is dropped rather
# than shown as "live" (18 Nov 2026: scheduled fixtures filled /live).
_LIVE_STATUSES = {"1h", "2h", "ht", "et", "live", "in progress",
                  "half time", "half-time", "1st half", "2nd half",
                  "extra time", "break time", "bt"}
_NOT_LIVE_STATUSES = {"", "scheduled", "ns", "not started", "tbd",
                      "postponed", "pst", "canc", "cancelled",
                      "abandoned", "abd", "ft", "aet",
                      "match finished", "finished"}


def _match_is_live(m: dict) -> bool:
    """Live-only gate for /live: True only for a genuinely
    in-progress match. A minute or a live status proves it; the
    1xBet and SofaScore live feeds are live-only by construction,
    so their matches pass even before a minute/score exists; a
    score with no not-started/finished flag also proves it. Anything
    else (scheduled, finished, or an unrecognised status with no
    minute and no score) is dropped."""
    if m.get("finished"):
        return False
    status = str(m.get("status") or "").strip().lower()
    if status in _NOT_LIVE_STATUSES:
        return False
    if isinstance(m.get("minute"), int):
        return True
    if status in _LIVE_STATUSES:
        return True
    if m.get("provider") in ("onexbet", "sofascore"):
        return True
    if m.get("home_score") is not None or m.get("away_score") is not None:
        return True
    return False


def _is_football_match(m: dict) -> bool:
    """Football-first sort for /live: the football-only providers
    (SportsDB / SofaScore / API-Football / ESPN) are football by
    construction; a 1xBet match is football only when it says so
    (unknown sport sorts after football, never guessed)."""
    if m.get("provider") != "onexbet":
        return True
    return str(m.get("sport") or "").strip().lower() == "football"


def merge_live_matches(sportsdb: list[dict], sofa: list[dict],
                       apf: list[dict], onex: list[dict],
                       espn: list[dict]) -> list[dict]:
    """Merge + dedup the /live providers (SportsDB/SofaScore/
    API-Football first, then ESPN, then 1xBet), preferring games
    with a score/minute — a scoreless game never pushes a live
    score off the cap. LIVE ONLY: every list is passed through
    _match_is_live first, so a scheduled or finished fixture from
    any provider can never appear in /live. FOOTBALL FIRST:
    football matches sort before other sports (18 Nov 2026: /live
    showed 15 games, all cricket, zero football — 1xBet's feed
    is cricket-heavy and filled the cap). Pure: no network, no
    Discord. The caller slices [:LIVE_RENDER_CAP]."""
    sportsdb = [m for m in sportsdb if _match_is_live(m)]
    sofa = [m for m in sofa if _match_is_live(m)]
    apf = [m for m in apf if _match_is_live(m)]
    onex = [m for m in onex if _match_is_live(m)]
    espn = [m for m in espn if _match_is_live(m)]
    seen_pairs = ({(m["home"], m["away"]) for m in sportsdb}
                  | {(m["home"], m["away"]) for m in sofa}
                  | {(m["home"], m["away"]) for m in apf})
    combined = list(sportsdb) + list(sofa) + list(apf)
    for m in espn:
        pair = (m["home"], m["away"])
        if pair not in seen_pairs:
            combined.append(m)
            seen_pairs.add(pair)
    for m in onex:
        pair = (m["home"], m["away"])
        if pair not in seen_pairs:
            combined.append(m)
            seen_pairs.add(pair)
    # Stable sort: football first, then scored games first;
    # merge order kept inside each group.
    combined.sort(key=lambda m: (0 if _is_football_match(m) else 1,
                                 0 if _live_has_score(m) else 1))
    return combined


@bot.tree.command(name="live", description="All live matches right now")
async def cmd_live(interaction: discord.Interaction):
    await interaction.response.defer()
    espn_tasks = [espn_live(slug, lid) for lid, slug in ESPN_LEAGUE_MAP.items()]
    results = await asyncio.gather(
        sportsdb_live_matches(), sofa_live_matches(), apf_live(), onex_all_live_matches(), *espn_tasks,
        return_exceptions=True,
    )
    sportsdb, sofa, apf, onex, *espn_results = results
    sportsdb = sportsdb if isinstance(sportsdb, list) else []
    sofa = sofa if isinstance(sofa, list) else []
    apf  = apf  if isinstance(apf,  list) else []
    onex = onex if isinstance(onex, list) else []
    espn: list = []
    for r in espn_results:
        if isinstance(r, tuple):
            espn.extend(r[0])

    if not onex:
        # Full live list empty — fall back to the featured games
        # rather than showing nothing.
        try:
            featured = await onex_live_matches()
        except Exception:
            featured = []
        onex = featured if isinstance(featured, list) else []

    combined = merge_live_matches(sportsdb, sofa, apf, onex, espn)

    # Cross-link APF fixture IDs onto SofaScore/ESPN matches so Stats/Events buttons work
    _crosslink_apf(combined, apf)

    if not combined:
        await interaction.followup.send("No live matches right now.")
        return
    shown = combined[:LIVE_RENDER_CAP]
    for m in shown:
        await interaction.followup.send(embed=match_embed(m), view=MatchView(m))
    if len(combined) > len(shown):
        await interaction.followup.send(f"…and {len(combined)-len(shown)} more live matches.")


# ── /scoreboard ───────────────────────────────────────────────────────────────
@bot.tree.command(name="scoreboard", description="Live scoreboard in the test channel (NPL + top games)")
async def cmd_scoreboard(interaction: discord.Interaction):
    # Test channel only (incident 18 Nov 2026): never start a
    # scoreboard in #general or any other channel.
    if not scoreboard.is_scoreboard_channel(interaction.channel_id):
        await interaction.response.send_message(
            "The live scoreboard only runs in the test channel — "
            "I can't start it here.",
            ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    already = scoreboard.is_running(interaction.channel_id)
    try:
        posted = await scoreboard.start_for_channel(bot, interaction.channel_id)
    except Exception:
        log.exception("[scoreboard] start failed")
        await interaction.followup.send(
            "Could not start the scoreboard here. Please try again shortly.",
            ephemeral=True)
        return
    if posted:
        if already:
            await interaction.followup.send(
                "📊 The live scoreboard is already running in this "
                "channel — it updates when a score changes.",
                ephemeral=True)
        else:
            await interaction.followup.send(
                "📊 Live scoreboard started in this channel — it updates when "
                "a score changes.", ephemeral=True)
    else:
        # No blame assigned: a failed first post is usually the
        # live feed not answering, not a permissions problem.
        await interaction.followup.send(
            "Could not start the scoreboard here — the live feed "
            "didn't answer. Please try again shortly.", ephemeral=True)


# ── /stopscoreboard ─────────────────────────────────────────────────────────
@bot.tree.command(name="stopscoreboard", description="Stop the live scoreboard in this channel")
async def cmd_stopscoreboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    stopped = await scoreboard.stop_for_channel(bot, interaction.channel_id)
    if stopped:
        await interaction.followup.send(
            "📊 Live scoreboard stopped in this channel — it won't update "
            "here any more.",
            ephemeral=True)
    else:
        await interaction.followup.send(
            "No live scoreboard is running in this channel.",
            ephemeral=True)


# ── /game ─────────────────────────────────────────────────────────────────────
async def _send_game_detail(interaction: discord.Interaction,
                            detail: dict, level: str) -> None:
    """Send a /game detail reply and, when the game is live, make
    the reply itself the live panel: the shared 1s tick edits it
    in place (score + MM:SS clock) until full-time, with no new
    messages. When the game already has a panel in this channel
    the reply stays a static detail card."""
    body = watch.render_detail(detail, level,
                               updated=watch.now_npt_str())
    msg = await interaction.followup.send(
        body, view=watch.WatchAlertView(), wait=True)
    status = str(detail.get("status") or "")
    if status not in ("1H", "2H", "ET", "PEN", "HT", "Live",
                      "1st innings", "2nd innings"):
        return
    game_id = str(detail.get("onex_id") or detail.get("id") or "")
    if not game_id or msg is None:
        return
    await watch.register_game_panel(
        interaction.channel_id, game_id, msg.id, body, detail, level)


@bot.tree.command(name="game", description="Detail for one game — by ID or team name")
@app_commands.describe(query="Team, league or game ID", detail="Simple or advanced detail")
@app_commands.choices(detail=[
    app_commands.Choice(name="Simple", value="simple"),
    app_commands.Choice(name="Advanced", value="advanced"),
])
async def cmd_game(interaction: discord.Interaction, query: str, detail: str = "simple"):
    if not await _defer(interaction):
        return
    # A bare sport word ("football", "show any cricket game")
    # is a browse request, not a failed search: list that
    # sport's live games as a pick list.
    sport = watch._sport_from_words(query)
    if sport and watch._is_sport_only_query(query, sport):
        games = await watch.top_live_games(sport=sport)
        if games:
            await interaction.followup.send(
                watch.render_top_games(
                    games, header=watch.sport_list_header(sport)))
        else:
            await interaction.followup.send(
                watch.no_live_sport_message(sport))
        return
    try:
        result = await onex_search(query)
    except Exception:
        log.exception("[game] search failed")
        await interaction.followup.send(
            "Could not reach the live scores right now. Please try again shortly.")
        return
    if result.get("detail"):
        await _send_game_detail(interaction, result["detail"], detail)
        return
    games = result.get("games") or []
    if len(games) == 1:
        try:
            again = await onex_search(str(games[0]["onex_id"]))
        except Exception:
            again = {"detail": None}
        if again.get("detail"):
            await _send_game_detail(interaction, again["detail"], detail)
            return
        await _send_game_detail(interaction, games[0], detail)
        return
    if games:
        await interaction.followup.send(watch.render_match_list(games))
        return
    # Suggestions follow the sport the query names, if any — a
    # football query never suggests cricket. When that sport has
    # nothing live, say so instead of listing other sports.
    sport = watch._sport_from_words(query)
    suggestions = await watch.top_live_games(sport=sport)
    if suggestions:
        noun = f"live {sport.lower()} games" if sport else "live games"
        await interaction.followup.send(
            f"No game found for '{query}' — here are the {noun} right now:\n"
            + watch.render_top_games(suggestions, header=""))
        return
    if sport:
        await interaction.followup.send(
            watch.no_live_sport_message(sport))
        return
    await interaction.followup.send(
        f"No game found for '{query}'. Try a team name, a league, "
        "or the game ID from the match page.")


# ── /watch ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="watch", description="Follow one game — alerts here when it changes")
@app_commands.describe(
    what="Game ID, team/league name, or plain words like 'follow Lumbini, send advanced alerts to #general'",
    detail="Simple or advanced alerts (leave blank and the words decide)",
    channel="Channel for the alerts (default: this channel)",
)
@app_commands.choices(detail=[
    app_commands.Choice(name="Simple", value="simple"),
    app_commands.Choice(name="Advanced", value="advanced"),
])
async def cmd_watch(interaction: discord.Interaction, what: str,
                    detail: str | None = None,
                    channel: discord.TextChannel | None = None):
    await interaction.response.defer(ephemeral=True)
    guild = getattr(interaction, "guild", None)
    guild_channels: list[dict] = []
    if guild is not None:
        for ch in getattr(guild, "text_channels", []) or []:
            guild_channels.append({"id": ch.id,
                                   "name": getattr(ch, "name", "")})
    try:
        resolved = await watch.resolve_watch(what, ai_model, guild_channels)
    except Exception:
        log.exception("[watch] resolve failed")
        await interaction.followup.send(
            "Could not work out which game that is. Try a game ID or a team name.",
            ephemeral=True)
        return
    # Precedence: the dropdowns always win. Only when the user
    # leaves them blank do the words/AI decide; otherwise the
    # current channel and simple alerts.
    level = (detail if detail is not None
             else resolved.get("detail") or watch.DETAIL_SIMPLE)
    target = channel
    if target is None and resolved.get("channel_id") is not None:
        try:
            cid = int(resolved["channel_id"])
        except (TypeError, ValueError):
            cid = None
        if cid is not None and guild is not None:
            target = guild.get_channel(cid)
    if target is None:
        target = interaction.channel
    if target is None:
        await interaction.followup.send(
            "Could not tell which channel to watch in.", ephemeral=True)
        return
    channel_note = ""
    if resolved.get("channel_named") and not resolved.get("channel_id"):
        channel_note = (f" (I could not find a channel called "
                        f"'{resolved['channel_named']}' — using this "
                        f"channel instead)")
    if resolved.get("action") == "detail" and resolved.get("game_id"):
        try:
            result = await onex_search(str(resolved["game_id"]))
        except Exception:
            result = {"detail": None}
        if result.get("detail"):
            await interaction.followup.send(
                watch.render_detail(result["detail"], level),
                ephemeral=True, view=watch.WatchAlertView())
            return
    if resolved.get("action") == "list":
        games = resolved.get("games") or []
        sport = resolved.get("sport")
        if games:
            await interaction.followup.send(
                watch.render_top_games(
                    games, header=watch.sport_list_header(sport)),
                ephemeral=True)
        else:
            msg = (watch.no_live_sport_message(sport) if sport
                   else "No games are live right now — try again when a match is on.")
            await interaction.followup.send(msg, ephemeral=True)
        return
    if resolved.get("action") == "unfollow" and resolved.get("game_id"):
        # Unfollow stops the alerts; the live panel keeps ticking
        # in place while the game is live.
        entry = await watch.set_notify(
            target.id, str(resolved["game_id"]), False)
        if entry:
            await interaction.followup.send(
                f"🔕 Notifications off for **{entry['home']} vs "
                f"{entry['away']}** — the live panel keeps ticking "
                f"here while the game is live.", ephemeral=True)
        else:
            await interaction.followup.send(
                "That game is not watched in this channel.", ephemeral=True)
        return
    if not resolved.get("game_id"):
        candidates = resolved.get("candidates") or []
        if candidates:
            await interaction.followup.send(
                "I could not tell exactly which game — did you mean one of these?\n"
                + watch.render_match_list(candidates), ephemeral=True)
        else:
            suggestions = resolved.get("suggestions") or []
            sport = resolved.get("sport")
            if suggestions:
                noun = (f"live {sport.lower()} games" if sport
                        else "live games")
                await interaction.followup.send(
                    f"I could not find a game for '{what}' — here are the {noun} right now:\n"
                    + watch.render_top_games(suggestions, header=""),
                    ephemeral=True)
            elif sport:
                await interaction.followup.send(
                    watch.no_live_sport_message(sport), ephemeral=True)
            else:
                await interaction.followup.send(
                    f"Could not find a game for '{what}'. Try a game ID "
                    "or a team name.", ephemeral=True)
        return
    entry = resolved.get("entry") or {"id": resolved["game_id"], "home": "",
                                      "away": "", "league": "", "status": ""}
    try:
        started = await watch.start_watch(target.id, entry, level)
    except Exception:
        log.exception("[watch] start failed")
        await interaction.followup.send(
            "Could not start watching that game. Please try again shortly.",
            ephemeral=True)
        return
    if not started:
        await interaction.followup.send(
            "Could not find that game right now.", ephemeral=True)
        return
    # The game already has a live panel in this channel (a /game
    # reply): Follow switches its alerts on and the panel keeps
    # ticking — never a second panel message.
    if started.get("panel_exists"):
        await interaction.followup.send(
            f"🔔 Now following **{started['home']} vs {started['away']}** "
            f"({started['league']}) — the live panel in this channel "
            f"keeps ticking; {level} alerts only for goals, wickets "
            f"and cards.{channel_note}", ephemeral=True)
        return
    # The panel IS the reply when the game is live: one public
    # message in the target channel, ticking in place every
    # second — not a separate static confirmation. A game that
    # has not started yet gets the text reply instead, and its
    # panel appears on the first tick once it goes live.
    detail = started.get("live_detail")
    live_status = (str(detail.get("status") or "")
                   if isinstance(detail, dict) else "")
    if detail is not None and live_status in (
            "1H", "2H", "ET", "PEN", "HT", "Live",
            "1st innings", "2nd innings"):
        panel = watch.render_panel(detail, updated=watch.now_npt_str())
        view = watch.WatchAlertView(str(started["game_id"]))
        if target.id == interaction.channel_id:
            msg = await interaction.followup.send(
                panel, view=view, wait=True)
            if msg is not None:
                await watch.set_panel_message(
                    target.id, str(started["game_id"]), msg.id, panel)
            return
        await watch.post_panel_now(
            getattr(interaction, "client", None), target.id,
            str(started["game_id"]), detail)
        await interaction.followup.send(
            f"👀 Watching **{started['home']} vs {started['away']}** "
            f"({started['league']}) in <#{target.id}> — the live panel "
            f"is there now.{channel_note}", ephemeral=True)
        return
    await interaction.followup.send(
        f"👀 Watching **{started['home']} vs {started['away']}** "
        f"({started['league']}) in <#{target.id}> — a live panel there "
        f"updates every second; {level} alerts only for goals, wickets "
        f"and cards.{channel_note}", ephemeral=True)


# ── /unwatch ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="unwatch", description="Remove a game — its live panel and alerts stop")
@app_commands.describe(game="Game ID or team name")
async def cmd_unwatch(interaction: discord.Interaction, game: str):
    await interaction.response.defer(ephemeral=True)
    rows = await watch.list_channel_watches(interaction.channel_id,
                                            include_view_only=True)
    q = (game or "").strip()
    picked = None
    if q.isdigit():
        picked = next((r for r in rows if r["game_id"] == q), None)
    if picked is None:
        needle = watch._norm(q)
        matches = [r for r in rows
                   if needle and needle in watch._norm(f"{r['home']} {r['away']}")]
        if len(matches) == 1:
            picked = matches[0]
        elif len(matches) > 1:
            await interaction.followup.send(
                "More than one watched game matches — be specific:\n"
                + "\n".join(f"• **{r['home']} vs {r['away']}** (ID `{r['game_id']}`)"
                             for r in matches), ephemeral=True)
            return
    if picked is None:
        await interaction.followup.send(
            f"No watched game matches '{game}' in this channel. "
            "Use `/watching` to see what is watched here.", ephemeral=True)
        return
    # Full remove — not just alerts off. The panel stops
    # ticking, its message is deleted (best effort), and the
    # watch leaves the DB. This is the /unwatch command only:
    # the 🔕 Unfollow button stays notifications-only (the
    # panel keeps ticking), and /watch's unfollow action too.
    await watch.delete_panel_message(
        getattr(interaction, "client", None),
        interaction.channel_id, picked["game_id"])
    entry = await watch.remove_watch(
        interaction.channel_id, picked["game_id"])
    if entry:
        await interaction.followup.send(
            f"🗑️ Removed **{entry['home']} vs {entry['away']}** — "
            f"it is no longer refreshing in this channel.",
            ephemeral=True)
    else:
        await interaction.followup.send("That watch is already gone.", ephemeral=True)


# ── /watching ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="watching", description="Games this channel is following")
async def cmd_watching(interaction: discord.Interaction):
    rows = await watch.list_channel_watches(interaction.channel_id,
                                            include_view_only=True)
    if not rows:
        await interaction.response.send_message(
            "No games watched in this channel. Use `/watch <game>` to follow one.",
            ephemeral=True)
        return
    # How many are actually refreshing: the live panels share
    # Discord's ~1 edit/sec channel budget evenly — 1 game
    # refreshes every second, 2 every 2s each, and so on.
    live = [r for r in rows
            if watch.is_panel_live((r.get("snapshot") or {}).get("status"))]
    n = len(live)
    if n:
        each = "every second" if n == 1 else f"every {n}s"
        lines = [f"👀 **{n} game{'s' if n > 1 else ''} refreshing in this "
                 f"channel — each about {each}**\n"]
    else:
        lines = [f"👀 **No games refreshing yet** — {len(rows)} watched, "
                 f"starting later\n"]
    for r in rows:
        snap = r.get("snapshot") or {}
        score = ""
        hs, aws = snap.get("home_score"), snap.get("away_score")
        if hs not in (None, "") or aws not in (None, ""):
            score = f" — {hs or '?'}–{aws or '?'}"
        cricket = snap.get("cricket") or {}
        if cricket.get("is_break"):
            clock = " · Innings break"
        elif cricket.get("over_str"):
            clock = f" · {cricket['over_str']} ov"
        elif snap.get("clock"):
            clock = f" · {snap['clock']}"
        else:
            clock = ""
        alerts = "🔔 alerts on" if r.get("notify") else "🔕 alerts off"
        if watch.is_panel_live(snap.get("status")):
            each = "every second" if n == 1 else f"every {n}s"
            lines.append(f"• **{r['home']} vs {r['away']}**{score}{clock} · "
                         f"{each} · {alerts} "
                         f"({r['detail']}, ID `{r['game_id']}`)")
        else:
            lines.append(f"• **{r['home']} vs {r['away']}**{score}{clock} · "
                         f"not refreshing yet · {alerts} "
                         f"({r['detail']}, ID `{r['game_id']}`)")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ── /ask ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="ask", description="Ask about live matches in plain words")
@app_commands.describe(text="e.g. 'follow the Gambia game', 'what games are live?', 'when do Barcelona play next?'")
async def cmd_ask(interaction: discord.Interaction, text: str):
    guild = getattr(interaction, "guild", None)
    guild_channels: list[dict] = []
    if guild is not None:
        for ch in getattr(guild, "text_channels", []) or []:
            guild_channels.append({"id": ch.id,
                                   "name": getattr(ch, "name", "")})
    await assistant.handle_ask(interaction, text, ai_model, guild_channels)


# ── /notifications ──────────────────────────────────────────────────────────
@bot.tree.command(name="notifications", description="Manage alerts for the games watched in this channel")
async def cmd_notifications(interaction: discord.Interaction):
    await notify_manager.show_notifications(interaction)


# ── /history ────────────────────────────────────────────────────────────────
class HistoryView(discord.ui.View):
    """◀ ▶ pagination for /history. Short-lived (3 min) and
    non-persistent: the pages live only in this view, so after a
    restart the user just runs /history again."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Maintenance mode: a press answers with the short notice
        # and the pages do not turn.
        return not await maintenance.view_blocked(interaction)

    def __init__(self, pages: list[str]):
        super().__init__(timeout=180)
        self.pages = pages
        self.index = 0
        self._refresh()

    def _refresh(self):
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._refresh()
        await interaction.response.edit_message(
            content=self.pages[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._refresh()
        await interaction.response.edit_message(
            content=self.pages[self.index], view=self)


def _history_pages(matches: list[dict], notes: list[dict]) -> list[str]:
    """Match history + notification log as short pages."""
    pages: list[str] = []
    if not matches and not notes:
        return ["📜 No match history yet — finished games and sent "
                "alerts will appear here."]
    for i in range(0, len(matches), 5):
        chunk = matches[i:i + 5]
        lines = ["📜 **Match history**\n"]
        for m in chunk:
            score = ""
            fh, fa = m.get("final_home"), m.get("final_away")
            if fh not in (None, "") or fa not in (None, ""):
                score = f" {fh}–{fa}"
            lines.append(f"• **{m.get('home', '?')} vs {m.get('away', '?')}**"
                         f"{score} — {m.get('status', '')}")
        pages.append("\n".join(lines))
    for i in range(0, len(notes), 8):
        chunk = notes[i:i + 8]
        lines = ["🔔 **Recent alerts sent**\n"]
        for n in chunk:
            lines.append(f"• [{n.get('kind', '')}] "
                         f"{str(n.get('text', ''))[:120]}")
        pages.append("\n".join(lines))
    if len(pages) > 1:
        for i, page in enumerate(pages):
            pages[i] = page + f"\n\nPage {i + 1}/{len(pages)}"
    return pages


@bot.tree.command(name="history", description="Past matches and alerts that were sent")
async def cmd_history(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        matches = await history.recent_matches(limit=25)
    except Exception:
        matches = []
    try:
        notes = await history.recent_notifications(limit=24)
    except Exception:
        notes = []
    pages = _history_pages(matches, notes)
    await interaction.followup.send(pages[0], ephemeral=True,
                                    view=HistoryView(pages)
                                    if len(pages) > 1 else None)


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
