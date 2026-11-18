"""Notification manager (Feature 5): /notifications panel.

One ephemeral panel per invoking user, for one channel. The user
picks a watched game, chooses which event kinds to receive, and
can pause/resume alerts, switch simple/advanced detail, or
unfollow the game.

State lives in db notify_prefs (keyed by channel + game — the
prefs are channel-level by design, one watch per (channel,
game)) and in watch._watches. The view itself holds only the
transient "which game is selected" choice. Per-user isolation
is the owner check: only the user who opened the panel can
press its controls. Team follows ARE per-user (db.team_follows)
and are listed from the invoker's own follows only.

No provider name appears in any visible string.
"""
import logging

import discord

import db
import maintenance
import watch
from watch import DETAIL_ADVANCED, DETAIL_SIMPLE, EVENT_KINDS

log = logging.getLogger("notify_manager")

OWNER_ONLY = "These settings are for the person who opened them."
PICK_FIRST = "Pick a game first."
NO_WATCHES_OPTION = "No games watched in this channel"

# Short plain label per event kind the engine can detect. A
# second yellow is NOT a separate kind — the feed's card counts
# don't distinguish it, so it surfaces as the red-card count
# going up — and VAR has no feed signal, so neither is ever
# offered as a selectable kind.
EVENT_LABELS: dict[str, str] = {
    "goal": "⚽ Goals",
    "wicket": "🏏 Wickets",
    "yellow": "🟨 Yellow cards",
    "red": "🟥 Red cards",
    "penalty": "🎯 Penalties",
    "sub": "🔄 Substitutions",
    "kickoff": "▶ Kickoff",
    "ht": "⏸ Half-time",
    "second_half": "▶ Second half",
    "ft": "⏱ Full-time",
    "break": "☕ Innings break",
    "correction": "✏️ Score corrections",
    "postponed": "📅 Postponed",
    "suspended": "⏸ Suspended",
    "abandoned": "⚠️ Abandoned",
    "cancelled": "❌ Cancelled",
}


def _events_summary(events: list[str] | None) -> str:
    """Human-readable event list; an empty list means all kinds."""
    if not events:
        return "all events"
    labels = [EVENT_LABELS.get(k, k) for k in events if k in EVENT_KINDS]
    return ", ".join(labels) if labels else "all events"


def _selected_values(select, interaction) -> list[str]:
    """Values chosen in a select. In production discord.py fills
    select.values during dispatch; when a callback is invoked
    directly (tests) the values ride on interaction.data."""
    try:
        vals = list(getattr(select, "values", None) or [])
    except Exception:
        vals = []
    if not vals:
        data = getattr(interaction, "data", None) or {}
        vals = list(data.get("values") or [])
    return vals


class NotifyManagerView(discord.ui.View):
    """Persistent (timeout=None) manager panel for one user and
    one channel. The coordinator wires it to /notifications and
    passes the channel's current watches; prefs are pre-fetched
    so the Pause / detail buttons start with the right labels."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Maintenance mode: a press answers with the short notice
        # and nothing runs.
        return not await maintenance.view_blocked(interaction)

    def __init__(self, owner_id: int, channel_id: int,
                 watches: list[dict] | None = None,
                 prefs_by_game: dict[str, dict] | None = None):
        super().__init__(timeout=None)
        self.owner_id = int(owner_id)
        self.channel_id = int(channel_id)
        self.watches: list[dict] = list(watches or [])
        self.prefs_by_game: dict[str, dict] = dict(prefs_by_game or {})
        self.selected_game_id: str | None = None
        self.events_select.options = [
            discord.SelectOption(label=EVENT_LABELS[kind], value=kind)
            for kind in EVENT_KINDS
        ]
        self._refresh_game_options()
        self._refresh_button_labels()

    # ── option / label refresh ──────────────────────────────────

    def _watch_by_id(self, game_id: str) -> dict | None:
        for row in self.watches:
            if str(row.get("game_id")) == str(game_id):
                return row
        return None

    def _refresh_game_options(self) -> None:
        # discord.py has no per-option "disabled" flag, so with no
        # watches the whole select is disabled around the one
        # explanatory option.
        if not self.watches:
            self.game_select.disabled = True
            self.game_select.options = [
                discord.SelectOption(label=NO_WATCHES_OPTION, value="none")]
            return
        self.game_select.disabled = False
        self.game_select.options = [
            discord.SelectOption(
                label=f"{row.get('home', '?')} vs {row.get('away', '?')}",
                value=str(row["game_id"]))
            for row in self.watches
        ]

    def _effective_detail(self, game_id: str) -> str:
        prefs = self.prefs_by_game.get(str(game_id)) or {}
        if prefs.get("detail"):
            return str(prefs["detail"])
        row = self._watch_by_id(game_id)
        if row:
            return str(row.get("detail") or DETAIL_SIMPLE)
        return DETAIL_SIMPLE

    def _refresh_button_labels(self) -> None:
        prefs = (self.prefs_by_game.get(str(self.selected_game_id))
                 if self.selected_game_id else None) or {}
        self.pause_button.label = (
            "▶ Resume" if prefs.get("paused") else "⏸ Pause")
        detail = (self._effective_detail(self.selected_game_id)
                  if self.selected_game_id else DETAIL_SIMPLE)
        self.detail_button.label = (
            "🔵 Advanced" if detail == DETAIL_ADVANCED else "🟢 Simple")

    # ── guards ──────────────────────────────────────────────────

    async def _owner_ok(self, interaction: discord.Interaction) -> bool:
        if getattr(interaction.user, "id", None) != self.owner_id:
            await interaction.response.send_message(OWNER_ONLY, ephemeral=True)
            return False
        return True

    def _selected_watch(self) -> dict | None:
        if not self.selected_game_id:
            return None
        return self._watch_by_id(self.selected_game_id)

    async def _need_game(self, interaction: discord.Interaction) -> dict | None:
        row = self._selected_watch()
        if row is None:
            await interaction.response.send_message(PICK_FIRST, ephemeral=True)
            return None
        return row

    # ── components ──────────────────────────────────────────────

    @discord.ui.select(custom_id="notify:game-select",
                       placeholder="Pick a game to manage")
    async def game_select(self, interaction: discord.Interaction,
                          select: discord.ui.Select):
        if not await self._owner_ok(interaction):
            return
        values = _selected_values(select, interaction)
        if not values or values[0] == "none":
            await interaction.response.send_message(PICK_FIRST, ephemeral=True)
            return
        self.selected_game_id = str(values[0])
        row = self._watch_by_id(self.selected_game_id)
        self._refresh_button_labels()
        name = (f"**{row['home']} vs {row['away']}**"
                if row else f"game `{self.selected_game_id}`")
        await interaction.response.send_message(
            f"Selected {name} — choose which events to receive, "
            "or use Pause / detail / Unfollow below.", ephemeral=True)

    @discord.ui.select(custom_id="notify:events-select",
                       placeholder="Choose events to receive (empty = all)",
                       min_values=0, max_values=len(EVENT_KINDS))
    async def events_select(self, interaction: discord.Interaction,
                            select: discord.ui.Select):
        if not await self._owner_ok(interaction):
            return
        row = await self._need_game(interaction)
        if row is None:
            return
        game_id = str(row["game_id"])
        chosen = [v for v in _selected_values(select, interaction)
                  if v in EVENT_KINDS]
        try:
            stored = await db.set_prefs(self.channel_id, game_id,
                                        events=chosen)
        except Exception as exc:
            log.warning("[notify] set_prefs failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "Could not save those settings. Please try again shortly.",
                ephemeral=True)
            return
        self.prefs_by_game[game_id] = stored
        await interaction.response.send_message(
            f"🔔 **{row['home']} vs {row['away']}** will now alert on "
            f"{_events_summary(chosen)}.", ephemeral=True)

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.secondary,
                       custom_id="notify:pause")
    async def pause_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        if not await self._owner_ok(interaction):
            return
        row = await self._need_game(interaction)
        if row is None:
            return
        game_id = str(row["game_id"])
        try:
            current = await db.get_prefs(self.channel_id, game_id)
            paused = not bool((current or {}).get("paused"))
            stored = await db.set_prefs(self.channel_id, game_id,
                                        paused=paused)
        except Exception as exc:
            log.warning("[notify] pause toggle failed (%s)",
                        type(exc).__name__)
            await interaction.response.send_message(
                "Could not change the pause setting. Please try again shortly.",
                ephemeral=True)
            return
        self.prefs_by_game[game_id] = stored
        self._refresh_button_labels()
        state = "paused — no alerts will be sent" if paused else "resumed"
        await interaction.response.send_message(
            f"🔔 **{row['home']} vs {row['away']}** alerts {state}.",
            ephemeral=True)

    @discord.ui.button(label="🟢 Simple", style=discord.ButtonStyle.secondary,
                       custom_id="notify:detail")
    async def detail_button(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        if not await self._owner_ok(interaction):
            return
        row = await self._need_game(interaction)
        if row is None:
            return
        game_id = str(row["game_id"])
        new_detail = (DETAIL_SIMPLE
                      if self._effective_detail(game_id) == DETAIL_ADVANCED
                      else DETAIL_ADVANCED)
        try:
            stored = await db.set_prefs(self.channel_id, game_id,
                                        detail=new_detail)
        except Exception as exc:
            log.warning("[notify] detail toggle failed (%s)",
                        type(exc).__name__)
            await interaction.response.send_message(
                "Could not change the detail level. Please try again shortly.",
                ephemeral=True)
            return
        self.prefs_by_game[game_id] = stored
        # Keep the in-memory watch in step — the tick reads the
        # prefs override first, but /watching shows the watch row.
        entry = watch._watches.get((self.channel_id, game_id))
        if entry is not None:
            entry["detail"] = new_detail
        self._refresh_button_labels()
        await interaction.response.send_message(
            f"🔔 **{row['home']} vs {row['away']}** alerts are now "
            f"**{new_detail}**.", ephemeral=True)

    @discord.ui.button(label="🔕 Unfollow", style=discord.ButtonStyle.danger,
                       custom_id="notify:unfollow")
    async def unfollow_button(self, interaction: discord.Interaction,
                              button: discord.ui.Button):
        if not await self._owner_ok(interaction):
            return
        row = await self._need_game(interaction)
        if row is None:
            return
        game_id = str(row["game_id"])
        try:
            removed = await watch.remove_watch(self.channel_id, game_id)
            await db.delete_prefs(self.channel_id, game_id)
        except Exception as exc:
            log.warning("[notify] unfollow failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "Could not stop watching that game. Please try again shortly.",
                ephemeral=True)
            return
        self.watches = [r for r in self.watches
                        if str(r.get("game_id")) != game_id]
        self.prefs_by_game.pop(game_id, None)
        self.selected_game_id = None
        self._refresh_game_options()
        self._refresh_button_labels()
        name = (f"**{removed['home']} vs {removed['away']}**" if removed
                else f"game `{game_id}`")
        await interaction.response.send_message(
            f"🔕 Stopped watching {name}.", ephemeral=True)


async def show_notifications(interaction: discord.Interaction) -> None:
    """Handler the coordinator wires to /notifications: defers
    ephemerally, lists this channel's watches + the invoker's
    team follows + the current prefs summary, then sends the
    summary with a NotifyManagerView. Never raises — a failed
    read degrades to an empty section instead of an error."""
    await interaction.response.defer(ephemeral=True)
    channel_id = int(interaction.channel_id)
    user_id = int(getattr(interaction.user, "id", 0))
    try:
        watches = await watch.list_channel_watches(channel_id)
    except Exception as exc:
        log.warning("[notify] list watches failed (%s)", type(exc).__name__)
        watches = []
    try:
        prefs_rows = await db.list_prefs_for_channel(channel_id)
    except Exception as exc:
        log.warning("[notify] list prefs failed (%s)", type(exc).__name__)
        prefs_rows = []
    prefs_by_game = {str(r.get("game_id")): r for r in prefs_rows}
    try:
        follows = await db.list_team_follows(user_id)
    except Exception as exc:
        log.warning("[notify] list team follows failed (%s)",
                    type(exc).__name__)
        follows = []
    follows = [f for f in follows
               if int(f.get("channel_id") or 0) == channel_id]

    lines = ["🔔 **Notification settings for this channel**", ""]
    if watches:
        lines.append("**Watched games**")
        for row in watches:
            prefs = prefs_by_game.get(str(row["game_id"])) or {}
            bits = [f"{row.get('detail') or DETAIL_SIMPLE} alerts"]
            bits.append(_events_summary(prefs.get("events")))
            if prefs.get("paused"):
                bits.append("⏸ paused")
            lines.append(f"• **{row.get('home', '?')} vs "
                         f"{row.get('away', '?')}** — " + ", ".join(bits))
        lines.append("")
    else:
        lines.append("No games watched in this channel yet. "
                     "Use `/watch <game>` to follow one.")
        lines.append("")
    if follows:
        lines.append("**Teams you follow here**")
        for f in follows:
            lines.append(f"• **{f.get('team_name', '?')}** "
                         f"({f.get('detail') or DETAIL_SIMPLE} alerts)")
        lines.append("Team follows are removed via /ask — "
                     "say `unfollow <team>`.")
        lines.append("")
    lines.append("Pick a game below to manage its alerts.")
    try:
        await interaction.followup.send(
            "\n".join(lines),
            view=NotifyManagerView(user_id, channel_id, watches,
                                   prefs_by_game),
            ephemeral=True)
    except Exception as exc:
        log.warning("[notify] show failed (%s)", type(exc).__name__)
        try:
            await interaction.followup.send(
                "Could not load notification settings. "
                "Please try again shortly.", ephemeral=True)
        except Exception:
            pass
