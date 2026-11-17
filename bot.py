"""Football Discord Bot using verified public football-data sources."""
import asyncio
import logging
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
import google.generativeai as genai

import db
import http_client as hc
import barcelona_reminder
import scoreboard
from config import (
    DISCORD_BOT_TOKEN, TRACKED_LEAGUES,
    GEMINI_API_KEY, GEMINI_MODEL, SPORTSDB_LEAGUE_MAP, ESPN_LEAGUE_MAP,
)
from embeds import (
    match_embed, fixtures_embed, MatchView,
)
from providers import (
    apf_upcoming, free_fixtures_for_day,
    sportsdb_day, sportsdb_live_matches,
    sofa_live_matches, apf_live, espn_live, onex_live_matches,
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


def _league_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=name, value=str(lid))
        for lid, name in TRACKED_LEAGUES.items()
        if lid in SPORTSDB_LEAGUE_MAP
    ]


class FootballBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await db.init()
        await watch.load_watches()
        # Persistent alert buttons: one view per watched game, so the
        # 📊 Detail / 🔄 Live buttons on old alerts keep working
        # after a restart.
        for _cid, _gid in list(watch._watches):
            self.add_view(watch.WatchAlertView(_gid))
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
            "Live scores from **1xBet 🟠** for game search and watches; "
            "**SofaScore 🟡** + **ESPN 🟢** + **TheSportsDB** for fixtures and /live.\n"
        ),
        color=0x00C851,
    )
    embed.add_field(name="📡 Live", value=(
        "`/live` — all live matches\n"
        "`/scoreboard` — live scoreboard in this channel\n"
        "`/game` — detail for one game (ID or team name)"
    ), inline=True)
    embed.add_field(name="🔍 Follow a game", value=(
        "`/watch` — follow one game, alerts on goals/wickets/cards\n"
        "`/unwatch` — stop following a game\n"
        "`/watching` — games this channel follows"
    ), inline=True)
    embed.add_field(name="📅 Fixtures", value=(
        "`/today` — today's fixtures\n"
        "`/tomorrow` — tomorrow's fixtures\n"
        "`/fixtures` — upcoming games"
    ), inline=True)
    embed.add_field(name="🛠 Tools", value=(
        "`/ping` — bot connection health\n"
        "`/health` — service and reminder status\n"
        "`/nextbarca` — Barcelona's next match\n"
        "`/ai` — ask Gemini a question"
    ), inline=True)
    leagues = "\n".join(f"`{lid}` {name}" for lid, name in TRACKED_LEAGUES.items() if lid in SPORTSDB_LEAGUE_MAP)
    embed.add_field(name="🏆 Available Leagues", value=leagues, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Check whether the bot is online")
async def cmd_ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong — {round(bot.latency * 1000)} ms", ephemeral=True
    )


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


async def _ask_gemini(prompt: str) -> str:
    if ai_model is None:
        raise RuntimeError("Gemini is not configured on this bot.")
    response = await asyncio.to_thread(ai_model.generate_content, prompt)
    text = getattr(response, "text", "").strip()
    if not text:
        raise RuntimeError("Gemini returned no text for that request.")
    return text


@bot.tree.command(name="ai", description="Ask Gemini a question")
@app_commands.describe(prompt="Your question or request")
async def cmd_ai(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)
    try:
        answer = await _ask_gemini(prompt)
    except Exception:
        log.exception("[ai] Request failed")
        await interaction.followup.send(
            "❌ AI is temporarily unavailable. Please try again shortly.", ephemeral=True
        )
        return

    for start in range(0, len(answer), 2000):
        await interaction.followup.send(answer[start:start + 2000])


# ── /live ────────────────────────────────────────────────────────────────────────
@bot.tree.command(name="live", description="All live matches right now")
async def cmd_live(interaction: discord.Interaction):
    await interaction.response.defer()
    espn_tasks = [espn_live(slug, lid) for lid, slug in ESPN_LEAGUE_MAP.items()]
    results = await asyncio.gather(
        sportsdb_live_matches(), sofa_live_matches(), apf_live(), onex_live_matches(), *espn_tasks,
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

    # Deduplicate: SportsDB/SofaScore/API-Football take priority over ESPN and 1xBet
    seen_pairs = {(m["home"], m["away"]) for m in sportsdb} | {(m["home"], m["away"]) for m in sofa} | {(m["home"], m["away"]) for m in apf}
    combined   = sportsdb + sofa + apf
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

    # Cross-link APF fixture IDs onto SofaScore/ESPN matches so Stats/Events buttons work
    _crosslink_apf(combined, apf)

    if not combined:
        await interaction.followup.send("No live matches right now.")
        return
    for m in combined[:5]:
        await interaction.followup.send(embed=match_embed(m), view=MatchView(m))
    if len(combined) > 5:
        await interaction.followup.send(f"…and {len(combined)-5} more live matches.")


# ── /scoreboard ───────────────────────────────────────────────────────────────
@bot.tree.command(name="scoreboard", description="Live scoreboard in this channel (NPL + top games)")
async def cmd_scoreboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        posted = await scoreboard.start_for_channel(bot, interaction.channel_id)
    except Exception:
        log.exception("[scoreboard] start failed")
        await interaction.followup.send(
            "Could not start the scoreboard here. Please try again shortly.",
            ephemeral=True)
        return
    if posted:
        await interaction.followup.send(
            "📊 Live scoreboard started in this channel — it updates when "
            "a score changes.", ephemeral=True)
    else:
        await interaction.followup.send(
            "Could not post the scoreboard here — check the bot can send "
            "messages in this channel.", ephemeral=True)


# ── /today ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="today", description="Today's fixtures for a league")
@app_commands.describe(league="Pick a league")
@app_commands.choices(league=_league_choices())
async def cmd_today(interaction: discord.Interaction, league: str):
    if not await _defer(interaction):
        return
    lid      = int(league)
    fixtures = await free_fixtures_for_day(lid, date.today().isoformat())
    embeds   = fixtures_embed(fixtures, lid, f"📅 Today — {TRACKED_LEAGUES[lid]}")
    await _send_paginated(interaction, embeds)


# ── /tomorrow ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="tomorrow", description="Tomorrow's fixtures for a league")
@app_commands.describe(league="Pick a league")
@app_commands.choices(league=_league_choices())
async def cmd_tomorrow(interaction: discord.Interaction, league: str):
    if not await _defer(interaction):
        return
    lid      = int(league)
    fixtures = await free_fixtures_for_day(lid, (date.today() + timedelta(days=1)).isoformat())
    embeds   = fixtures_embed(fixtures, lid, f"📅 Tomorrow — {TRACKED_LEAGUES[lid]}")
    await _send_paginated(interaction, embeds)


# ── /fixtures ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="fixtures", description="Upcoming fixtures for a league")
@app_commands.describe(league="Pick a league")
@app_commands.choices(league=_league_choices())
async def cmd_fixtures(interaction: discord.Interaction, league: str):
    await interaction.response.defer()
    lid      = int(league)
    fixtures = []
    for offset in range(7):
        fixtures.extend(await sportsdb_day(lid, (date.today() + timedelta(days=offset)).isoformat()))
        if len(fixtures) >= 10:
            break
    if not fixtures:
        fixtures = await apf_upcoming(lid, next_n=10)
    embeds   = fixtures_embed(fixtures, lid, f"📆 Upcoming — {TRACKED_LEAGUES[lid]}")
    await _send_paginated(interaction, embeds)
























# ── /stats (bot performance) ──────────────────────────────────────────────────────
@bot.tree.command(name="stats", description="Bot performance stats (provider latency)")
async def cmd_stats(interaction: discord.Interaction):
    from http_client import stats_summary
    embed = discord.Embed(title="📈 Bot Performance Stats", color=0x9C27B0)
    embed.description = f"```\n{stats_summary()}\n```"
    embed.add_field(name="Games watched", value=len(watch._watches), inline=True)
    embed.add_field(name="Scoreboard channels", value=len(scoreboard._state), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /game ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="game", description="Detail for one game — by ID or team name (1xBet)")
@app_commands.describe(query="Team, league or 1xBet game ID", detail="Simple or advanced detail")
@app_commands.choices(detail=[
    app_commands.Choice(name="Simple", value="simple"),
    app_commands.Choice(name="Advanced", value="advanced"),
])
async def cmd_game(interaction: discord.Interaction, query: str, detail: str = "simple"):
    if not await _defer(interaction):
        return
    try:
        result = await onex_search(query)
    except Exception:
        log.exception("[game] search failed")
        await interaction.followup.send(
            "Could not reach 1xBet right now. Please try again shortly.")
        return
    if result.get("detail"):
        await interaction.followup.send(
            watch.render_detail(result["detail"], detail))
        return
    games = result.get("games") or []
    if len(games) == 1:
        try:
            again = await onex_search(str(games[0]["onex_id"]))
        except Exception:
            again = {"detail": None}
        if again.get("detail"):
            await interaction.followup.send(
                watch.render_detail(again["detail"], detail))
            return
        await interaction.followup.send(watch.render_detail(games[0], detail))
        return
    if games:
        await interaction.followup.send(watch.render_match_list(games))
        return
    suggestions = await watch.top_live_games()
    if suggestions:
        await interaction.followup.send(
            f"No game found for '{query}' — here are the live games right now:\n"
            + watch.render_top_games(suggestions, header=""))
        return
    await interaction.followup.send(
        f"No game found for '{query}' on 1xBet. Try a team name, a league, "
        "or the game ID from the match page.")


# ── /watch ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="watch", description="Follow one game — alerts here when it changes (1xBet)")
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
                ephemeral=True)
            return
    if resolved.get("action") == "list":
        games = resolved.get("games") or []
        if games:
            await interaction.followup.send(
                watch.render_top_games(games), ephemeral=True)
        else:
            await interaction.followup.send(
                "No games are live on 1xBet right now — try again when a match is on.",
                ephemeral=True)
        return
    if resolved.get("action") == "unfollow" and resolved.get("game_id"):
        removed = await watch.remove_watch(target.id, str(resolved["game_id"]))
        if removed:
            await interaction.followup.send(
                f"🔕 Stopped watching **{removed['home']} vs {removed['away']}**.",
                ephemeral=True)
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
            if suggestions:
                await interaction.followup.send(
                    f"I could not find a game for '{what}' — here are the live games right now:\n"
                    + watch.render_top_games(suggestions, header=""),
                    ephemeral=True)
            else:
                await interaction.followup.send(
                    f"Could not find a game for '{what}' on 1xBet. Try a game ID "
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
            "Could not find that game on 1xBet right now.", ephemeral=True)
        return
    await interaction.followup.send(
        f"👀 Watching **{started['home']} vs {started['away']}** "
        f"({started['league']}) in <#{target.id}> — {level} alerts, "
        f"only when the game changes.{channel_note}", ephemeral=True)


# ── /unwatch ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="unwatch", description="Stop following a game in this channel")
@app_commands.describe(game="Game ID or team name")
async def cmd_unwatch(interaction: discord.Interaction, game: str):
    await interaction.response.defer(ephemeral=True)
    rows = await watch.list_channel_watches(interaction.channel_id)
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
    removed = await watch.remove_watch(interaction.channel_id, picked["game_id"])
    if removed:
        await interaction.followup.send(
            f"🔕 Stopped watching **{removed['home']} vs {removed['away']}**.",
            ephemeral=True)
    else:
        await interaction.followup.send("That watch is already gone.", ephemeral=True)


# ── /watching ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="watching", description="Games this channel is following")
async def cmd_watching(interaction: discord.Interaction):
    rows = await watch.list_channel_watches(interaction.channel_id)
    if not rows:
        await interaction.response.send_message(
            "No games watched in this channel. Use `/watch <game>` to follow one.",
            ephemeral=True)
        return
    lines = ["👀 **Watched games in this channel**\n"]
    for r in rows:
        snap = r.get("snapshot") or {}
        score = ""
        hs, aws = snap.get("home_score"), snap.get("away_score")
        if hs not in (None, "") or aws not in (None, ""):
            score = f" — {hs or '?'}–{aws or '?'}"
        clock = f" · {snap.get('clock')}" if snap.get("clock") else ""
        lines.append(f"• **{r['home']} vs {r['away']}**{score}{clock} "
                     f"({r['detail']}, ID `{r['game_id']}`)")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
