"""
Discord embed builders and interactive Views.
"""
import discord
from config import LEAGUE_COLORS, LEAGUE_LOGOS, TRACKED_LEAGUES


def _color(league_id: int) -> int:
    return LEAGUE_COLORS.get(league_id, 0x2B2D31)


def _logo(league_id: int) -> str:
    return LEAGUE_LOGOS.get(league_id, "")


_SOURCE_BADGES = {
    "sofascore":   "🟡 SofaScore",
    "apifootball": "🔵 API-Football",
    "espn":        "🟢 ESPN",
    "onexbet":     "🟠 Live",
}
_SOURCE_DOTS = {"sofascore": "🟡", "apifootball": "🔵", "espn": "🟢", "onexbet": "🟠"}


def _source_badge(provider: str) -> str:
    return _SOURCE_BADGES.get(provider, "⚪ Unknown")


def _source_dot(provider: str) -> str:
    return _SOURCE_DOTS.get(provider, "⚪")


def _apf_id(match: dict) -> int | None:
    """
    Return a numeric API-Football fixture ID from a match dict, or None.
    ESPN IDs ("espn_...") and SofaScore IDs ("sofa_...") are not valid
    APF IDs and must be rejected before passing to apf_* functions.
    """
    raw = match.get("apf_id") or match.get("id")
    if raw is None:
        return None
    s = str(raw)
    return int(s) if s.isdigit() else None


# ── Match card ────────────────────────────────────────────────────────────────────────

def match_embed(m: dict, title_prefix: str = "") -> discord.Embed:
    lid    = m.get("league_id", 0)
    sh     = m["home_score"] if m["home_score"] is not None else "-"
    sa     = m["away_score"] if m["away_score"] is not None else "-"
    min_   = f" {m['minute']}'" if m.get("minute") else ""
    src    = _source_badge(m.get("provider", ""))
    status = m.get("status", "?")

    embed = discord.Embed(
        title=f"{title_prefix}⚽  {m['home']}  {sh} – {sa}  {m['away']}",
        color=_color(lid),
    )
    embed.set_author(name=m["league"], icon_url=_logo(lid))

    info_parts = [f"**Status:** `{status}{min_}`"]
    if m.get("stadium"):
        info_parts.append(f"**Venue:** {m['stadium']}")
    if m.get("referee"):
        info_parts.append(f"**Referee:** {m['referee']}")
    if m.get("date"):
        info_parts.append(f"**Date:** {m['date']}")

    embed.description = "\n".join(info_parts)
    embed.set_footer(text=src)
    if m.get("home_logo"):
        embed.set_thumbnail(url=m["home_logo"])
    return embed


def event_embed(ev: dict, match: dict) -> discord.Embed:
    lid = match.get("league_id", 0)
    sh  = match["home_score"] if match["home_score"] is not None else "-"
    sa  = match["away_score"] if match["away_score"] is not None else "-"
    src = _source_dot(ev.get("provider", ""))

    embed = discord.Embed(
        title=f"{ev['icon']}  {ev['detail']}  —  {ev['minute']}'",
        description=(
            f"**{ev['player']}** ({ev['team']})"
            + (f"\n↳ Assist: **{ev['assist']}**" if ev.get("assist") else "")
        ),
        color=_color(lid),
    )
    embed.set_author(name=f"{match['home']} {sh}–{sa} {match['away']}  |  {match['league']}",
                     icon_url=_logo(lid))
    embed.set_footer(text=f"via {src}")
    return embed


def alert_embed(msg_type: str, match: dict) -> discord.Embed:
    icons = {
        "kickoff":   ("🟢", "Kick Off!"),
        "ht":        ("⏸️", "Half Time"),
        "2h":        ("▶️", "Second Half Started"),
        "et":        ("⏱️", "Extra Time"),
        "pens":      ("🎯", "Penalty Shootout!"),
        "ft":        ("🏁", "Full Time"),
        "pst":       ("📅", "Match Postponed"),
        "canc":      ("❌", "Match Cancelled"),
        "abd":       ("⚠️", "Match Abandoned"),
        "int":       ("⏸️", "Match Interrupted"),
    }
    lid  = match.get("league_id", 0)
    sh   = match["home_score"] if match["home_score"] is not None else "-"
    sa   = match["away_score"] if match["away_score"] is not None else "-"
    icon, label = icons.get(msg_type, ("📌", msg_type))

    embed = discord.Embed(
        title=f"{icon}  {label}",
        description=f"**{match['home']}  {sh} – {sa}  {match['away']}**",
        color=_color(lid),
    )
    embed.set_author(name=match["league"], icon_url=_logo(lid))
    if match.get("stadium"):
        embed.set_footer(text=f"🏙 {match['stadium']}")
    return embed


# ── Fixtures list ────────────────────────────────────────────────────────────────────

def fixtures_embed(fixtures: list[dict], league_id: int, title: str) -> list[discord.Embed]:
    """Paginate fixtures into ≤10 per embed."""
    embeds = []
    chunk  = 10
    for i in range(0, len(fixtures), chunk):
        page = fixtures[i:i+chunk]
        embed = discord.Embed(title=title, color=_color(league_id))
        embed.set_author(name=TRACKED_LEAGUES.get(league_id, ""), icon_url=_logo(league_id))
        lines = []
        for f in page:
            sh = f["home_score"] if f["home_score"] is not None else "vs"
            sa = f["away_score"] if f["away_score"] is not None else ""
            score = f"{sh}–{sa}" if sa != "" else sh
            lines.append(f"`{f['date']}`  **{f['home']}** {score} **{f['away']}**  `{f['status']}`")
        embed.description = "\n".join(lines)
        embeds.append(embed)
    return embeds or [discord.Embed(title=title, description="No fixtures found.", color=0x888888)]


# ── Standings ──────────────────────────────────────────────────────────────────────



# ── Top scorers ────────────────────────────────────────────────────────────────────



# ── Team last N ────────────────────────────────────────────────────────────────────



# ── Injuries ───────────────────────────────────────────────────────────────────────



# ── H2H ─────────────────────────────────────────────────────────────────────────────



# ── Lineups ────────────────────────────────────────────────────────────────────────



# ── Prediction ─────────────────────────────────────────────────────────────────────



# ══════════════════════════════════════════════════════════════════════════
# Interactive Views
# ══════════════════════════════════════════════════════════════════════════

class MatchView(discord.ui.View):
    """Buttons: Stats · Events · Refresh attached to a live match embed."""

    def __init__(self, match: dict):
        super().__init__(timeout=300)
        self.match = match

    @discord.ui.button(label="📊 Stats", style=discord.ButtonStyle.secondary)
    async def stats_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        from providers import apf_stats
        apf_id = _apf_id(self.match)
        await interaction.response.defer(ephemeral=True)
        if apf_id is None:
            await interaction.followup.send(
                "Stats require an API-Football fixture ID. "
                "This match was sourced from ESPN or SofaScore only.",
                ephemeral=True,
            )
            return
        stats = await apf_stats(apf_id)
        if not stats:
            await interaction.followup.send("No stats available yet.", ephemeral=True)
            return
        lines = []
        for block in stats:
            lines.append(f"**{block['team']['name']}**")
            for s in block["statistics"][:10]:
                lines.append(f"  {s['type']}: {s['value'] or '—'}")
        await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)

    @discord.ui.button(label="📋 Events", style=discord.ButtonStyle.secondary)
    async def events_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        from providers import apf_events
        apf_id = _apf_id(self.match)
        await interaction.response.defer(ephemeral=True)
        if apf_id is None:
            await interaction.followup.send(
                "Events require an API-Football fixture ID. "
                "This match was sourced from ESPN or SofaScore only.",
                ephemeral=True,
            )
            return
        events = await apf_events(apf_id)
        if not events:
            await interaction.followup.send("No events yet.", ephemeral=True)
            return
        lines = [f"{e['icon']} **{e['minute']}'** {e['player']} ({e['team']})" for e in events]
        await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary)
    async def refresh_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.defer()
        embed = match_embed(self.match, title_prefix="🔄 ")
        await interaction.edit_original_response(embed=embed, view=self)
