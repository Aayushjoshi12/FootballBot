"""
Football Discord Bot — production grade
Commands: /live /fixtures /match /team /standings /subscribe /unsubscribe /subs
          /today /tomorrow /h2h /lineups /topscorers /injuries /form /player
          /prediction /transfers /favorites /stats
"""
import asyncio
import logging

import discord
from discord import app_commands

import db
import http_client as hc
import notifier
from config import DISCORD_BOT_TOKEN, TRACKED_LEAGUES, ESPN_LEAGUE_MAP
from embeds import (
    match_embed, fixtures_embed, standings_embed, team_embed,
    injuries_embed, h2h_embed, lineups_embed, topscorers_embed,
    prediction_embed, MatchView,
)
from providers import (
    sofa_live_matches, apf_live, espn_live, apf_upcoming, apf_stats, apf_events,
    apf_standings, apf_team_fixtures, apf_search_team, apf_topscorers,
    apf_injuries, apf_h2h, apf_prediction, apf_lineups, apf_transfers,
    apf_player, apf_today, apf_tomorrow,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("bot")


def _league_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=name, value=str(lid)) for lid, name in TRACKED_LEAGUES.items()]


class FootballBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await db.init()
        notifier.subscriptions = await db.get_all_subscriptions()
        await self.tree.sync()
        self.loop.create_task(notifier.poll_loop(self))
        log.info("[bot] Setup complete. %d channels subscribed.", len(notifier.subscriptions))

    async def on_ready(self):
        log.info("[bot] Ready as %s", self.user)
        await self.change_presence(activity=discord.Game("⚽ Football Live"))

    async def close(self):
        await hc.close()
        await db.close()
        await super().close()


bot = FootballBot()


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


async def _find_team(name: str) -> tuple[dict | None, list]:
    results = await apf_search_team(name)
    if not results:
        return None, []
    return results[0]["team"], results


# ══════════════════════════════════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="help", description="Show all commands")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚽ Football Bot",
        description=(
            "Tri-provider: **SofaScore 🟡** + **API-Football 🔵** + **ESPN 🟢**\n"
            "Live alerts sent to subscribed channels automatically.\n​"
        ),
        color=0x00C851,
    )
    embed.add_field(name="📡 Live", value=(
        "`/live` — all live matches\n"
        "`/today` — today's fixtures\n"
        "`/tomorrow` — tomorrow's fixtures\n"
        "`/match` — events + stats\n"
        "`/lineups` — team lineups"
    ), inline=True)
    embed.add_field(name="📊 Stats", value=(
        "`/standings` — league table\n"
        "`/topscorers` — top scorers\n"
        "`/form` — team recent form\n"
        "`/h2h` — head to head\n"
        "`/prediction` — match prediction"
    ), inline=True)
    embed.add_field(name="🔍 Search", value=(
        "`/team` — last 5 results\n"
        "`/player` — player stats\n"
        "`/injuries` — team injuries\n"
        "`/transfers` — team transfers\n"
        "`/fixtures` — upcoming games"
    ), inline=True)
    embed.add_field(name="🔔 Alerts", value=(
        "`/subscribe` — live alerts here\n"
        "`/unsubscribe` — stop alerts\n"
        "`/subs` — active subscriptions"
    ), inline=True)
    embed.add_field(name="⭐ Favorites", value=(
        "`/favorites list` — your teams\n"
        "`/favorites add` — add a team\n"
        "`/favorites remove` — remove a team"
    ), inline=True)
    leagues = "\n".join(f"`{lid}` {name}" for lid, name in TRACKED_LEAGUES.items())
    embed.add_field(name="🏆 Available Leagues", value=leagues, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /live ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="live", description="All live matches right now")
async def cmd_live(interaction: discord.Interaction):
    await interaction.response.defer()
    espn_tasks = [espn_live(slug, lid) for lid, slug in ESPN_LEAGUE_MAP.items()]
    results = await asyncio.gather(
        sofa_live_matches(), apf_live(), *espn_tasks, return_exceptions=True
    )
    sofa, apf, *espn_results = results
    sofa = sofa if isinstance(sofa, list) else []
    apf  = apf  if isinstance(apf,  list) else []
    espn: list = []
    for r in espn_results:
        if isinstance(r, tuple):
            espn.extend(r[0])

    seen_pairs = {(m["home"], m["away"]) for m in sofa} | {(m["home"], m["away"]) for m in apf}
    combined   = sofa + apf
    for m in espn:
        pair = (m["home"], m["away"])
        if pair not in seen_pairs:
            combined.append(m)
            seen_pairs.add(pair)

    if not combined:
        await interaction.followup.send("No live matches right now.")
        return
    for m in combined[:5]:
        await interaction.followup.send(embed=match_embed(m), view=MatchView(m))
    if len(combined) > 5:
        # Use followup instead of channel.send — channel can be None in DMs
        await interaction.followup.send(f"…and {len(combined)-5} more live matches.")


# ── /today ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="today", description="Today's fixtures for a league")
@app_commands.describe(league="Pick a league")
@app_commands.choices(league=_league_choices())
async def cmd_today(interaction: discord.Interaction, league: str):
    await interaction.response.defer()
    lid      = int(league)
    fixtures = await apf_today(lid)
    embeds   = fixtures_embed(fixtures, lid, f"📅 Today — {TRACKED_LEAGUES[lid]}")
    await _send_paginated(interaction, embeds)


# ── /tomorrow ──────────────────────────────────────────────────────────────
@bot.tree.command(name="tomorrow", description="Tomorrow's fixtures for a league")
@app_commands.describe(league="Pick a league")
@app_commands.choices(league=_league_choices())
async def cmd_tomorrow(interaction: discord.Interaction, league: str):
    await interaction.response.defer()
    lid      = int(league)
    fixtures = await apf_tomorrow(lid)
    embeds   = fixtures_embed(fixtures, lid, f"📅 Tomorrow — {TRACKED_LEAGUES[lid]}")
    await _send_paginated(interaction, embeds)


# ── /fixtures ──────────────────────────────────────────────────────────────
@bot.tree.command(name="fixtures", description="Upcoming fixtures for a league")
@app_commands.describe(league="Pick a league")
@app_commands.choices(league=_league_choices())
async def cmd_fixtures(interaction: discord.Interaction, league: str):
    await interaction.response.defer()
    lid      = int(league)
    fixtures = await apf_upcoming(lid, next_n=10)
    embeds   = fixtures_embed(fixtures, lid, f"📆 Upcoming — {TRACKED_LEAGUES[lid]}")
    await _send_paginated(interaction, embeds)


# ── /match ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="match", description="Events and stats for a fixture")
@app_commands.describe(fixture_id="APF Fixture ID (from /live or /fixtures)")
async def cmd_match(interaction: discord.Interaction, fixture_id: int):
    await interaction.response.defer()
    events, stats = await asyncio.gather(apf_events(fixture_id), apf_stats(fixture_id))
    if not events and not stats:
        await interaction.followup.send("No data found. Check the fixture ID.")
        return

    ev_lines = [f"{e['icon']} **{e['minute']}'** {e['player']} ({e['team']})" for e in events]
    msg = "📋 **Events**\n\n" + ("\n".join(ev_lines) if ev_lines else "No events yet.")
    if stats:
        msg += "\n\n📊 **Stats**\n"
        for block in stats:
            msg += f"\n**{block['team']['name']}**\n"
            for s in block["statistics"][:10]:
                msg += f"  {s['type']}: {s['value'] or '—'}\n"
    for chunk in [msg[i:i+2000] for i in range(0, len(msg), 2000)]:
        await interaction.followup.send(chunk)


# ── /team ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="team", description="Last 5 results for a team")
@app_commands.describe(name="Team name")
async def cmd_team(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    team, _ = await _find_team(name)
    if not team:
        await interaction.followup.send("Team not found.")
        return
    fixtures = await apf_team_fixtures(team["id"], last=5)
    await interaction.followup.send(embed=team_embed(team["name"], fixtures))


# ── /form ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="form", description="Recent form for a team (last 10)")
@app_commands.describe(name="Team name")
async def cmd_form(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    team, _ = await _find_team(name)
    if not team:
        await interaction.followup.send("Team not found.")
        return
    fixtures = await apf_team_fixtures(team["id"], last=10)
    await interaction.followup.send(embed=team_embed(team["name"], fixtures))


# ── /standings ─────────────────────────────────────────────────────────────
@bot.tree.command(name="standings", description="League table")
@app_commands.describe(league="Pick a league")
@app_commands.choices(league=_league_choices())
async def cmd_standings(interaction: discord.Interaction, league: str):
    await interaction.response.defer()
    lid  = int(league)
    data = await apf_standings(lid)
    await interaction.followup.send(embed=standings_embed(data, lid))


# ── /topscorers ────────────────────────────────────────────────────────────
@bot.tree.command(name="topscorers", description="Top scorers for a league")
@app_commands.describe(league="Pick a league")
@app_commands.choices(league=_league_choices())
async def cmd_topscorers(interaction: discord.Interaction, league: str):
    await interaction.response.defer()
    lid  = int(league)
    data = await apf_topscorers(lid)
    await interaction.followup.send(embed=topscorers_embed(data, lid))


# ── /injuries ──────────────────────────────────────────────────────────────
@bot.tree.command(name="injuries", description="Team injury list")
@app_commands.describe(name="Team name")
async def cmd_injuries(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    team, _ = await _find_team(name)
    if not team:
        await interaction.followup.send("Team not found.")
        return
    injuries = await apf_injuries(team["id"])
    await interaction.followup.send(embed=injuries_embed(team["name"], injuries))


# ── /h2h ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="h2h", description="Head to head between two teams")
@app_commands.describe(team1="First team", team2="Second team")
async def cmd_h2h(interaction: discord.Interaction, team1: str, team2: str):
    await interaction.response.defer()
    t1, _ = await _find_team(team1)
    t2, _ = await _find_team(team2)
    if not t1 or not t2:
        await interaction.followup.send("One or both teams not found.")
        return
    fixtures = await apf_h2h(t1["id"], t2["id"])
    await interaction.followup.send(embed=h2h_embed(t1["name"], t2["name"], fixtures))


# ── /lineups ───────────────────────────────────────────────────────────────
@bot.tree.command(name="lineups", description="Match lineups (APF fixture ID)")
@app_commands.describe(fixture_id="APF Fixture ID")
async def cmd_lineups(interaction: discord.Interaction, fixture_id: int):
    await interaction.response.defer()
    lineups = await apf_lineups(fixture_id)
    if not lineups:
        await interaction.followup.send("No lineup data yet.")
        return
    m = {"home": lineups[0]["team"]["name"] if lineups else "?",
         "away": lineups[1]["team"]["name"] if len(lineups) > 1 else "?",
         "league_id": 0}
    await interaction.followup.send(embed=lineups_embed(m, lineups))


# ── /prediction ────────────────────────────────────────────────────────────
@bot.tree.command(name="prediction", description="AI match prediction (APF fixture ID)")
@app_commands.describe(fixture_id="APF Fixture ID")
async def cmd_prediction(interaction: discord.Interaction, fixture_id: int):
    await interaction.response.defer()
    pred = await apf_prediction(fixture_id)
    if not pred:
        await interaction.followup.send("No prediction available.")
        return
    m = {"home": pred.get("teams", {}).get("home", {}).get("name", "?"),
         "away": pred.get("teams", {}).get("away", {}).get("name", "?"),
         "league_id": 0}
    await interaction.followup.send(embed=prediction_embed(m, pred))


# ── /player ────────────────────────────────────────────────────────────────
@bot.tree.command(name="player", description="Player stats by ID")
@app_commands.describe(player_id="Player ID from API-Football")
async def cmd_player(interaction: discord.Interaction, player_id: int):
    await interaction.response.defer()
    data = await apf_player(player_id)
    if not data:
        await interaction.followup.send("Player not found.")
        return
    p    = data.get("player", {})
    stat = (data.get("statistics") or [{}])[0]
    embed = discord.Embed(title=f"👤 {p.get('name','?')}", color=0x2196F3)
    embed.set_thumbnail(url=p.get("photo", ""))
    embed.add_field(name="Age",         value=p.get("age", "?"),                             inline=True)
    embed.add_field(name="Nationality", value=p.get("nationality", "?"),                      inline=True)
    embed.add_field(name="Team",        value=stat.get("team", {}).get("name", "?"),         inline=True)
    embed.add_field(name="Goals",       value=stat.get("goals", {}).get("total", 0),         inline=True)
    embed.add_field(name="Assists",     value=stat.get("goals", {}).get("assists", 0),       inline=True)
    embed.add_field(name="Apps",        value=stat.get("games", {}).get("appearences", 0),   inline=True)
    embed.add_field(name="Rating",      value=stat.get("games", {}).get("rating") or "N/A",  inline=True)
    embed.add_field(name="Yellow",      value=stat.get("cards", {}).get("yellow", 0),        inline=True)
    embed.add_field(name="Red",         value=stat.get("cards", {}).get("red", 0),           inline=True)
    await interaction.followup.send(embed=embed)


# ── /transfers ─────────────────────────────────────────────────────────────
@bot.tree.command(name="transfers", description="Recent transfers for a team")
@app_commands.describe(name="Team name")
async def cmd_transfers(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    team, _ = await _find_team(name)
    if not team:
        await interaction.followup.send("Team not found.")
        return
    transfers = await apf_transfers(team["id"])
    if not transfers:
        await interaction.followup.send("No transfer data found.")
        return
    embed = discord.Embed(title=f"💰 {team['name']} — Transfers", color=0xFFD700)
    lines = []
    for item in transfers[:15]:
        p = item.get("player", {})
        for t in (item.get("transfers") or [])[:1]:
            date = t.get("date", "")[:10]
            tin  = t.get("teams", {}).get("in",  {}).get("name", "?")
            tout = t.get("teams", {}).get("out", {}).get("name", "?")
            fee  = t.get("type", "?")
            lines.append(f"`{date}` **{p.get('name','')}** {tout} → {tin}  `{fee}`")
    embed.description = "\n".join(lines) or "No transfers."
    await interaction.followup.send(embed=embed)


# ── /subscribe ─────────────────────────────────────────────────────────────
@bot.tree.command(name="subscribe", description="Get live alerts in this channel")
@app_commands.describe(league="League to subscribe to")
@app_commands.choices(league=_league_choices())
async def cmd_subscribe(interaction: discord.Interaction, league: str):
    lid = int(league)
    cid = interaction.channel_id
    gid = interaction.guild_id or 0
    await db.add_subscription(gid, cid, lid)
    notifier.subscriptions.setdefault(cid, set()).add(lid)
    embed = discord.Embed(
        title="✅ Subscribed!",
        description=f"This channel will now receive **{TRACKED_LEAGUES[lid]}** live alerts.",
        color=0x00C851,
    )
    await interaction.response.send_message(embed=embed)


# ── /unsubscribe ───────────────────────────────────────────────────────────
@bot.tree.command(name="unsubscribe", description="Stop all alerts in this channel")
async def cmd_unsubscribe(interaction: discord.Interaction):
    cid = interaction.channel_id
    await db.remove_subscriptions(cid)
    notifier.subscriptions.pop(cid, None)
    await interaction.response.send_message(
        embed=discord.Embed(title="🔕 Unsubscribed", description="No more alerts in this channel.", color=0xFF4444)
    )


# ── /subs ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="subs", description="Active subscriptions for this channel")
async def cmd_subs(interaction: discord.Interaction):
    leagues = notifier.subscriptions.get(interaction.channel_id, set())
    if not leagues:
        await interaction.response.send_message("No active subscriptions. Use `/subscribe`.", ephemeral=True)
        return
    embed = discord.Embed(title="📡 Subscriptions", color=0x2196F3)
    embed.description = "\n".join(f"• **{TRACKED_LEAGUES.get(l, l)}**" for l in leagues)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /favorites ─────────────────────────────────────────────────────────────
favorites_group = app_commands.Group(name="favorites", description="Manage your favorite teams")
bot.tree.add_command(favorites_group)


@favorites_group.command(name="list", description="List your favorite teams")
async def fav_list(interaction: discord.Interaction):
    favs = await db.get_favorites(interaction.user.id)
    if not favs:
        await interaction.response.send_message("No favorites yet. Use `/favorites add`.", ephemeral=True)
        return
    embed = discord.Embed(title="⭐ Your Favorite Teams", color=0xFFD700)
    embed.description = "\n".join(f"• **{f['team_name']}** (ID: {f['team_id']})" for f in favs)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@favorites_group.command(name="add", description="Add a team to favorites")
@app_commands.describe(name="Team name to search and add")
async def fav_add(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    team, _ = await _find_team(name)
    if not team:
        await interaction.followup.send("Team not found.", ephemeral=True)
        return
    await db.add_favorite(interaction.user.id, team["id"], team["name"])
    await interaction.followup.send(f"⭐ **{team['name']}** added to favorites!", ephemeral=True)


@favorites_group.command(name="remove", description="Remove a team from favorites")
@app_commands.describe(team_id="Team ID to remove")
async def fav_remove(interaction: discord.Interaction, team_id: int):
    await db.remove_favorite(interaction.user.id, team_id)
    await interaction.response.send_message(f"Removed team `{team_id}` from favorites.", ephemeral=True)


# ── /stats (bot performance) ───────────────────────────────────────────────
@bot.tree.command(name="stats", description="Bot performance stats (provider latency)")
async def cmd_stats(interaction: discord.Interaction):
    from http_client import stats_summary
    embed = discord.Embed(title="📈 Bot Performance Stats", color=0x9C27B0)
    embed.description = f"```\n{stats_summary()}\n```"
    embed.add_field(name="Live fixtures tracked", value=len(notifier._state),        inline=True)
    embed.add_field(name="Subscribed channels",   value=len(notifier.subscriptions), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
