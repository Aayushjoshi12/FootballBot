"""Per-game watch: /game detail, /watch alerts, AI resolution.

1xBet ONLY — every game, score and stat here comes from 1xBet's
service-api (via providers.onex_*). No other provider is touched.

WATCH vs EDIT, same rule as the scoreboard: the bot WATCHES 1xBet
every second (shared with the scoreboard loop — on_tick() is called
from scoreboard_loop with that tick's raw live entries, so there is
NO second 1-second poller) but it SENDS a message only when a
watched game actually changes: a goal, a wicket, a card, an innings
break, full-time. A score that only ticks over in cricket (runs
with no wicket) sends nothing.

LIVE PANEL: every watched game also gets ONE panel message that
edits in place on the same 1s tick — score + ticking MM:SS clock
(or overs for cricket) — so the time and score change live without
a new message. Routine changes only ever edit the panel; only
goals, wickets, cards and other big events send a separate
notification. When the game ends the panel shows the final score
and stops ticking. The panel proves it is alive with an
"Updated HH:MM:SS NPT" footer: the footer advances on every
edit, and when the game itself has not changed the tick
heartbeat-edits the footer alone every second, so a cricket
panel whose score has not moved for an over still shows life —
the panel edits once a second, like the ticking clock on the
site. Detail / Live / Simple / Advanced / Refresh send the
requested view as a NEW channel message — and that message
BECOMES the live panel: the tick edits it from then on, in
the tapped view, while the old panel message is left frozen
(the tap itself never edits any message in place; see
_switch_panel_view). Timeline, Follow, Unfollow
and the 👀 Watching list likewise send new channel messages;
Watching also carries one ❌ Remove button per watched game.

A /game reply IS a panel too: the same tick edits the detail card
in place (panel_kind "detail") while the game is live. Follow
controls notifications ONLY, not the live view — a followed game
ticks its panel and sends event alerts; Unfollow stops the alerts
and the panel keeps ticking until full-time. One registry, one
tick, one edit discipline for every panel.

AI resolution: /watch takes free text. The text is resolved
deterministically first (game ID, exact team name, unique partial
name); only if that fails does the AI model see it — grounded on the
live + upcoming games list, asked for STRICT JSON, and the game_id
it returns MUST be one from that list. A made-up id is never used.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import barcelona_reminder
import channel_edits
import db
import history
import maintenance
from normalizer import (base_team_name, is_reserve_team,
                        reserve_suffix, team_name_matches)
import providers as prov

log = logging.getLogger("watch")
NEPAL = ZoneInfo("Asia/Kathmandu")

DETAIL_SIMPLE = "simple"
DETAIL_ADVANCED = "advanced"

# Every event kind the engine can detect. Prefs (Feature 5)
# filter on these; history (Feature 6) records them.
EVENT_KINDS = (
    "goal", "wicket", "yellow", "red", "penalty", "sub",
    "kickoff", "ht", "second_half", "ft", "break", "correction",
    "postponed", "suspended", "abandoned", "cancelled",
)
# Statuses that end a match without a final whistle on the
# scoreboard: the feed may report them for a fixture that never
# starts or is stopped. Normalised lower-case -> (kind, icon, label).
TERMINAL_STATUSES: dict[str, tuple[str, str, str]] = {
    "postponed": ("postponed", "📅", "Match postponed"),
    "pst": ("postponed", "📅", "Match postponed"),
    "suspended": ("suspended", "⏸", "Match suspended"),
    "interrupted": ("suspended", "⏸", "Match suspended"),
    "abandoned": ("abandoned", "⚠️", "Match abandoned"),
    "abd": ("abandoned", "⚠️", "Match abandoned"),
    "cancelled": ("cancelled", "❌", "Match cancelled"),
    "canc": ("cancelled", "❌", "Match cancelled"),
}

# A watched game that vanishes from the live list is NOT ended at
# once: the games1x2 feed caps at 50 entries, so a game can flicker
# off the list for minutes while still live (18 Nov 2026: the NPL
# Eliminator vanished that way and the 10-tick rule killed a live
# watch). The watch ends only after this long of CONTINUOUS
# absence from a live list that DID arrive — or on an explicit
# terminal status (full-time / postponed / ...), handled where the
# detail lands. A game that reappears before then resumes the SAME
# panel; a tick whose fetch failed never counts at all (see
# on_tick: it arrives as raw_games=None).
MISSING_END_AFTER_S = 300.0

# The AI model gets this long to resolve free text; on timeout the
# caller falls back to the candidate list instead of hanging the
# /watch reply. (wait_for cancels the wait, not the model call —
# the thread finishes on its own in the background.)
AI_TIMEOUT_S = 15


# ══════════════════════════════════════════════════════════════════════════
# RENDERING (pure — no Discord, no network)
# ══════════════════════════════════════════════════════════════════════════

def _score_str(d: dict) -> str:
    hs, aws = d.get("home_score"), d.get("away_score")
    if hs is None and aws is None:
        return "vs"
    return f"{hs if hs is not None else '?'} – {aws if aws is not None else '?'}"


def _clock_str(d: dict) -> str:
    if d.get("clock"):
        return str(d["clock"])
    minute = d.get("minute")
    if isinstance(minute, int):
        return f"{minute}'"
    status = str(d.get("status") or "")
    if status == "HT":
        return "Half-time"
    if status == "FT":
        return "Full-time"
    if status in ("1H", "2H", "ET", "PEN"):
        return status
    return "Live"


def format_kickoff(start_ts: int | float | None) -> str:
    if not start_ts:
        return ""
    dt = datetime.fromtimestamp(start_ts, NEPAL)
    today = datetime.now(NEPAL).date()
    day = "Today" if dt.date() == today else dt.strftime("%a %d %b")
    return f"{day}, {dt.strftime('%-I:%M %p').lower()}"


def _stat_value(stats: list[dict] | None, name: str) -> tuple[str, str] | None:
    for s in stats or []:
        if s.get("name") == name:
            return (str(s.get("home", "")), str(s.get("away", "")))
    return None


def format_stats_table(stats: list[dict] | None) -> list[str]:
    """Full stats table, one line per stat: home — name — away."""
    lines = []
    for s in stats or []:
        lines.append(f"{s.get('home', '')}  {s.get('name', '')}  {s.get('away', '')}")
    return lines


def render_detail(d: dict, level: str = DETAIL_SIMPLE,
                  updated: str | None = None) -> str:
    """One game's detail. Simple: score, clock, venue/weather/
    format/kickoff, game ID, goals + cards summary. Advanced:
    everything simple has + the full stats table and the cricket
    target/need/balls-left/run-rate. `updated` appends the panel's
    Updated footer (a live panel's proof it is ticking)."""
    lines = [f"**{d.get('home', '?')} {_score_str(d)} {d.get('away', '?')}**"]
    if d.get("league"):
        lines.append(str(d["league"]))
    lines.append("")
    lines.append(_clock_str(d))
    if d.get("sport"):
        lines.append(f"Sport: {d['sport']}")
    if d.get("venue"):
        lines.append(f"Venue: {d['venue']}")
    if d.get("format"):
        lines.append(f"Format: {d['format']}")
    if d.get("weather"):
        lines.append(f"Weather: {d['weather']}")
    kickoff = format_kickoff(d.get("start_ts"))
    if kickoff:
        lines.append(f"Kickoff: {kickoff}")
    gid = str(d.get("onex_id") or d.get("id") or "?")
    const = d.get("const_id")
    lines.append(f"Game ID: {gid}" + (f" · {const}" if const else ""))
    if d.get("video"):
        lines.append("Video: Live stream available")
    if d.get("has_lineups"):
        lines.append("Line-ups: Available")
    stats = d.get("stats")
    yellow = _stat_value(stats, "Yellow cards")
    red = _stat_value(stats, "Red card")
    if yellow or red:
        cards = []
        if yellow:
            cards.append(f"🟨 {yellow[0]}–{yellow[1]}")
        if red:
            cards.append(f"🟥 {red[0]}–{red[1]}")
        lines.append("Cards: " + "  ".join(cards))
    if level == DETAIL_ADVANCED:
        cricket = d.get("cricket") or {}
        if cricket.get("target") is not None:
            lines.append(f"Target: {cricket['target']}")
            need = cricket.get("need")
            balls = cricket.get("balls_left")
            if need is not None:
                lines.append(
                    f"Need: {need} off {balls} balls" if balls is not None
                    else f"Need: {need} runs")
        if cricket.get("run_rate"):
            lines.append(f"Run rate: {cricket['run_rate']}")
        card_lines = format_cricket_scorecard(d, full=True)
        if card_lines:
            lines.append("")
            lines.extend(card_lines)
        table = format_stats_table(stats)
        if table:
            lines.append("")
            lines.append("**Match stats**")
            lines.extend(table)
    return "\n".join(lines) + _updated_footer(updated)


# Preferred stat order per sport (Feature 4); any stat the
# feed sends that is not listed for its sport is appended
# after these, in the order it arrived. The orders below come
# from a real live-feed snapshot (18 Nov 2026): football
# carries Possession/Shots/Corners/Cards/Attacks/xG/Saves/...,
# basketball 2-/3-pointers and free throws, tennis serve and
# break stats, volleyball serve points, ice hockey powerplay
# stats. Cricket carries NO tabloStats at all — its rows come
# from the cricket score data instead (see
# format_cricket_stats). "Fouls" is not in football's stats,
# so it never appears for football — nothing is invented.
_ANALYTICS_ORDERS = {
    "football": (
        "Possession %", "Shots on target", "Shots off target",
        "Corner", "Yellow cards", "Red card", "Attacks",
        "Dangerous attacks", "xG", "Saves", "Penalty",
        "Substitutions", "Key Passes", "Passing Accuracy %",
        "Crosses",
    ),
    "basketball": (
        "2-pointers", "3-pointers", "Free throws",
        "Free throws scored %", "Fouls", "Timeouts remaining",
    ),
    "tennis": (
        "Aces", "Double faults", "1st Serve %",
        "Win % 1st Serve", "Win % 2nd Serve", "Breaks",
        "Break point %", "Points on own serve",
    ),
    "volleyball": (
        "Points on own serve", "Points on opponent's serve",
        "Max pts in a row", "Timeouts",
    ),
    "ice hockey": (
        "Possession %", "Attacks", "Goals during powerplay",
        "Penalties", "2-min penalties",
    ),
}
_ANALYTICS_LABELS = {"Possession %": "Possession", "Corner": "Corners"}


def _analytics_order_for(sport: str | None) -> tuple[str, ...]:
    """The preferred stat order for this sport, from the feed's
    own sport name. An unknown sport gets no preferred order —
    its stats render in the order the feed sent them."""
    return _ANALYTICS_ORDERS.get(str(sport or "").strip().lower(), ())

# One stat row's split bar: 10 blocks, home share filled.
# Discord does not render markdown tables, so the old pipe
# table showed up as raw text. The first bar layout (one long
# line per stat: label + values + bar) wrapped mid-row on
# narrow phone screens, splitting the numbers from their bar —
# so each stat now gets TWO short lines: the label, then home
# value, bar, away value.
_STAT_BAR_LEN = 10
_STAT_BAR_FULL = "█"
_STAT_BAR_EMPTY = "░"


def _stat_bar(home, away) -> str | None:
    """10-block split bar for one stat, or None when either side
    is not a number (the row then renders without a bar). The
    numbers beside the bar stay the source of truth."""
    try:
        h = float(str(home).rstrip("%"))
        a = float(str(away).rstrip("%"))
    except (TypeError, ValueError):
        return None
    total = h + a
    filled = 0 if total <= 0 else round(h / total * _STAT_BAR_LEN)
    filled = max(0, min(_STAT_BAR_LEN, filled))
    return (_STAT_BAR_FULL * filled
            + _STAT_BAR_EMPTY * (_STAT_BAR_LEN - filled))


def _stat_display_value(name: str, value) -> str:
    """Values for %-stats carry the % the stat name declares
    (the feed sends bare numbers); every other value is shown
    exactly as the feed sent it — nothing is invented. The %
    can sit anywhere in the name ("Win % 1st Serve"), not just
    at the end, so any % in the name counts."""
    value = str(value)
    if "%" in name and not value.endswith("%"):
        return value + "%"
    return value


def _short_team(name: str, width: int = 15) -> str:
    """A team name shortened to fit one side of the block's
    header row: cut with … when it would overflow."""
    name = str(name or "").strip()
    return name if len(name) <= width else name[:width - 1] + "…"


def format_analytics_stats(stats: list[dict] | None,
                           home: str = "", away: str = "") -> list[str]:
    """The analytics stat block, in display order: a team header
    row INSIDE the monospace block (home team left, away team
    right, shortened so the row stays 32 chars), then TWO lines
    per stat — the label on its own line, then home value, a
    10-block split bar, away value on the next line. The header
    above the block names the sides in full, but on a narrow
    phone screen the reader never has to look outside the box:
    the columns are labelled with the actual teams right where
    the numbers are. One long line per stat wrapped mid-row on
    narrow screens, so the home value landed on the label line
    and the bar + away value dropped below it — two short lines
    cannot wrap into nonsense: every value line is ~22
    characters. Stats the feed sends that are not numbers get a
    plain value line with no bar. Returns [] when there are no
    stats."""
    rows = []
    for s in stats or []:
        name = str(s.get("name", ""))
        label = _ANALYTICS_LABELS.get(name, name)
        if label.endswith(" %"):
            label = label[:-2]
        home_v = _stat_display_value(name, s.get("home", ""))
        away_v = _stat_display_value(name, s.get("away", ""))
        bar = _stat_bar(s.get("home", ""), s.get("away", ""))
        rows.append((label, home_v, away_v, bar))
    if not rows:
        return []
    lines = ["```"]
    lines.append(f"{_short_team(home):<15}  {_short_team(away):>15}")
    for label, home_v, away_v, bar in rows:
        lines.append(label)
        if bar is None:
            lines.append(f"{home_v} — {away_v}")
        else:
            lines.append(f"{home_v:>4}  {bar}  {away_v:<4}".rstrip())
    lines.append("```")
    return lines


def _cricket_wickets(score: object) -> str | None:
    """Wickets fallen out of a wicketScore string ("222/9" -> "9").
    None when the score carries no wickets (not started yet)."""
    try:
        parts = str(score).split("/")
        return parts[1] if len(parts) == 2 and parts[1].isdigit() else None
    except (TypeError, ValueError):
        return None


def format_cricket_stats(d: dict) -> list[str]:
    """The analytics stat block for a cricket game, in the same
    two-line shape as the other sports: a team header row inside
    the block, then Score (runs/wickets per side), Wickets
    fallen, Overs and Run rate. Cricket carries NO tabloStats in
    the feed — these rows come from the cricket score data the
    bot already has (wicketScore, overStr, run rate). Overs and
    run rate describe the innings in progress only, so they
    render as single-value lines rather than a home/away pair.
    Returns [] when the game carries no cricket data at all."""
    cricket = d.get("cricket") or {}
    home_score = d.get("home_score")
    away_score = d.get("away_score")
    rows: list[tuple[str, str, str | None, str | None]] = []
    if home_score is not None and away_score is not None:
        rows.append(("Score", str(home_score), str(away_score), None))
        hw, aw = _cricket_wickets(home_score), _cricket_wickets(away_score)
        if hw is not None and aw is not None:
            rows.append(("Wickets", hw, aw,
                         _stat_bar(hw, aw)))
    over = cricket.get("over_str")
    if over is not None:
        rows.append(("Overs", f"{over} ov", None, None))
    run_rate = cricket.get("run_rate")
    if run_rate:
        rows.append(("Run rate", str(run_rate), None, None))
    if not rows:
        return []
    home, away = str(d.get("home", "")), str(d.get("away", ""))
    lines = ["```"]
    lines.append(f"{_short_team(home):<15}  {_short_team(away):>15}")
    for label, home_v, away_v, bar in rows:
        lines.append(label)
        if away_v is None:
            lines.append(home_v)
        elif bar is None:
            lines.append(f"{home_v} — {away_v}")
        else:
            lines.append(f"{home_v:>4}  {bar}  {away_v:<4}".rstrip())
    lines.append("```")
    return lines


def format_cricket_scorecard(d: dict, full: bool = False) -> list[str]:
    """Advanced-only cricket scorecard: striker, non-striker,
    current bowler, partnership, last wicket, current run rate.
    full=True adds the current-innings batting + bowling card.
    NPL only — the 1xBet feed carries no player names, so other
    cricket has no scorecard and this returns []. Nothing is
    invented: a missing name or figure simply drops its line."""
    card = (d.get("cricket") or {}).get("scorecard") or {}
    if not card:
        return []
    lines = ["**🏏 At the crease**"]
    striker = card.get("striker") or {}
    if striker.get("name"):
        lines.append(
            f"Striker: {striker['name']} {striker.get('runs', '')}"
            f" ({striker.get('balls', '')})"
            + (f" · {striker.get('fours', '')}x4"
               f" {striker.get('sixes', '')}x6"
               if striker.get("fours") not in (None, "")
               else ""))
    non = card.get("non_striker") or {}
    if non.get("name"):
        lines.append(
            f"Non-striker: {non['name']} {non.get('runs', '')}"
            f" ({non.get('balls', '')})")
    bowler = card.get("bowler") or {}
    if bowler.get("name"):
        lines.append(
            f"Bowler: {bowler['name']} {bowler.get('overs', '')}"
            f"-{bowler.get('maidens', '')}-{bowler.get('runs', '')}"
            f"-{bowler.get('wickets', '')}")
    bits = []
    if card.get("partnership"):
        bits.append(f"Partnership: {card['partnership']}")
    if card.get("crr"):
        bits.append(f"CRR: {card['crr']}")
    if bits:
        lines.append(" · ".join(bits))
    lw = card.get("last_wicket") or {}
    if lw.get("name"):
        lines.append(
            f"Last wicket: {lw['name']} {lw.get('runs', '')}"
            + (f" ({lw['dismissal']})" if lw.get("dismissal") else "")
            + (f" · {lw['fow']}/{lw['over']}"
               if lw.get("fow") and lw.get("over") else ""))
    if card.get("last_over"):
        lines.append(f"Last over: {card['last_over']}")
    if not full:
        return lines
    batting = card.get("batting") or []
    if batting:
        lines.append("")
        lines.append("**Batting**")
        for b in batting:
            if not b.get("name"):
                continue
            mark = "*" if b.get("on_strike") is True else ""
            dis = b.get("dismissal") or ""
            lines.append(
                f"{b['name']}{mark} {b.get('runs', '')}"
                f" ({b.get('balls', '')})"
                + (f" · {dis}" if dis and dis != "not out" else
                   " · not out" if dis == "not out" else ""))
    bowling = card.get("bowling") or []
    if bowling:
        lines.append("")
        lines.append("**Bowling**")
        for b in bowling:
            if not b.get("name"):
                continue
            lines.append(
                f"{b['name']} {b.get('overs', '')}"
                f"-{b.get('maidens', '')}-{b.get('runs', '')}"
                f"-{b.get('wickets', '')}"
                + (f" · Econ {b['econ']}" if b.get("econ") else ""))
    return lines


def render_analytics(d: dict, momentum: str | None = None,
                     updated: str | None = None) -> str:
    """Advanced analytics view: home/away comparison from the
    stats the game actually carries, in that sport's own order —
    football gets its Possession/Shots/Corners table, basketball
    its shooting stats, tennis its serve stats, volleyball its
    serve points, ice hockey its powerplay stats — plus cricket
    target/need when present and a momentum estimate (football
    only) when one was computed from real snapshots. Cricket
    carries no stats table in the feed, so it gets its own
    block: Score, Wickets, Overs, Run rate, from the cricket
    score data. A game with no stat data at all says so plainly
    instead of showing an empty table. Ends with the Game ID
    line so the panel buttons on this message keep resolving."""
    lines = [f"**{d.get('home', '?')} {_score_str(d)} {d.get('away', '?')}**"]
    if d.get("league"):
        lines.append(str(d["league"]))
    lines.append("")
    lines.append(_clock_str(d))
    sport = str(d.get("sport") or "").strip().lower()
    stats = d.get("stats") or []
    by_name = {str(s.get("name")): s for s in stats}
    order = _analytics_order_for(d.get("sport"))
    ordered = [n for n in order if n in by_name]
    ordered += [n for n in by_name if n not in order]
    home, away = str(d.get("home", "?")), str(d.get("away", "?"))
    if sport == "cricket":
        block = format_cricket_stats(d)
        if not block and ordered:
            block = format_analytics_stats(
                [by_name[n] for n in ordered], home, away)
    else:
        block = (format_analytics_stats(
            [by_name[n] for n in ordered], home, away)
            if ordered else [])
    if block:
        lines.append("")
        lines.append("**📊 Match statistics**")
        # Name the sides once, right above the block: the left
        # number on every value line is the home team, the right
        # one the away team. The block also carries a team header
        # row inside it, so the columns stay labelled with the
        # actual teams even on a narrow screen.
        lines.append(f"Home: {home} · Away: {away}")
        lines.extend(block)
    else:
        # No stats table for this game in the live feed — that
        # is normal for lower leagues, not a failure: stats
        # come from the live list's tabloStats, and this game
        # simply carries none. But when the details feed
        # (gameInfo) is ALSO not answering — info_failed, set
        # by onex_game_detail — say so plainly instead of
        # leaving the user to guess why the card is thin.
        lines.append("")
        if d.get("info_failed"):
            lines.append("No match statistics available for this game.")
            lines.append("Match details are unavailable right now — "
                         "the details feed is not answering.")
        else:
            lines.append("No match statistics available yet.")
    card_lines = format_cricket_scorecard(d)
    if card_lines:
        lines.append("")
        lines.extend(card_lines)
    cricket = d.get("cricket") or {}
    if cricket.get("target") is not None:
        lines.append(f"Target: {cricket['target']}")
        need = cricket.get("need")
        balls = cricket.get("balls_left")
        if need is not None:
            lines.append(f"Need: {need} off {balls} balls" if balls is not None
                         else f"Need: {need} runs")
    # The momentum estimate comes from football stat snapshots
    # only — it never applies to cricket or any other sport.
    if momentum and sport == "football":
        lines.append("")
        lines.append(momentum)
    gid = str(d.get("onex_id") or d.get("id") or "?")
    const = d.get("const_id")
    lines.append(f"Game ID: {gid}" + (f" · {const}" if const else ""))
    return "\n".join(lines) + _updated_footer(updated)


def render_match_list(games: list[dict]) -> str:
    """Numbered pick list when a name matches several games."""
    lines = ["Found more than one game — pick one by its ID:\n"]
    for i, g in enumerate(games, 1):
        clock = _clock_str(g)
        lines.append(
            f"{i}. **{g.get('home', '?')} {_score_str(g)} {g.get('away', '?')}**"
            f" · {clock} · {g.get('league', '')} · ID `{g.get('onex_id')}`")
    lines.append("\nUse `/game <ID>` for the full detail, or `/watch <ID>`.")
    return "\n".join(lines)


def render_top_games(games: list[dict],
                     header: str = "⚽ **Live games right now — pick one:**",
                     limit: int = 10, budget: int = 1750) -> str:
    """Top live games as a pick list — when the user asks to see
    games, or as suggestions after a search found nothing.
    Same line format as render_match_list (score, clock, league,
    game ID); pass header="" when the caller's own sentence is
    already the header. Capped at `limit` lines AND a character
    budget, with an '…and N more' tail — a 10-line list of long
    team/league names can pass Discord's 2000-character message
    limit on its own, and the send then raises and the whole
    answer is lost (the same failure /ask hit on 18 Nov 2026)."""
    lines = ([header] if header else [])
    shown = list(games)[:limit]
    total = sum(len(line) + 1 for line in lines)
    kept = []
    for i, g in enumerate(shown, 1):
        clock = _clock_str(g)
        line = (
            f"{i}. **{g.get('home', '?')} {_score_str(g)} {g.get('away', '?')}**"
            f" · {clock} · {g.get('league', '')} · ID `{g.get('onex_id')}`")
        if kept and total + 1 + len(line) > budget:
            break
        kept.append(line)
        total += len(line) + 1
    lines.extend(kept)
    rest = len(games) - len(kept)
    if rest > 0:
        lines.append(f"…and {rest} more live games.")
    lines.append("\nUse `/game <ID>` for the full detail, or `/watch <ID>` to follow one.")
    return "\n".join(lines)


async def top_live_games(limit: int = 5,
                         sport: str | None = None) -> list[dict]:
    """Top live games with score + clock, for pick lists and
    suggestions. Never raises — a failed fetch is an empty list.
    With a sport, only that sport's games."""
    try:
        return await prov.onex_top_live(limit, sport=sport)
    except Exception as exc:
        log.warning("[watch] top live games failed (%s)", type(exc).__name__)
        return []


# ══════════════════════════════════════════════════════════════════════════
# SNAPSHOT + CHANGE DETECTION (pure)
# ══════════════════════════════════════════════════════════════════════════

def _wickets(score: object) -> int | None:
    if score is None:
        return None
    parts = str(score).split("/")
    if len(parts) != 2:
        return 0 if str(score).strip() != "" else None
    try:
        return int(parts[1])
    except ValueError:
        return None


def snapshot_from_detail(d: dict) -> dict:
    stats = {}
    for s in d.get("stats") or []:
        stats[str(s.get("name"))] = (str(s.get("home", "")),
                                     str(s.get("away", "")))
    cricket = d.get("cricket") or {}
    clock = str(d.get("clock") or "")
    timer_sec = d.get("timer_sec")
    if not clock and isinstance(timer_sec, (int, float)) \
            and str(d.get("status") or "") in ("1H", "2H", "ET", "PEN"):
        # Football detail with a timer but no clock string — the
        # event lines need MM:SS, not a bare minute.
        clock = prov.format_mmss(timer_sec) or ""
    return {
        "home_score": "" if d.get("home_score") is None else str(d["home_score"]),
        "away_score": "" if d.get("away_score") is None else str(d["away_score"]),
        "status": str(d.get("status") or ""),
        "minute": d.get("minute") if isinstance(d.get("minute"), int) else None,
        "clock": clock,
        "timer_sec": timer_sec if isinstance(timer_sec, (int, float)) else None,
        "is_break": bool(cricket.get("is_break")),
        "stats": stats,
        "cricket": {
            "team": cricket.get("team"),
            "over_str": cricket.get("over_str"),
            "target": cricket.get("target"),
            "need": cricket.get("need"),
            "balls_left": cricket.get("balls_left"),
            "run_rate": cricket.get("run_rate"),
        },
    }


def _score_runs(score: object) -> int | None:
    """Numeric score: football "2" -> 2, cricket "120/2" -> 120
    (runs). None when there is no score yet."""
    if score is None:
        return None
    text = str(score).strip()
    if not text or text in ("?", "–"):
        return None
    try:
        return int(text.split("/")[0])
    except ValueError:
        return None


def detect_events(old: dict, new: dict, level: str = DETAIL_SIMPLE,
                  game_id: str = "") -> list[dict]:
    """Event dicts for what changed between two snapshots:
    {"kind", "key", "text"}. key is stable across restarts
    (game + kind + score + clock/counts) so the history table
    can dedup it — a restart never re-sends an old event.
    Nothing changed -> []. A cricket score that only moved in
    runs (no wicket) is NOT an event — it would spam every ball.
    A score that went DOWN is a correction, never a goal."""
    events: list[dict] = []
    title = f"{new.get('_home', '?')} {new['home_score']}–{new['away_score']} {new.get('_away', '?')}"
    clock = new.get("clock") or ""
    suffix = f" ({clock})" if clock else ""

    def _key(kind: str, extra: str = "") -> str:
        base = f"{game_id}|{kind}|{new.get('home_score')}–{new.get('away_score')}|{clock}"
        return base + (f"|{extra}" if extra else "")

    def _add(kind: str, text: str, extra: str = "") -> None:
        events.append({"kind": kind, "key": _key(kind, extra), "text": text})

    old_status, new_status = old.get("status", ""), new.get("status", "")
    if old_status in ("upcoming", "", "?") and new_status == "live":
        _add("kickoff", f"▶ **Kickoff** — {title}{suffix}")
    if new_status == "HT" and old_status != "HT":
        _add("ht", f"⏸ **Half-time** — {title}{suffix}")
    if old_status == "HT" and new_status == "2H":
        _add("second_half", f"▶ **Second-half start** — {title}{suffix}")
    if new_status == "FT" and old_status != "FT":
        _add("ft", f"⏱ **Full-time** — {title}")
        return events
    terminal = TERMINAL_STATUSES.get(str(new_status).strip().lower())
    if terminal and old_status != new_status:
        kind, icon, label = terminal
        _add(kind, f"{icon} **{label}** — {title}")
        return events
    if new.get("is_break") and not old.get("is_break"):
        _add("break", f"☕ **Innings break** — {title}{suffix}")
    if old.get("home_score") != new.get("home_score") or old.get("away_score") != new.get("away_score"):
        # First sighting ("" -> "0") is kickoff, not a goal.
        first = (old.get("home_score") in (None, "")
                 and old.get("away_score") in (None, ""))
        if first:
            return events
        old_h, old_a = _score_runs(old.get("home_score")), _score_runs(old.get("away_score"))
        new_h, new_a = _score_runs(new.get("home_score")), _score_runs(new.get("away_score"))
        if ((old_h is not None and new_h is not None and new_h < old_h)
                or (old_a is not None and new_a is not None and new_a < old_a)):
            # Score went DOWN: a correction (reversed goal), not a goal.
            _add("correction",
                 f"✏️ **Score corrected** — was {old.get('home_score')}–{old.get('away_score')}, "
                 f"now {title}{suffix}")
        else:
            old_wk = _wickets(old.get("home_score"))
            new_wk = _wickets(new.get("home_score"))
            old_wk2 = _wickets(old.get("away_score"))
            new_wk2 = _wickets(new.get("away_score"))
            if ((old_wk is not None and new_wk is not None and new_wk > old_wk)
                    or (old_wk2 is not None and new_wk2 is not None and new_wk2 > old_wk2)):
                _add("wicket", f"🏏 **WICKET** — {title}{suffix}")
            elif new.get("status") not in ("upcoming", "", "?"):
                # Football score change is a goal; cricket runs alone
                # (no wicket) are skipped above by the wicket check —
                # but a football score has no "/" so _wickets returns 0
                # for both sides and this branch fires correctly.
                if "/" not in (new.get("home_score") or "") and "/" not in (new.get("away_score") or ""):
                    _add("goal", f"⚽ **GOAL** — {title}{suffix}")
    old_stats, new_stats = old.get("stats") or {}, new.get("stats") or {}
    for label, icon, kind in (("Yellow cards", "🟨", "yellow"),
                              ("Red card", "🟥", "red")):
        o = old_stats.get(label)
        n = new_stats.get(label)
        if o is None or n is None:
            continue
        try:
            oh, oa = int(o[0] or 0), int(o[1] or 0)
            nh, na = int(n[0] or 0), int(n[1] or 0)
        except ValueError:
            continue
        if nh > oh:
            _add(kind, f"{icon} **{'Yellow card' if kind == 'yellow' else 'RED CARD'}** — {title}{suffix} (home {nh}, was {oh})", f"home:{nh}")
        if na > oa:
            _add(kind, f"{icon} **{'Yellow card' if kind == 'yellow' else 'RED CARD'}** — {title}{suffix} (away {na}, was {oa})", f"away:{na}")
    # Penalty awarded: a major event at both levels (a scored
    # penalty also fires its own GOAL line above; a missed one
    # only shows here). Substitutions stay advanced-only — simple
    # watches chose minimal alerts.
    o = old_stats.get("Penalty")
    n = new_stats.get("Penalty")
    if o is not None and n is not None:
        try:
            oh, oa = int(o[0] or 0), int(o[1] or 0)
            nh, na = int(n[0] or 0), int(n[1] or 0)
        except ValueError:
            oh = oa = nh = na = 0
        if nh > oh or na > oa:
            _add("penalty", f"🎯 **Penalty** — {title}{suffix} (now {nh}–{na}, was {oh}–{oa})", f"{nh}–{na}")
    if level == DETAIL_ADVANCED:
        o = old_stats.get("Substitutions")
        n = new_stats.get("Substitutions")
        if o is not None and n is not None:
            try:
                oh, oa = int(o[0] or 0), int(o[1] or 0)
                nh, na = int(n[0] or 0), int(n[1] or 0)
            except ValueError:
                oh = oa = nh = na = 0
            if nh > oh or na > oa:
                _add("sub", f"🔄 **Substitutions** — {title}{suffix} (now {nh}–{na}, was {oh}–{oa})", f"{nh}–{na}")
    return events


def detect_changes(old: dict, new: dict, level: str = DETAIL_SIMPLE) -> list[str]:
    """Event lines for what changed between two snapshots.
    Nothing changed -> []. Kept as the plain-text wrapper around
    detect_events (same texts, same order) for callers that only
    need the lines."""
    return [e["text"] for e in detect_events(old, new, level)]


# ══════════════════════════════════════════════════════════════════════════
# AI RESOLUTION
# ══════════════════════════════════════════════════════════════════════════

_AI_PROMPT = """You resolve a Discord user's request about a live sports game to ONE game from the list below.
Reply with STRICT JSON only — no other text, no markdown, no explanation:
{"game_id": "<id from the list, or null>", "action": "follow|unfollow|detail|list", "detail": "simple|advanced", "channel_id": "<id from the channels list, or null>", "sport": "<sport name or null>", "note": "<one short line>"}
Rules:
- game_id MUST be one of the ids in the games list. If no game matches the request, use null.
- A bare base name never means a reserve team: "Barcelona" means FC Barcelona only — never "Barcelona II", "Barcelona B" or "Barça Atlètic". A reserve team is only a match when the user names its suffix ("Barcelona II").
- action is "follow" when the user wants alerts/updates for a game, "unfollow" when they want to stop following it, "detail" when they only want to see it now.
- action is "list" when the user wants to browse/see the live games rather than follow one specific game (e.g. "show me a game", "what's live", "any game to test") — then game_id is null.
- sport is the sport the user names when they want to browse games of one sport (e.g. "show me football games" -> "Football", "any cricket live?" -> "Cricket") — use only a sport name from the games list, or null when the user names none. Never invent a sport name.
- detail is "advanced" only when the user asks for full/advanced/detailed stats; otherwise "simple".
- channel_id MUST be one of the ids in the channels list, or null when the user does not name a channel. If the user names a channel that is not in the list, use null.
- note is one short line saying what you understood, e.g. "Lumbini vs Sudurpaschim, NPL, alerts to #general".

Games (id | home vs away | league | sport | status):
__GAMES__

Channels (id | #name):
__CHANNELS__

User request: "__TEXT__"
"""


async def parse_watch_ai(text: str, grounding: list[dict],
                         model, channels: list[dict] | None = None) -> dict | None:
    """Ask the AI model (Groq first, Gemini fallback) to resolve
    free text to a game (and, when the user names one, a
    channel). model is the configured AI model (or None).
    Returns the validated dict or None — a game_id outside the
    grounding list, or a channel_id outside the channel list,
    is rejected."""
    if model is None:
        return None
    games = "\n".join(
        f"{g['id']} | {g['home']} vs {g['away']} | {g['league']} | "
        f"{g.get('sport') or '?'} | {g['status']}"
        for g in grounding)
    if channels:
        chans = "\n".join(
            f"{c['id']} | #{c.get('name', '')}" for c in channels)
    else:
        chans = "(none — use null)"
    prompt = (_AI_PROMPT.replace("__GAMES__", games)
              .replace("__CHANNELS__", chans).replace("__TEXT__", text))
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(model.generate_content, prompt),
            timeout=AI_TIMEOUT_S)
        raw = (getattr(response, "text", "") or "").strip()
    except asyncio.TimeoutError:
        log.warning("[watch] AI resolve timed out (%ss) — using candidates",
                    AI_TIMEOUT_S)
        return None
    except Exception as exc:
        log.warning("[watch] AI resolve failed (%s)", type(exc).__name__)
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    gid = data.get("game_id")
    if gid is not None:
        gid = str(gid)
        if gid not in {str(g["id"]) for g in grounding}:
            log.warning("[watch] AI returned an id outside the grounding list")
            return None
    action = data.get("action")
    if action not in ("follow", "unfollow", "detail", "list"):
        action = "follow"
    detail = data.get("detail")
    if detail not in (DETAIL_SIMPLE, DETAIL_ADVANCED):
        detail = DETAIL_SIMPLE
    cid = data.get("channel_id")
    channel_id = None
    if cid is not None:
        cid = str(cid)
        if cid in {str(c["id"]) for c in (channels or [])}:
            channel_id = cid
        else:
            log.warning("[watch] AI returned a channel id "
                        "outside the channel list")
    return {"game_id": gid, "action": action, "detail": detail,
            "channel_id": channel_id,
            "sport": _canonical_sport(data.get("sport")),
            "note": str(data.get("note") or "")}


def _norm(s: str) -> str:
    return prov.normalize(s or "")


def _match_entries(text: str, grounding: list[dict]) -> list[dict]:
    """Grounding entries whose home/away/league contains the text.
    A bare base name never matches a reserve side ("Barcelona"
    does not match "Barcelona II") — see normalizer."""
    needle = _norm(text)
    if not needle:
        return []
    out = []
    for g in grounding:
        if (team_name_matches(text, g["home"])
                or team_name_matches(text, g["away"])
                or needle in _norm(g["league"])):
            out.append(g)
    return out


def is_barca_query(text: str) -> bool:
    """Free text asking for the main Barcelona team — the word
    "barcelona"/"barca" with no reserve suffix named."""
    from normalizer import canonical_team_name
    return ("barcelona" in canonical_team_name(text).split()
            and reserve_suffix(text) is None)


def _is_barca_reserve_game(entry: dict) -> bool:
    """A game involving a Barcelona reserve side."""
    for side in (entry.get("home", ""), entry.get("away", "")):
        if (is_reserve_team(side)
                and base_team_name(side) == "barcelona"):
            return True
    return False


def _is_reserve_mismatch(text: str, entry: dict) -> bool:
    """True when a resolved game is a reserve side but the user's
    words name only the base team — e.g. the AI picking
    "Barcelona II" for "Barcelona". (18 Nov 2026: that exact
    pick posted a Barcelona II panel in #general.)"""
    from normalizer import canonical_team_name
    q = canonical_team_name(text)
    for side in (entry.get("home", ""), entry.get("away", "")):
        if is_reserve_team(side):
            base = base_team_name(side)
            if (base and base in q
                    and reserve_suffix(q) != reserve_suffix(side)):
                return True
    return False


# Words that pick the detail level without the AI — checked
# before the model call, so "advanced stats" never waits on a model call.
_ADVANCED_WORDS = ("advanced", "detailed", "full stats", "all stats",
                   "everything", "full detail", "detailed stats")
_SIMPLE_WORDS = ("simple", "basic", "just goals", "only goals",
                 "just the goals", "goals only")


def _detail_from_words(text: str) -> str | None:
    """Detail level the user's own words ask for, or None."""
    n = _norm(text or "")
    if any(w in n for w in _ADVANCED_WORDS):
        return DETAIL_ADVANCED
    if any(w in n for w in _SIMPLE_WORDS):
        return DETAIL_SIMPLE
    return None


# User words -> the sport name the live data uses. Multi-word
# phrases first (longest first) so "football" never steals
# "american football", and "hockey" means ice hockey — the data
# carries no field hockey. Sports the data never carries
# (baseball, rugby, American football as a real sport) still
# map — the filter then honestly finds nothing live instead of
# guessing a different sport.
_SPORT_WORDS = (
    ("american football", "American Football"),
    ("beach volleyball", "Beach Volleyball"),
    ("basketball 3x3", "Basketball 3x3"),
    ("ice hockey", "Ice Hockey"),
    ("table tennis", "Table Tennis"),
    ("field hockey", "Field Hockey"),
    ("ping pong", "Table Tennis"),
    ("football", "Football"),
    ("soccer", "Football"),
    ("cricket", "Cricket"),
    ("basketball", "Basketball"),
    ("hockey", "Ice Hockey"),
    ("tennis", "Tennis"),
    ("volleyball", "Volleyball"),
    ("handball", "Handball"),
    ("badminton", "Badminton"),
    ("baseball", "Baseball"),
    ("rugby", "Rugby"),
    ("futsal", "Futsal"),
    ("golf", "Golf"),
    ("boxing", "Boxing"),
    ("snooker", "Snooker"),
    ("darts", "Darts"),
    ("floorball", "Floorball"),
    ("billiards", "Billiards"),
    ("athletics", "Athletics"),
    ("fifa", "FIFA"),
    ("esports", "Esports"),
)

_SPORT_EMOJI = {
    "Football": "⚽", "Cricket": "🏏", "Basketball": "🏀",
    "Tennis": "🎾", "Ice Hockey": "🏒", "Volleyball": "🏐",
    "Beach Volleyball": "🏐", "Table Tennis": "🏓",
    "Badminton": "🏸", "Handball": "🤾", "Baseball": "⚾",
    "Rugby": "🏉", "American Football": "🏈", "Golf": "⛳",
    "Boxing": "🥊", "Snooker": "🎱", "Billiards": "🎱",
    "Futsal": "⚽", "Athletics": "🏃", "Esports": "🎮",
    "Basketball 3x3": "🏀", "Field Hockey": "🏑", "FIFA": "🎮",
    "Darts": "🎯", "Floorball": "🏑",
}


def _sport_from_words(text: str) -> str | None:
    """The sport the user's words name, in the live data's own
    words — or None. Whole-word match, so "football" never
    matches inside "American football" (checked first) or a
    team name."""
    n = _norm(text or "")
    if not n:
        return None
    for phrase, sport in _SPORT_WORDS:
        if re.search(r"\b" + re.escape(phrase) + r"\b", n):
            return sport
    return None


def _canonical_sport(name: object) -> str | None:
    """An AI-returned sport name -> the canonical name the live
    data uses, or None when it is not a sport we know (never
    invented — an unknown sport means "no filter")."""
    if not isinstance(name, str) or not name.strip():
        return None
    n = _norm(name)
    for phrase, sport in _SPORT_WORDS:
        if n == _norm(phrase) or n == _norm(sport):
            return sport
    return None


def sport_list_header(sport: str | None) -> str:
    """Pick-list header: "⚽ Live football right now — pick one:"
    for a sport, the plain header when none."""
    if not sport:
        return "⚽ **Live games right now — pick one:**"
    return (f"{_SPORT_EMOJI.get(sport, '⚽')} **Live "
            f"{sport.lower()} right now — pick one:**")


def no_live_sport_message(sport: str) -> str:
    """Plain answer when no game of that sport is live — no
    other sport is offered in its place."""
    return f"No {sport.lower()} games live right now."


_SPORT_QUERY_FILLER = {"live", "games", "game", "matches", "match",
                       "any", "show", "list", "me", "the", "a", "an",
                       "please", "right", "now", "on", "today", "tonight"}


def _is_sport_only_query(text: str, sport: str) -> bool:
    """The query is just the sport (plus filler like "live
    games") — e.g. /game "football" or "show any cricket game",
    not "football team X"."""
    n = _norm(text or "")
    if not n or not sport:
        return False
    for phrase, _name in _SPORT_WORDS:
        n = re.sub(r"\b" + re.escape(phrase) + r"\b", " ", n)
    return not [w for w in n.split() if w not in _SPORT_QUERY_FILLER]


def _channel_mentions(text: str) -> list[str]:
    """Channel names the user typed: #name tokens and
    "in/to/for <name> channel" phrases."""
    mentions: list[str] = []
    for m in re.finditer(r"#([\w-]+)", text or ""):
        mentions.append(m.group(1))
    for m in re.finditer(r"\b(?:in|to|for)\s+([\w-]+)\s+channel\b",
                         text or "", re.I):
        mentions.append(m.group(1))
    return mentions


def _channel_from_words(text: str,
                        channels: list[dict] | None) -> tuple[dict | None,
                                                              str | None]:
    """Deterministic channel match: a #name or "in <name> channel"
    that equals a real channel name (case-insensitive).
    Returns (channel|None, named|None) — named is the name the
    user typed when it matches NO channel, so the caller can say
    so instead of silently watching elsewhere."""
    mentions = _channel_mentions(text)
    named = mentions[0] if mentions else None
    for name in mentions:
        for ch in channels or []:
            if _norm(name) == _norm(str(ch.get("name", ""))):
                return ch, None
    return None, named


# Words that mean "show me the games" rather than "follow this
# one game" — checked when no game matched, so "show amy new
# game to test" (a typo for "any") still lists games instead of
# dead-ending. Runs AFTER the AI attempt, so a request naming
# a specific team ("show me the Lumbini game") still resolves
# to that game first.
_SHOW_WORDS = ("show", "list", "see", "browse", "display", "any",
               "whats", "what's", "what")
_GAME_WORDS = ("game", "games", "match", "matches", "live", "playing",
               "on now", "test")


def _wants_list(text: str) -> bool:
    """The user wants to browse games, not follow one. A show
    word plus a game word ("show any game") — or plus a sport
    word ("show me cricket", "show football") — means browse."""
    n = _norm(text or "")
    if not any(w in n for w in _SHOW_WORDS):
        return False
    return (any(w in n for w in _GAME_WORDS)
            or _sport_from_words(text) is not None)


async def resolve_watch(text: str, model, channels: list[dict] | None = None) -> dict:
    """Resolve free text to a game — and, when the words name
    one, to a channel and a detail level. Game: game ID first,
    then exact/unique team-name match, then the AI model on the
    grounding list. Channel: #name / "in <name> channel" first,
    then the AI model on the channel list. Detail: the words' own
    keywords first, then the AI model's parsed detail.
    Returns {"game_id"|None, "action", "detail"|None,
    "channel"|None, "channel_id"|None, "channel_named"|None,
    "candidates", "entry"|None, "source", "games", "suggestions",
    "sport"|None}.
    "games" holds the top live games when action is "list";
    "suggestions" holds them when nothing matched at all, so the
    caller can offer them instead of a bare dead end. "sport"
    is the sport the words name — list and suggestion games are
    filtered to it, and an empty filtered list means "no game of
    that sport is live", never a list of other sports. Never
    invents an id or a channel id. "detail" is None when the words
    say nothing about it — the /watch dropdown always wins, and a
    hardcoded "simple" here used to discard the user's Advanced choice."""
    q = (text or "").strip()
    words_detail = _detail_from_words(q)
    word_channel, channel_named = _channel_from_words(q, channels)

    def _out(game_id, action, detail, candidates, entry, source,
             channel=None, note="", games=None, suggestions=None,
             sport=None, barca_not_live=False, reserve_games=None):
        channel = channel if channel is not None else word_channel
        return {"game_id": game_id, "action": action, "detail": detail,
                "candidates": candidates, "entry": entry, "source": source,
                "note": note, "channel": channel,
                "channel_id": (str(channel["id"]) if channel else None),
                "channel_named": channel_named,
                "games": games or [], "suggestions": suggestions or [],
                "sport": sport, "barca_not_live": barca_not_live,
                "reserve_games": reserve_games or []}

    if not q:
        return _out(None, "follow", words_detail, [], None, "none")
    if q.isdigit() and len(q) >= 5:
        return _out(q, "follow", words_detail, [], None, "id")
    grounding = await prov.onex_grounding()
    # "Barcelona" means the main team, live now. An upcoming
    # fixture is not a match — the caller says the team is not
    # live (with its next fixture) instead — and the reserve team
    # is never a match (18 Nov 2026: Barcelona II panel in
    # #general). Other teams keep the old behaviour: an upcoming
    # fixture can still be followed.
    barca_query = is_barca_query(q)
    if barca_query:
        grounding = [g for g in grounding
                     if str(g.get("status") or "").lower()
                     in _BARCA_LIVE_STATUSES]
    exact = [g for g in grounding
             if _norm(g["home"]) == _norm(q) or _norm(g["away"]) == _norm(q)]
    if len(exact) == 1:
        return _out(str(exact[0]["id"]), "follow", words_detail, [],
                    exact[0], "name")
    matches = _match_entries(q, grounding)
    if len(matches) == 1:
        return _out(str(matches[0]["id"]), "follow", words_detail, [],
                    matches[0], "name")
    ai = await parse_watch_ai(q, grounding, model, channels)
    if ai and ai.get("game_id"):
        # The AI may still pick a reserve side from the list — a
        # bare base name never means it, so reject that pick and
        # fall through to the not-live answer instead.
        entry = next((g for g in grounding
                      if str(g["id"]) == str(ai["game_id"])), None)
        if entry is not None and _is_reserve_mismatch(q, entry):
            log.warning("[watch] AI picked a reserve team (%s vs %s) "
                        "for a bare base name — ignoring it",
                        entry.get("home"), entry.get("away"))
            ai = None
    if ai and ai.get("action") == "list":
        # The AI's sport wins; the words back it up when the
        # model did not name one — "show any football game"
        # must not fall back to mixed sports.
        sport = ai.get("sport") or _sport_from_words(q)
        top = await top_live_games(sport=sport)
        return _out(None, "list", words_detail, [], None, "ai",
                    games=top, note=ai.get("note", ""), sport=sport)
    if ai and ai.get("game_id"):
        entry = next((g for g in grounding
                      if str(g["id"]) == str(ai["game_id"])), None)
        ai_channel = None
        if word_channel is None and ai.get("channel_id"):
            ai_channel = next(
                (c for c in (channels or [])
                 if str(c["id"]) == str(ai["channel_id"])), None)
        detail = (words_detail if words_detail is not None
                  else ai["detail"])
        return _out(str(ai["game_id"]), ai["action"], detail, [],
                    entry, "ai", channel=ai_channel,
                    note=ai.get("note", ""))
    if _wants_list(q):
        sport = _sport_from_words(q)
        top = await top_live_games(sport=sport)
        return _out(None, "list", words_detail, [], None, "list",
                    games=top, sport=sport)
    sport = _sport_from_words(q)
    suggestions = await top_live_games(sport=sport)
    # A Barcelona query with no live main-team game: the caller
    # answers "not live" (with the next fixture) and offers the
    # live reserve game as a separate choice — it is never
    # silently picked.
    reserve_games = ([g for g in grounding if _is_barca_reserve_game(g)]
                     if barca_query else [])
    return _out(None, "follow", words_detail, matches[:5], None, "none",
                suggestions=suggestions, sport=sport,
                barca_not_live=barca_query,
                reserve_games=reserve_games)


# ══════════════════════════════════════════════════════════════════════════
# WATCH REGISTRY + TICK (shared with the scoreboard's 1s loop)
# ══════════════════════════════════════════════════════════════════════════

# (channel_id, game_id) -> {home, away, league, detail, fmt,
#                           snapshot, missing}
_watches: dict[tuple[int, str], dict] = {}
_loaded = False


# ══════════════════════════════════════════════════════════════════════════
# BARÇA AUTO-WATCH — rides the shared 1s tick, no second poller.
#
# When an FC Barcelona match goes live, start a SIMPLE watch for it
# in #general so goal alerts post there with no one typing /watch.
# Silent by design: nothing is sent before kickoff or outside the
# match — no "now watching" notice, no pre-match chatter. The
# first message is whatever the normal on-change alerts send
# (kickoff, then goals/cards/half-time/full-time) while the game
# is live. The 12h reminder in barcelona_reminder.py is untouched.
# ══════════════════════════════════════════════════════════════════════════

# One auto-watch check per tick is too much (the search also reads
# the fixture lists) — the 1s tick is cheap only when the check is
# throttled, and a match only goes live once.
_BARCA_CHECK_EVERY_S = 30.0
_barca_last_check = 0.0

# Statuses that mean "live right now" for a searched game.
# "live" is the line-feed word for a fixture past kickoff; FT and
# "?" / upcoming are never live.
_BARCA_LIVE_STATUSES = ("live", "1h", "2h", "et", "pen", "ht",
                        "half-time", "1st innings", "2nd innings")


def _barca_already_watched(channel_id: int) -> bool:
    """A Barcelona game already watched in this channel — manual
    or auto — so the auto-watch never double-starts one."""
    for (cid, _gid), w in _watches.items():
        if cid != channel_id:
            continue
        if barcelona_reminder._is_barcelona(
                {"home": w.get("home", ""), "away": w.get("away", "")}):
            return True
    return False


async def maybe_auto_watch_barca() -> dict | None:
    """Start a SIMPLE watch for a live Barcelona game in #general.
    The game id comes from onex_search("Barcelona") against the
    live list — never invented; if Barcelona is not live, start
    nothing and stay silent. Returns the started game or None.
    Never raises — a failed search just skips this check."""
    if await maintenance.is_on():
        return None
    global _barca_last_check
    channel_id = barcelona_reminder.GENERAL_CHANNEL_ID
    if not channel_id:
        return None
    if _barca_already_watched(channel_id):
        return None
    now = time.monotonic()
    if now - _barca_last_check < _BARCA_CHECK_EVERY_S:
        return None
    _barca_last_check = now
    try:
        result = await prov.onex_search("Barcelona")
    except Exception as exc:
        log.warning("[barca-watch] search failed (%s)", type(exc).__name__)
        return None
    live = [g for g in (result or {}).get("games") or []
            if str(g.get("status") or "").lower() in _BARCA_LIVE_STATUSES
            and barcelona_reminder._is_barcelona(g)]
    if not live:
        return None
    game = live[0]
    entry = {"id": str(game["onex_id"]), "home": game.get("home", ""),
             "away": game.get("away", ""), "league": game.get("league", ""),
             "status": game.get("status", "")}
    try:
        started = await start_watch(channel_id, entry, DETAIL_SIMPLE)
    except Exception as exc:
        log.warning("[barca-watch] start failed (%s)", type(exc).__name__)
        return None
    if not started:
        return None
    log.info("[barca-watch] auto-watch started %s vs %s (game %s) "
             "in channel %s", started["home"], started["away"],
             started["game_id"], channel_id)
    return started


async def _prefs_for(channel_id: int, game_id: str) -> tuple[set[str] | None, bool, str]:
    """Effective notification prefs for a watch: (allowed kinds
    or None for "all", paused, detail override or ""). A missing
    row or an empty events list means "everything, not paused".
    Never raises — a failed read means defaults."""
    try:
        row = await db.get_prefs(channel_id, game_id)
    except Exception as exc:
        log.warning("[watch] prefs read failed (%s)", type(exc).__name__)
        return None, False, ""
    if row is None:
        return None, False, ""
    events = row.get("events") or []
    allowed = set(events) if events else None
    return allowed, bool(row.get("paused")), str(row.get("detail") or "")


# Team follows (Feature 5): same throttled pattern as the Barça
# auto-watch — one check per 30s, reads the tick cache only.
_TEAM_CHECK_EVERY_S = 30.0
_team_last_check = 0.0


async def maybe_team_follow_check() -> None:
    """Start watches for live games involving a followed team.
    Silent: nothing is sent before kickoff or outside the match
    — the normal on-change alerts do the talking."""
    global _team_last_check
    now = time.monotonic()
    if now - _team_last_check < _TEAM_CHECK_EVERY_S:
        return
    _team_last_check = now
    try:
        follows = await db.get_all_team_follows()
    except Exception as exc:
        log.warning("[team-follow] read failed (%s)", type(exc).__name__)
        return
    for follow in follows:
        try:
            channel_id = int(follow["channel_id"])
            team_name = str(follow.get("team_name") or "")
            if not team_name or not channel_id:
                continue
            already = any(
                cid == channel_id
                and (team_name_matches(team_name, w.get("home", ""))
                     or team_name_matches(team_name, w.get("away", "")))
                for (cid, _gid), w in _watches.items())
            if already:
                continue
            result = await prov.onex_search(team_name)
            live = [g for g in (result or {}).get("games") or []
                    if str(g.get("status") or "").lower() in _BARCA_LIVE_STATUSES
                    and (team_name_matches(team_name, g.get("home", ""))
                         or team_name_matches(team_name, g.get("away", "")))]
            if not live:
                continue
            game = live[0]
            entry = {"id": str(game["onex_id"]), "home": game.get("home", ""),
                     "away": game.get("away", ""), "league": game.get("league", ""),
                     "status": game.get("status", "")}
            started = await start_watch(
                channel_id, entry, str(follow.get("detail") or DETAIL_SIMPLE))
            if started:
                log.info("[team-follow] auto-watch started %s vs %s "
                         "(game %s) in channel %s for team %s",
                         started["home"], started["away"],
                         started["game_id"], channel_id, team_name)
        except Exception as exc:
            log.warning("[team-follow] check failed (%s)", type(exc).__name__)


async def load_watches() -> None:
    """Load persisted watches into memory (called once at startup)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        rows = await db.get_all_watches()
    except Exception as exc:
        log.warning("[watch] could not load watches (%s)", type(exc).__name__)
        return
    for row in rows:
        try:
            snapshot = json.loads(row.get("snapshot") or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        panel_id = row.get("panel_message_id") or 0
        _watches[(row["channel_id"], str(row["game_id"]))] = {
            "home": row.get("home", ""), "away": row.get("away", ""),
            "league": row.get("league", ""), "detail": row.get("detail") or DETAIL_SIMPLE,
            "fmt": row.get("fmt") or "", "snapshot": snapshot, "missing": 0, "missing_since": 0.0,
            # notify / panel_kind default to the old behaviour for
            # rows written before the /game-panel change.
            "notify": bool(row.get("notify", 1)),
            "panel_kind": row.get("panel_kind") or "compact",
            "panel_message_id": int(panel_id) if panel_id else None,
            "panel_body": None, "panel_base_body": None,
            "panel_static": {}, "panel_momentum": None,
            "panel_momentum_ts": 0.0, "last_panel_edit": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
    if _watches:
        log.info("[watch] loaded %d watches", len(_watches))


async def start_watch(channel_id: int, entry: dict, level: str,
                    notify: bool = True) -> dict | None:
    """Start watching one game in a channel. entry is a grounding
    dict ({id, home, away, league, status}); the detail is fetched
    fresh so the snapshot starts from live data. When the game
    already has a live panel in this channel (e.g. a /game reply),
    the panel is kept — same message, same kind — and only the
    notification flag is switched on. The result says whether a
    panel already existed, so the caller never posts a second one."""
    game_id = str(entry["id"])
    detail = None
    try:
        result = await prov.onex_search(game_id)
        detail = result.get("detail")
    except Exception as exc:
        log.warning("[watch] detail fetch failed for %s (%s)", game_id,
                    type(exc).__name__)
    if detail is not None:
        # onex_search resolves alias ids to the live game — watch
        # under the live id or the tick lookup never matches.
        game_id = str(detail.get("onex_id") or game_id)
    # The panel lookup runs after the alias resolution above, so a
    # /game panel registered under the live id is found.
    old = _watches.get((channel_id, game_id))
    panel_exists = bool(old and old.get("panel_message_id"))
    panel_kind = (old.get("panel_kind") if old else None) or "compact"
    if detail is None:
        detail = {
            "onex_id": game_id, "home": entry.get("home", ""),
            "away": entry.get("away", ""), "league": entry.get("league", ""),
            "home_score": None, "away_score": None,
            "status": entry.get("status", ""), "minute": None, "clock": "",
            "stats": None, "cricket": None, "format": "",
        }
    snapshot = snapshot_from_detail(detail)
    snapshot["_home"], snapshot["_away"] = detail.get("home", ""), detail.get("away", "")
    fmt = str(detail.get("format") or "")
    await db.add_watch(channel_id, game_id, detail.get("home", ""),
                       detail.get("away", ""), detail.get("league", ""),
                       level, fmt, snapshot, notify=notify,
                       panel_kind=panel_kind)
    # A re-follow keeps the old panel's message id, body and
    # clock anchor, so the tick edits the same message.
    kept = old or {}
    _watches[(channel_id, game_id)] = {
        "home": detail.get("home", ""), "away": detail.get("away", ""),
        "league": detail.get("league", ""), "detail": level, "fmt": fmt,
        "snapshot": snapshot, "missing": 0, "missing_since": 0.0,
        "notify": bool(notify), "panel_kind": panel_kind,
        # Live panel: posted on the next 1s tick (see _refresh_panel)
        # and edited in place after that — never a second message.
        "panel_message_id": kept.get("panel_message_id"),
        "panel_body": kept.get("panel_body"),
        "panel_base_body": kept.get("panel_base_body"),
        # A re-follow keeps the old panel's static fields too, so
        # the next tick does not strip the card's Venue/Weather.
        "panel_static": (kept.get("panel_static") or
                         _panel_static_from_detail(detail)),
        "panel_momentum": kept.get("panel_momentum"),
        "panel_momentum_ts": kept.get("panel_momentum_ts") or 0.0,
        "last_panel_edit": kept.get("last_panel_edit") or 0.0,
        "panel_timer_sec": kept.get("panel_timer_sec"),
        "panel_timer_ts": kept.get("panel_timer_ts") or 0.0,
    }
    return {"game_id": game_id, "home": detail.get("home", ""),
            "away": detail.get("away", ""), "league": detail.get("league", ""),
            "status": detail.get("status", ""), "detail": level,
            "live_detail": detail, "panel_exists": panel_exists}


async def set_panel_message(channel_id: int, game_id: str,
                            message_id: int, body: str) -> None:
    """Record the panel message for a watch (in memory + db), so
    the 1s tick edits this message instead of posting a new one.
    Used when /watch posts the panel as its reply."""
    watch = _watches.get((channel_id, str(game_id)))
    if watch is not None:
        watch["panel_message_id"] = message_id
        watch["panel_body"] = body
        watch["panel_base_body"] = _strip_updated_footer(body)
    await db.set_watch_panel(channel_id, str(game_id), message_id)


async def post_panel_now(bot, channel_id: int, game_id: str,
                        detail: dict) -> bool:
    """Post the live panel for a watch right now (used by /watch
    when the target channel is not the command channel). True
    when the panel was posted (or already existed)."""
    watch = _watches.get((channel_id, str(game_id)))
    if watch is None:
        return False
    return await _refresh_panel(bot, channel_id, game_id, watch, detail)


async def register_game_panel(channel_id: int, game_id: str,
                              message_id: int, body: str,
                              detail: dict, level: str) -> bool:
    """Turn a /game reply into a live panel: the same 1s tick
    edits this message in place (score + MM:SS clock) while the
    game is live, but no event alerts are sent — Follow switches
    alerts on later. False when the game already has a panel in
    this channel (the reply stays a static detail card instead of
    a second ticking message)."""
    game_id = str(game_id)
    if (channel_id, game_id) in _watches:
        return False
    snapshot = snapshot_from_detail(detail)
    snapshot["_home"], snapshot["_away"] = detail.get("home", ""), detail.get("away", "")
    fmt = str(detail.get("format") or "")
    await db.add_watch(channel_id, game_id, detail.get("home", ""),
                       detail.get("away", ""), detail.get("league", ""),
                       level, fmt, snapshot, notify=False,
                       panel_kind="detail")
    await db.set_watch_panel(channel_id, game_id, message_id)
    _watches[(channel_id, game_id)] = {
        "home": detail.get("home", ""), "away": detail.get("away", ""),
        "league": detail.get("league", ""), "detail": level, "fmt": fmt,
        "snapshot": snapshot, "missing": 0, "missing_since": 0.0,
        "notify": False, "panel_kind": "detail",
        "panel_message_id": message_id, "panel_body": body,
        "panel_base_body": _strip_updated_footer(body),
        "panel_static": _panel_static_from_detail(detail),
        "panel_momentum": None, "panel_momentum_ts": 0.0,
        "last_panel_edit": 0.0,
        "panel_timer_sec": None, "panel_timer_ts": 0.0,
    }
    return True


async def _persist_panel_view(channel_id: int, game_id: str,
                              entry: dict) -> None:
    """Persist a panel view switch (kind/detail level) without
    touching the panel message id — add_watch's ON CONFLICT
    keeps the stored panel_message_id. Best effort: the in-memory
    view is what the tick uses."""
    try:
        await db.add_watch(channel_id, game_id, entry.get("home", ""),
                           entry.get("away", ""), entry.get("league", ""),
                           entry.get("detail") or DETAIL_SIMPLE,
                           entry.get("fmt") or "",
                           entry.get("snapshot") or {},
                           notify=bool(entry.get("notify", True)),
                           panel_kind=entry.get("panel_kind") or "compact")
    except Exception:
        pass


async def adopt_game_panel(channel_id: int, game_id: str,
                           message_id: int, body: str,
                           detail: dict, level: str,
                           kind: str = "detail") -> dict:
    """Adopt a detail-card or compact-panel message as the live
    panel — called by view taps (see _switch_panel_view):
    the message a tap just sent BECOMES the panel, so the
    tick edits it from then on. Any older panel message for
    the game stops ticking (it stays as it is, frozen). The
    watch's alerts flag, snapshot and clock anchor are kept,
    so the refresh continues without a gap. Returns the watch
    entry. The db writes are best effort: the in-memory entry
    is what the tick uses, so a call never fails because
    sqlite hiccuped."""
    game_id = str(game_id)
    snapshot = snapshot_from_detail(detail)
    snapshot["_home"], snapshot["_away"] = detail.get("home", ""), detail.get("away", "")
    fmt = str(detail.get("format") or "")
    static = _panel_static_from_detail(detail)
    old = _watches.get((channel_id, game_id))
    if old is None:
        try:
            await db.add_watch(channel_id, game_id, detail.get("home", ""),
                               detail.get("away", ""), detail.get("league", ""),
                               level, fmt, snapshot, notify=False,
                               panel_kind=kind)
            await db.set_watch_panel(channel_id, game_id, message_id)
        except Exception as exc:
            log.warning("[watch] adopt panel persist failed for %s (%s)",
                        game_id, type(exc).__name__)
        entry = {
            "home": detail.get("home", ""), "away": detail.get("away", ""),
            "league": detail.get("league", ""), "detail": level, "fmt": fmt,
            "snapshot": snapshot, "missing": 0, "missing_since": 0.0,
            "notify": False, "panel_kind": kind,
            "panel_message_id": message_id, "panel_body": body,
            "panel_base_body": _strip_updated_footer(body),
            "panel_static": static,
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            "last_panel_edit": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        _watches[(channel_id, game_id)] = entry
        return entry
    # An entry already exists: re-point it at the tapped message
    # and keep its alerts flag, snapshot and clock anchor.
    old["panel_message_id"] = message_id
    old["panel_body"] = body
    old["panel_base_body"] = _strip_updated_footer(body)
    old["panel_kind"] = kind
    old["detail"] = level
    old["panel_static"] = static
    old["last_panel_edit"] = 0.0
    await _persist_panel_view(channel_id, game_id, old)
    try:
        await db.set_watch_panel(channel_id, game_id, message_id)
    except Exception as exc:
        log.warning("[watch] adopt panel persist failed for %s (%s)",
                    game_id, type(exc).__name__)
    return old


async def set_notify(channel_id: int, game_id: str,
                     notify: bool) -> dict | None:
    """Follow / Unfollow: switch event alerts for a game without
    touching its live panel — the panel keeps ticking either way.
    Returns the entry, or None when the game has no panel here."""
    watch = _watches.get((channel_id, str(game_id)))
    if watch is None:
        return None
    watch["notify"] = bool(notify)
    await db.set_watch_notify(channel_id, str(game_id), bool(notify))
    return watch


async def remove_watch(channel_id: int, game_id: str) -> dict | None:
    entry = _watches.pop((channel_id, str(game_id)), None)
    # Stop the panel's worker, if one is waiting for its slot —
    # it checks the watch itself, but cancelling avoids a stale
    # edit landing after the watch is gone. Never cancel the
    # task running right now (remove_watch can be reached from
    # inside a worker's own _refresh_panel call).
    task = _panel_workers.pop((channel_id, str(game_id)), None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    await db.remove_watch(channel_id, str(game_id))
    return entry


async def list_channel_watches(channel_id: int,
                               include_view_only: bool = False) -> list[dict]:
    """Rows for /watching, /unwatch and /notifications. By
    default only games with alerts on; with include_view_only
    every watched game in the channel, including view-only /game
    panels (they still tick). Every row carries its "notify"
    flag so the caller can show alerts on/off."""
    rows = await db.list_watches(channel_id)
    out = []
    for row in rows:
        # View-only /game panels are not alert watches — they
        # are left out unless the caller asked for all of them.
        if not include_view_only and not row.get("notify", 1):
            continue
        try:
            snapshot = json.loads(row.get("snapshot") or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        out.append({
            "game_id": str(row["game_id"]), "home": row.get("home", ""),
            "away": row.get("away", ""), "league": row.get("league", ""),
            "detail": row.get("detail") or DETAIL_SIMPLE,
            "snapshot": snapshot,
            "notify": bool(row.get("notify", 1)),
        })
    return out


# Statuses whose panel is live and refreshing right now — the
# same set /game uses to decide a reply becomes a panel.
_PANEL_LIVE_STATUSES = ("1H", "2H", "ET", "PEN", "HT", "Live",
                        "1st innings", "2nd innings")


def is_panel_live(status) -> bool:
    """True when a watch's snapshot status means its panel is
    live and refreshing (as opposed to an upcoming game, whose
    panel has not started yet)."""
    return str(status or "") in _PANEL_LIVE_STATUSES


def refresh_interval_s(live_count: int) -> int:
    """Seconds between refreshes for ONE live game when
    `live_count` games share the channel's ~1 edit/s budget:
    1 game -> every 1s, 2 games -> every 2s each, 3 -> every
    3s, 4 -> every 4s. This is the ATTEMPT rule — the panel
    tries an edit on every tick it is due; how many edits
    actually land is Discord's call (see the note on
    _maybe_log_panel_cadence: the live journal showed only
    ~27–33 edits/min for 1 panel on 18 Nov 2026, not 60)."""
    return max(1, int(live_count or 1))


def watching_label(live_count: int | None) -> str:
    """The 👀 Watching button label for a channel with
    `live_count` live games: the count plus each game's
    refresh speed. None means the count is unknown (a
    persistent view with no channel, an ephemeral detail) —
    then the plain static label, never a guessed number."""
    if live_count is None:
        return "👀 Watching"
    n = int(live_count)
    if n <= 0:
        return "👀 Watching: 0"
    if n == 1:
        return "👀 Watching: 1 · 1s"
    return f"👀 Watching: {n} · {n}s each"


def live_watch_count(channel_id: int) -> int:
    """Live games watched in this channel right now, from the
    in-memory registry (no db read — this runs on every panel
    edit, so it must stay cheap). A game counts when its
    snapshot status is live; upcoming games and ended watches
    do not count."""
    return sum(
        1 for (cid, _gid), w in _watches.items()
        if cid == channel_id
        and is_panel_live((w.get("snapshot") or {}).get("status")))


def _watching_text(rows: list[dict]) -> str:
    """The /watching text from already-fetched rows — the SAME
    text the /watching command and the Watching button show, so
    they can never drift apart. Header: live count + each
    game's refresh interval (the divide rule); one row per
    watched game (teams, score, clock/overs, alerts on/off,
    detail level, game ID); upcoming games say "not refreshing
    yet". No games at all: the short "use /watch" line."""
    if not rows:
        return ("No games watched in this channel. "
                "Use `/watch <game>` to follow one.")
    live = [r for r in rows
            if is_panel_live((r.get("snapshot") or {}).get("status"))]
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
        if is_panel_live(snap.get("status")):
            each = "every second" if n == 1 else f"every {n}s"
            lines.append(f"• **{r['home']} vs {r['away']}**{score}{clock} · "
                         f"{each} · {alerts} "
                         f"({r['detail']}, ID `{r['game_id']}`)")
        else:
            lines.append(f"• **{r['home']} vs {r['away']}**{score}{clock} · "
                         f"not refreshing yet · {alerts} "
                         f"({r['detail']}, ID `{r['game_id']}`)")
    return "\n".join(lines)


async def render_watching(channel_id: int) -> str:
    """The /watching text for a channel — fetched rows rendered
    by _watching_text, the same text the Watching button shows,
    so the command and the button can never drift apart."""
    rows = await list_channel_watches(channel_id, include_view_only=True)
    return _watching_text(rows)


def live_line(d: dict) -> str:
    """One short live line for the 🔄 Live button: teams, score,
    and the MM:SS clock for football, overs for cricket."""
    title = f"{d.get('home', '?')} {_score_str(d)} {d.get('away', '?')}"
    cricket = d.get("cricket") or {}
    if cricket.get("is_break"):
        return f"{title} · Innings break"
    if cricket.get("over_str") is not None:
        return f"{title} · {cricket['over_str']} ov"
    clock = _clock_str(d)
    return f"{title} · {clock}" if clock else title


# ══════════════════════════════════════════════════════════════════════════
# LIVE PANEL — one message per watched game, edited in place.
# ══════════════════════════════════════════════════════════════════════════

def now_npt_str() -> str:
    """Current Nepal time as HH:MM:SS, for the panel's Updated
    footer — the visible proof a panel is alive."""
    return datetime.now(NEPAL).strftime("%H:%M:%S")


_UPDATED_FOOTER_RE = re.compile(r"\n\n-# Updated \d{2}:\d{2}:\d{2} NPT$")


def _strip_updated_footer(text: str) -> str:
    """The panel text without its Updated footer — the part the
    tick compares to decide whether the GAME changed. The footer
    alone must never count as a change, or the panel would edit
    itself every second forever."""
    return _UPDATED_FOOTER_RE.sub("", text or "")


def _updated_footer(updated: str | None) -> str:
    return f"\n\n-# Updated {updated} NPT" if updated else ""


def render_panel(d: dict, clock: str | None = None,
                 updated: str | None = None) -> str:
    """One game's live panel: league header, score, ticking clock
    (or overs / Innings break for cricket), Game ID line so the
    panel buttons keep resolving. The panel is edited in place on
    every tick — routine changes never send a new message. Pass
    clock=None to use the detail's own clock (one-off refreshes);
    the tick passes the locally ticking MM:SS."""
    status = str(d.get("status") or "")
    league = str(d.get("league") or "")
    if status == "FT":
        header = "🏁 Full-time" + (f" — {league}" if league else "")
    else:
        header = "🔴 LIVE" + (f" — {league}" if league else "")
    lines = [header,
             f"**{d.get('home', '?')} {_score_str(d)} {d.get('away', '?')}**"]
    cricket = d.get("cricket") or {}
    if cricket.get("is_break"):
        lines.append("☕ Innings break")
    elif cricket.get("over_str") is not None:
        lines.append(f"🏏 {cricket['over_str']} ov")
    else:
        if clock is None:
            clock = _clock_str(d)
        if clock == "Half-time":
            lines.append("⏸️ Half-time")
        elif clock == "Full-time":
            pass  # the header already says Full-time
        elif clock:
            lines.append(f"⏱️ {clock}")
    gid = str(d.get("onex_id") or d.get("id") or "?")
    const = d.get("const_id")
    lines.append(f"Game ID: {gid}" + (f" · {const}" if const else ""))
    return "\n".join(lines) + _updated_footer(updated)


def _panel_clock(watch: dict, detail: dict, now: float) -> str:
    """MM:SS for the panel, ticking every second. The feed's
    timer_sec is the anchor; between feed updates the clock runs
    forward locally (same trick as the website scoreboard), so the
    panel time changes every second even when the feed is quiet.
    Only runs during a live half — half-time/full-time show their
    labels instead, and cricket has no seconds clock."""
    status = str(detail.get("status") or "")
    if status in ("HT", "Half-time"):
        return "Half-time"
    if status in ("FT", "Full-time"):
        return "Full-time"
    if status not in ("1H", "2H", "ET", "PEN"):
        return _clock_str(detail)
    timer = detail.get("timer_sec")
    if isinstance(timer, (int, float)):
        base = watch.get("panel_timer_sec")
        if base is None or float(timer) != float(base):
            watch["panel_timer_sec"] = float(timer)
            watch["panel_timer_ts"] = now
            return prov.format_mmss(timer) or _clock_str(detail)
    base = watch.get("panel_timer_sec")
    base_ts = watch.get("panel_timer_ts") or 0.0
    if isinstance(base, (int, float)) and base_ts:
        return (prov.format_mmss(base + max(0.0, now - base_ts))
                or _clock_str(detail))
    return _clock_str(detail)


# Panel heartbeat: a panel whose game has not changed still
# proves it is alive — the Updated line advances every second,
# like the ticking clock on the site. Score/overs/clock changes
# still land within a second; the heartbeat only fires when
# nothing else changed, so a quiet cricket panel edits once a
# second on its footer alone. The Updated time is the LAST-EDIT
# time, not a promise the score moved — honest, and it is the
# only thing that can move when no ball has been bowled.
#
# Every panel gets its edit on every tick (see
# _schedule_panel_edits): the tick hands each panel's fresh
# detail to that panel's worker task, content changes first,
# and the edits go through the ONE shared per-channel scheduler
# (see channel_edits) — 1 game ATTEMPTS a refresh every 1s, 2
# games every 2s each, 3 every 3s, 4 every 4s. Attempt, not
# landing: the live journal showed 1 panel landing only
# ~27–33 edits/min, so the divide rule is about how the
# ~1 edit/s channel budget is SHARED, never a promise that
# 60 edits/min will land. The old design paced
# the panels with a per-panel slot timeline (1s apart) and let
# the scoreboard edit the same channel OUTSIDE that timeline:
# the VM journal showed only ~30 edits/min for 2 panels with
# ~14.6 Discord 429s/min, because any 5s window with 5 panel
# edits plus one scoreboard edit blew the 5-per-5s bucket and
# each 429 stalled ~4.2s. The scheduler owns ALL edits in the
# channel now — panels and scoreboard alike — so the bucket
# never sees more than 1 edit/s and there is nothing left to
# collide with. A worker always edits the FRESHEST pending
# detail, so a panel never reverts to a stale score.
# 0.9s, not 1.0s: the scheduler spaces edits 1.0s apart, and a
# gate at exactly 1.0s would refuse a slot-timed heartbeat that
# lands a few ms early — halving a quiet panel's cadence.
_PANEL_HEARTBEAT_S = 0.9
# Analytics momentum stays on its old 30s cadence — it is a db
# read, not a visible edit, and nothing about it needs to move
# every second.
_PANEL_MOMENTUM_REFRESH_S = 30.0
# One background worker task per live panel, keyed by
# (channel_id, game_id). A worker loops while its panel has a
# fresh detail pending and exits when there is nothing to do;
# the tick only hands it fresh detail and starts it if it is not
# already running — so there is never more than one worker per
# panel, and a panel never edits from a stale detail. The
# workers do NOT pace edits themselves any more: pacing lives
# entirely in channel_edits, which also paces the scoreboard
# and any button-tap edit in the same channel.
_panel_workers: dict[tuple, asyncio.Task] = {}
# Cadence log: successful tick edits in the current 60s window.
# Logged once a minute (see _maybe_log_panel_cadence) so the VM
# journal can prove how fast panels really edit.
_PANEL_LOG_EVERY_S = 60.0
_panel_log_window_start: float = 0.0
_panel_log_edits: int = 0


async def _get_channel(bot: discord.Client, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await asyncio.wait_for(bot.fetch_channel(channel_id), 20)
        except Exception as exc:
            log.warning("[watch] panel channel %s unavailable (%s)",
                        channel_id, type(exc).__name__)
            return None
    return channel


def _panel_static_from_detail(detail: dict) -> dict:
    """The fields a /game reply knows that the 1s tick does not:
    venue / weather / format come from gameInfo, which the tick
    never calls. Kept on the watch so the first tick does not
    strip those lines off the card. In-memory only — after a
    restart they drop once and come back on the next Refresh."""
    return {
        "venue": str(detail.get("venue") or ""),
        "weather": str(detail.get("weather") or ""),
        "format": str(detail.get("format") or ""),
        "league": str(detail.get("league") or ""),
        "start_ts": detail.get("start_ts"),
    }


def _with_static(detail: dict, watch: dict) -> dict:
    """Fill the tick detail's blank venue/weather/format/league/
    kickoff from the panel's stored static fields."""
    static = watch.get("panel_static") or {}
    if not static:
        return detail
    out = dict(detail)
    for key in ("venue", "weather", "format", "league"):
        if not out.get(key) and static.get(key):
            out[key] = static[key]
    if not out.get("start_ts") and static.get("start_ts"):
        out["start_ts"] = static["start_ts"]
    return out


def _render_panel_view(detail: dict, watch: dict, clock: str,
                       updated: str | None) -> str:
    """Render the panel in its current view: the /game detail
    card (simple or advanced), the analytics table, or the
    compact live line. The ticking clock and the stored static
    fields go into the detail views; the compact view renders
    from the raw detail."""
    kind = watch.get("panel_kind") or "compact"
    if kind == "detail":
        ticking = _with_static(dict(detail), watch)
        ticking["clock"] = clock
        return render_detail(ticking,
                             watch.get("detail") or DETAIL_SIMPLE,
                             updated=updated)
    if kind == "analytics":
        ticking = _with_static(dict(detail), watch)
        ticking["clock"] = clock
        return render_analytics(ticking, watch.get("panel_momentum"),
                                updated=updated)
    return render_panel(detail, clock, updated=updated)


async def _refresh_panel(bot: discord.Client, channel_id: int,
                         game_id: str, watch: dict,
                         detail: dict | None = None,
                         text: str | None = None,
                         post_if_missing: bool = True,
                         heartbeat: bool = True,
                         priority: bool = False) -> bool:
    """Post the watch panel on its first tick, then edit it in
    place whenever its text changes. Pass text to edit the panel to
    a fixed line (e.g. the ended state) instead of rendering from
    detail. Returns True when a send/edit happened. Never raises —
    a failed edit is retried on the next tick, a deleted watch
    panel is re-posted once, and a deleted /game panel stops
    quietly (its reply stays deleted, no new message).

    Edits go through the shared per-channel scheduler
    (channel_edits): it paces ALL edits in the channel — panels,
    scoreboard, button taps — 1.0s apart, so the 5-per-5s bucket
    never overflows. A 429 pauses the WHOLE channel until
    retry_after (the bucket is per-channel, so any other edit in
    the meantime would 429 too); there is no per-panel block any
    more — the old one let other panels keep firing into a
    drained bucket, which is what cascaded. `priority` jumps the
    edit ahead of queued heartbeats (user taps and final
    full-time states only) — still paced, still inside the budget.

    The tick compares the render WITHOUT the Updated footer
    (panel_base_body): a score/overs/clock change edits at once,
    and when nothing about the game changed the panel still
    heartbeat-edits its footer every second so it visibly proves
    it is alive. The base is only stored after a successful edit —
    a failed edit must be retried, not swallowed by the
    heartbeat. heartbeat=False skips the heartbeat edit (a
    content change still edits) — the final full-time edit uses
    it, so a panel that is about to be removed never spends its
    last edit on a footer-only heartbeat."""
    new_base = None
    if text is None:
        if detail is None:
            return False
        now = time.monotonic()
        if watch.get("panel_kind") == "analytics" and \
                now - (watch.get("panel_momentum_ts") or 0) \
                >= _PANEL_MOMENTUM_REFRESH_S:
            # The momentum estimate stays fresh without a db read
            # on every tick — still a 30s cadence, even though the
            # heartbeat itself is now 1s.
            try:
                watch["panel_momentum"] = await history.momentum_summary(game_id)
            except Exception:
                pass
            watch["panel_momentum_ts"] = now
        clock = _panel_clock(watch, detail, now)
        base = _render_panel_view(detail, watch, clock, updated=None)
        if base == watch.get("panel_base_body"):
            # Nothing about the game changed: heartbeat only.
            if not heartbeat:
                return False
            if now - (watch.get("last_panel_edit") or 0) \
                    < _PANEL_HEARTBEAT_S:
                return False
        new_base = base
        text = _render_panel_view(detail, watch, clock,
                                  updated=now_npt_str())
    if text == watch.get("panel_body"):
        return False
    # Fresh view on every edit, so the 👀 Watching button's
    # count + speed update as games go live and end.
    view = WatchAlertView(game_id,
                          live_count=live_watch_count(channel_id))
    message_id = watch.get("panel_message_id")
    if not message_id:
        if not post_if_missing:
            # Ended watch with no panel yet: edit only, never post.
            return False
        # First post of a panel is a single send, not an edit —
        # sends do not go through the edit scheduler, and a panel
        # posted and edited in quick succession (the clock test
        # fakes 5s passing) must not be blocked by its own post.
        channel = await _get_channel(bot, channel_id)
        if channel is None:
            return False
        try:
            message = await channel.send(
                text, view=view,
                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("[watch] panel post failed for %s (%s)",
                        game_id, exc)
            return False
        watch["panel_message_id"] = message.id
        try:
            await db.set_watch_panel(channel_id, game_id, message.id)
        except Exception:
            pass
        watch["panel_body"] = text
        if new_base is not None:
            # Only after a successful send — a failed send is
            # retried on the next tick, not swallowed.
            watch["panel_base_body"] = new_base
        watch["last_panel_edit"] = time.monotonic()
        return True
    # Edit in place, through the shared per-channel scheduler —
    # it fetches the message, paces the edit against every other
    # edit in the channel (scoreboard included), and reports
    # whether the message is gone.
    status, _landed = await channel_edits.edit_message(
        bot, channel_id, message_id, content=text, view=view,
        priority=priority)
    if status == "gone":
        if watch.get("panel_kind") in ("detail", "analytics"):
            # A deleted /game reply stays deleted — the panel
            # stops quietly instead of posting a new message.
            log.info("[watch] game panel deleted for %s; stopping",
                     game_id)
            await remove_watch(channel_id, game_id)
            return False
        log.info("[watch] panel message gone for %s; posting fresh",
                 game_id)
        if not post_if_missing:
            return False
        watch["panel_message_id"] = None
        return await _refresh_panel(bot, channel_id, game_id, watch,
                                    detail=detail, text=text,
                                    post_if_missing=post_if_missing,
                                    heartbeat=heartbeat,
                                    priority=priority)
    if status != "ok":
        # Failed, superseded, or the channel is paused after a
        # 429 — the next tick retries with fresh detail.
        return False
    watch["panel_body"] = text
    if new_base is not None:
        # Only after a successful edit — a failed edit is
        # retried on the next tick, not swallowed.
        watch["panel_base_body"] = new_base
    # The landing time, not the render time: the edit may have
    # waited in the scheduler's queue, and the heartbeat gate
    # measures from when the panel actually moved.
    watch["last_panel_edit"] = time.monotonic()
    return True


async def delete_panel_message(bot: discord.Client | None,
                               channel_id: int, game_id: str) -> bool:
    """Delete a watch's panel message — used by /unwatch, the
    full remove. Best-effort, never raises. Terminal watches
    keep their panel (it shows the final score), so on_tick
    never calls this; the 🔕 Unfollow button never calls it
    either (notifications-only, panel keeps ticking)."""
    if bot is None:
        return False
    watch = _watches.get((channel_id, str(game_id)))
    if not watch:
        return False
    message_id = watch.get("panel_message_id")
    watch["panel_message_id"] = None
    watch["panel_body"] = None
    if not message_id:
        return False
    try:
        channel = await _get_channel(bot, channel_id)
        if channel is None:
            return False
        message = await channel.fetch_message(message_id)
        await message.delete()
        return True
    except Exception as exc:
        log.warning("[watch] panel delete failed for %s (%s)",
                    game_id, type(exc).__name__)
        return False


async def _fetch_live_detail(game_id: str) -> dict | None:
    """Current detail for a game via onex_search (tick cache, fast).
    None when the game is gone from the live list — a score-less
    gameInfo-only detail is not live data either."""
    try:
        result = await prov.onex_search(str(game_id))
    except Exception as exc:
        log.warning("[watch] live detail fetch failed for %s (%s)",
                    game_id, type(exc).__name__)
        return None
    detail = (result or {}).get("detail")
    if detail is None:
        return None
    if detail.get("home_score") is None and detail.get("away_score") is None \
            and str(detail.get("status") or "") in ("?", "upcoming", ""):
        return None
    return detail


_GAME_ID_RE = re.compile(r"Game ID:\s*(\d+)")


def _resolve_game_id(interaction: discord.Interaction) -> str | None:
    """Game id for a 📊 Detail / 🔄 Live button click. First the
    per-game custom_id ("watch:detail:<id>" / "watch:live:<id>")
    when the button was built with one; otherwise the "Game ID:
    <id>" line in the message the button sits on — every detail
    reply and every alert prints one. None when neither is there."""
    custom_id = ""
    try:
        custom_id = str((interaction.data or {}).get("custom_id") or "")
    except Exception:
        custom_id = ""
    parts = custom_id.split(":")
    if len(parts) >= 3 and parts[2].isdigit():
        return parts[2]
    message = getattr(interaction, "message", None)
    content = getattr(message, "content", "") or ""
    match = _GAME_ID_RE.search(content)
    if match:
        return match.group(1)
    return None


# Refresh cooldown: one manual refresh per user per game per 10s,
# so a button cannot hammer the provider.
_REFRESH_COOLDOWN_S = 10.0
_refresh_cooldown: dict[tuple[int, str], float] = {}


def _tap_log(interaction: discord.Interaction, button: str,
             game_id: str | None, outcome: str) -> None:
    """One journal line per button tap: button, game id,
    channel, user, outcome. Taps used to log NOTHING — no
    on_interaction handler, no logging in any button callback
    — so a "buttons not working" report was invisible in the
    journal and could only be investigated from the database.
    Every tap below logs exactly one line: maintenance-blocked
    taps log here from interaction_check (they never reach a
    handler); every other tap logs its outcome from its
    handler."""
    try:
        channel_id = getattr(interaction, "channel_id", None)
        user_id = getattr(getattr(interaction, "user", None), "id", None)
    except Exception:
        channel_id = user_id = None
    log.info("[watch] tap %s game=%s channel=%s user=%s -> %s",
             button, game_id, channel_id, user_id, outcome)


async def _switch_panel_view(interaction: discord.Interaction,
                             kind: str | None,
                             level: str | None = None,
                             button: str = "view") -> None:
    """Detail / Live / Simple / Advanced / Refresh: send the
    requested view as a NEW channel message — never edit the
    live panel in place. kind None = Refresh (the panel's
    current view, or a simple detail card with no panel).

    WHY (corrected 18 Nov 2026 from the VM journal + db, NOT
    from first principles — the first diagnosis below was
    wrong and is kept here so nobody reverts to it): the
    first version of this docstring blamed a race — the tap
    editing the panel in place while the 1s tick's queued
    PATCH overwrote it with the old view. The journal
    DISPROVES that for the reported case: the Advanced tap DID
    reach the bot (the watch row for Cheetah Kasoa vs Gaz
    Nobles 772022290 has panel_kind='analytics', set only by
    the Advanced path) and the tick renders from the stored
    panel_kind, so it does NOT wipe the chosen view — panel
    edits were landing ~48-55/min with 0 Discord 429s. What
    the user actually saw was: (1) Advanced showed "No match
    statistics available yet" because that game carries no
    stats table in the live feed — CORRECT, not a bug (stats
    come from the live list's tabloStats, not gameInfo; the
    separate gameInfo 400s only lose venue/format — see
    providers.onex_game_info); (2) a tap on any message OTHER
    than the live panel — an old panel, a goal alert, an
    ephemeral reply — silently switched the REAL panel
    elsewhere, with no visible feedback on the message tapped;
    (3) old panel messages are stale/frozen after a
    restart/repost, so tapping one did nothing visible;
    (4) taps logged NOTHING, so none of this showed in the
    journal. The new-message design fixes (2) and (3) by
    construction — every tap answers with a visible new
    message on the message tapped, and never adopts a stale
    message as a panel — and _tap_log fixes (4). (The
    two-writers race stays a real hazard of the OLD in-place
    design, which is one more reason it is gone; it just was
    not what happened here.)

    - THE PANEL MOVES (18 Nov 2026, proven live by a
      read-only VM check at 22:57 NPT — this replaces the
      aadf03dd repair below, which was not enough): after the
      user's Advanced tap on CR Temouchent vs JS El Biar
      (772046268) the live panel kept ticking at 45–52
      edits/min for 6+ minutes with 0 Tracebacks, and the
      panel_kind DID switch to analytics — but the user stared
      at the tap's OWN new message, which is never edited
      again, while the tick kept editing only the original
      registered panel_message_id elsewhere in the channel.
      That dead copy is what "refreshing stopped" was. So the
      new message BECOMES the live panel: its id is stored as
      the watch's panel_message_id (persisted), the tick edits
      THAT message from then on, in the tapped view, and the
      old panel message is left as-is, just never edited
      again. A second tap moves the panel again, to the
      newest message. A tap on a stale message behaves the
      same — the newest tapped message is the panel.
    - The tap itself never edits any message in place: the
      panel moves by re-pointing, not by an edit. The new
      message IS the feedback: no ephemeral "updated"
      follow-up is sent as well. Errors (unknown game, game no
      longer live) stay ephemeral, so a failed tap never spams
      the channel.
    NOTE: "No match statistics available yet" on Advanced is
    NOT a bug — render_analytics only shows stats the feed
    actually sends (football: tabloStats), and a game/league
    that sends none has nothing to show. When the details
    feed is ALSO not answering (info_failed), the view says
    so plainly instead.

    REPAIR (aadf03dd, 18 Nov 2026 — "Advanced stops the
    refresh", first attempt): the snapshot was a one-off
    message that never ticks, and the live panel kept its OLD
    view — so the user stared at a frozen analytics card
    while the panel ticked on in compact view somewhere else.
    That build made a view tap ALSO switch the live panel to
    the tapped view (kind + detail level, persisted) and
    re-assert its worker. It was not enough: the panel
    switched view, but on its OLD message, while the user
    kept looking at the new one. The PANEL MOVES rule above
    is the complete fix — the view switch rides along with
    the move (adopt_game_panel sets kind + level too)."""
    game_id = _resolve_game_id(interaction)
    if game_id is None:
        _tap_log(interaction, button, None, "failed: unknown game")
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "Could not tell which game this is.", ephemeral=True)
        return
    channel_id = getattr(interaction, "channel_id", None)
    # Defer visibly: the answer is a new channel message, so
    # the followup below must not be ephemeral.
    await interaction.response.defer()
    detail = await _fetch_live_detail(game_id)
    if detail is None:
        _tap_log(interaction, button, game_id,
                 "failed: game no longer live")
        await interaction.followup.send(
            "That game is no longer live.", ephemeral=True)
        return
    entry = (_watches.get((channel_id, game_id))
             if channel_id is not None else None)
    now = time.monotonic()
    momentum = None
    if kind == "analytics":
        try:
            momentum = await history.momentum_summary(game_id)
        except Exception:
            momentum = None
        text = render_analytics(detail, momentum,
                                updated=now_npt_str())
    elif kind == "compact":
        clock = _panel_clock(entry, detail, now) if entry else None
        text = render_panel(detail, clock, updated=now_npt_str())
    elif kind == "detail":
        ticking = _with_static(dict(detail), entry) if entry else detail
        ticking["clock"] = (_panel_clock(entry, detail, now) if entry
                            else _clock_str(detail))
        text = render_detail(ticking, level or DETAIL_SIMPLE,
                             updated=now_npt_str())
    else:  # Refresh: the panel's current view, or simple detail.
        if entry is not None:
            if entry.get("panel_kind") == "analytics" and \
                    now - (entry.get("panel_momentum_ts") or 0) \
                    >= _PANEL_MOMENTUM_REFRESH_S:
                try:
                    entry["panel_momentum"] = \
                        await history.momentum_summary(game_id)
                except Exception:
                    pass
                entry["panel_momentum_ts"] = now
            clock = _panel_clock(entry, detail, now)
            text = _render_panel_view(detail, entry, clock,
                                      updated=now_npt_str())
        else:
            text = render_detail(detail, DETAIL_SIMPLE,
                                 updated=now_npt_str())
    # wait=True: the sent message's id is needed below — it
    # becomes the live panel (see the PANEL MOVES note in the
    # docstring). Real discord.py returns the Message; the
    # test fakes return a namespace with an id.
    sent = await interaction.followup.send(
        text,
        view=WatchAlertView(
            game_id,
            live_count=(live_watch_count(channel_id)
                        if channel_id is not None else None)),
        wait=True)
    sent_id = getattr(sent, "id", None)
    outcome = "sent new message"
    if entry is not None and sent_id is not None and \
            channel_id is not None:
        # THE PANEL MOVES (see the docstring): the message in
        # front of the user becomes the live panel — the tick
        # edits THIS message from now on, in the tapped view.
        # The old panel message is left as-is, just never
        # edited again. adopt_game_panel re-points the watch
        # (kind + detail level, persisted like
        # _persist_panel_view) while keeping its alerts flag,
        # snapshot and clock anchor, so the refresh continues
        # without a gap. Refresh (kind None) moves the panel
        # without switching its view.
        new_kind = kind or entry.get("panel_kind") or "compact"
        new_level = level or entry.get("detail") or DETAIL_SIMPLE
        entry = await adopt_game_panel(channel_id, game_id, sent_id,
                                       text, detail, new_level,
                                       kind=new_kind)
        if kind == "analytics":
            entry["panel_momentum"] = momentum
            entry["panel_momentum_ts"] = now
        outcome = f"sent new message; panel now {sent_id}"
    elif entry is not None and kind in ("analytics", "compact", "detail"):
        # No message id came back (a fake that returns None):
        # fall back to the old view-switch-only repair — the
        # panel keeps refreshing in the tapped view, on its
        # existing message.
        if kind == "analytics":
            entry["panel_kind"] = "analytics"
            entry["panel_momentum"] = momentum
            entry["panel_momentum_ts"] = now
        elif kind == "compact":
            entry["panel_kind"] = "compact"
        else:
            entry["panel_kind"] = "detail"
            if level:
                entry["detail"] = level
        await _persist_panel_view(channel_id, game_id, entry)
    _tap_log(interaction, button, game_id, outcome)
    if entry is not None:
        # Re-assert the panel worker with the fresh detail, so
        # the panel's next edit lands at once in the (possibly
        # new) view instead of waiting for the next 1s tick —
        # and so a worker that died mid-edit is recreated now,
        # not whenever the tick notices. Idempotent: when a
        # worker is already running it just gets fresher detail.
        # No bot handle in the interaction (tests, mostly) means
        # no worker to assert — the tick re-creates it anyway.
        bot = getattr(interaction, "client", None)
        if bot is not None and channel_id is not None:
            _schedule_panel_edits(
                bot, {(channel_id, game_id): detail},
                {(channel_id, game_id)})


class WatchAlertView(discord.ui.View):
    """Buttons on a watch alert or a detail reply — the live match
    panel (Feature 2). Persistent (timeout=None) with STATIC
    custom_ids ("watch:detail" / "watch:live" / "panel:*") — the
    game id is resolved from the message the button sits on, so
    one view registered in setup_hook covers every button on
    every message, even after a restart. A per-game custom_id
    ("watch:detail:<id>", "panel:refresh:<id>", ...) still works
    as a fallback when the view was built with a game id.

    Detail / Live / Simple / Advanced / Refresh send the
    requested view as a NEW channel message — and that message
    BECOMES the live panel (see _switch_panel_view): the tick
    edits it from then on, in the tapped view, while the old
    panel message is left frozen. The tap itself never edits
    any message in place.
    Timeline sends the chronoLine events as a new channel
    message too; Follow / Unfollow send a new channel message
    confirming, and start/stop alerts for this game in this
    channel. The 9th button, 👀 Watching, shows how many games
    are live in this channel and each one's refresh speed
    (1 game -> every 1s, 2 -> every 2s each, and so on — the
    divide rule); tapping it sends the /watching list as a new
    channel message, with one ❌ Remove button per watched
    game. Its label is rebuilt with every panel edit, so the
    count follows games going live and ending."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Maintenance mode: a press answers with the short notice
        # and no button runs — the bot is silent. A blocked tap
        # logs here (it never reaches a handler, so this is its
        # only journal line); an accepted tap logs its outcome
        # from its handler.
        if await maintenance.view_blocked(interaction):
            try:
                custom_id = str((interaction.data or {}).get(
                    "custom_id") or "?")
            except Exception:
                custom_id = "?"
            _tap_log(interaction, custom_id,
                     _resolve_game_id(interaction),
                     "blocked: maintenance")
            return False
        return True

    def __init__(self, game_id: str | None = None,
                 live_count: int | None = None,
                 channel_id: int | None = None,
                 watch_button: bool = False):
        super().__init__(timeout=None)
        self.game_id = str(game_id) if game_id else None
        # The live count, when the caller knows it (every panel
        # edit passes it — see _refresh_panel) or when the
        # channel is known and the registry can count it. None
        # stays None: the persistent view (no channel, no count)
        # and ephemeral details get the plain static label.
        if live_count is None and channel_id is not None:
            try:
                live_count = live_watch_count(int(channel_id))
            except Exception:
                live_count = None
        self.live_count = live_count

        def _cid(prefix: str) -> str:
            return f"{prefix}:{self.game_id}" if self.game_id else prefix

        specs = [
            ("📊 Detail", discord.ButtonStyle.primary, _cid("watch:detail"),
             self._on_detail),
            ("🔄 Live", discord.ButtonStyle.secondary, _cid("watch:live"),
             self._on_live),
            ("🔄 Refresh", discord.ButtonStyle.secondary, _cid("panel:refresh"),
             self._on_refresh),
            ("📜 Timeline", discord.ButtonStyle.secondary, _cid("panel:timeline"),
             self._on_timeline),
            ("🟢 Simple", discord.ButtonStyle.secondary, _cid("panel:simple"),
             self._on_simple),
            ("🔵 Advanced", discord.ButtonStyle.secondary, _cid("panel:advanced"),
             self._on_advanced),
            ("🔔 Follow", discord.ButtonStyle.success, _cid("panel:follow"),
             self._on_follow),
            ("🔕 Unfollow", discord.ButtonStyle.danger, _cid("panel:unfollow"),
             self._on_unfollow),
            (watching_label(live_count), discord.ButtonStyle.secondary,
             _cid("panel:watching"), self._on_watching),
        ]
        for label, style, custom_id, callback in specs:
            btn = discord.ui.Button(label=label, style=style,
                                    custom_id=custom_id)
            btn.callback = callback
            self.add_item(btn)
        if watch_button and self.game_id:
            # /ask detail replies only (18 Nov 2026): the user
            # asked for a ▶ Watch button when /ask shows a game.
            # Live panels do NOT get it — the game there is
            # already watched, and 🔔 Follow already starts a
            # watch from a panel.
            self.add_item(WatchGameButton(self.game_id, "▶ Watch"))

    async def _on_detail(self, interaction: discord.Interaction):
        # The detail card as a new message — which then becomes
        # the live panel (see _switch_panel_view).
        await _switch_panel_view(interaction, "detail",
                                 level=DETAIL_ADVANCED, button="Detail")

    async def _on_live(self, interaction: discord.Interaction):
        # The compact live line as a new message.
        await _switch_panel_view(interaction, "compact", button="Live")

    async def _on_refresh(self, interaction: discord.Interaction):
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            _tap_log(interaction, "Refresh", None,
                     "failed: unknown game")
            await interaction.response.send_message(
                "Could not tell which game this is.", ephemeral=True)
            return
        user_id = getattr(getattr(interaction, "user", None), "id", 0)
        now = time.monotonic()
        last = _refresh_cooldown.get((user_id, game_id), 0.0)
        if now - last < _REFRESH_COOLDOWN_S:
            # The 10s per-user cooldown is why a second quick
            # Refresh tap feels dead — it answers ephemeral
            # (visible feedback) and logs, instead of silence.
            _tap_log(interaction, "Refresh", game_id,
                     "blocked: refresh cooldown")
            await interaction.response.send_message(
                "Give it a few seconds before refreshing again.",
                ephemeral=True)
            return
        _refresh_cooldown[(user_id, game_id)] = now
        # Refresh re-renders the panel's current view — as a new
        # message; the tick already shows new scores on its own.
        await _switch_panel_view(interaction, None, button="Refresh")

    async def _on_timeline(self, interaction: discord.Interaction):
        # Timeline as a NEW channel message (was an ephemeral
        # one-off): the list is visible to the channel, like
        # every other tap now.
        await interaction.response.defer()
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            _tap_log(interaction, "Timeline", None,
                     "failed: unknown game")
            await interaction.followup.send(
                "Could not tell which game this is.", ephemeral=True)
            return
        detail = await _fetch_live_detail(game_id)
        if detail is None:
            _tap_log(interaction, "Timeline", game_id,
                     "failed: game no longer live")
            await interaction.followup.send(
                "That game is no longer live.", ephemeral=True)
            return
        try:
            gid = int(game_id)
        except (TypeError, ValueError):
            _tap_log(interaction, "Timeline", game_id,
                     "failed: bad game id")
            await interaction.followup.send(
                "Could not read this game's timeline.", ephemeral=True)
            return
        events = await prov.onex_events(
            gid, detail.get("home", ""), detail.get("away", ""))
        if not events:
            _tap_log(interaction, "Timeline", game_id,
                     "no timeline events yet")
            await interaction.followup.send(
                "No timeline events yet for this game.", ephemeral=True)
            return
        lines = [f"**{detail.get('home', '?')} {_score_str(detail)} "
                 f"{detail.get('away', '?')}** — timeline\n"]
        for ev in sorted(events, key=lambda e: e.get("minute_int") or 0):
            lines.append(f"{ev.get('icon', '')} **{ev.get('minute', '?')}'** "
                         f"{ev.get('detail', '')} — {ev.get('player', '')} "
                         f"({ev.get('team', '')})")
        await interaction.followup.send("\n".join(lines)[:2000])
        _tap_log(interaction, "Timeline", game_id, "sent new message")

    async def _on_simple(self, interaction: discord.Interaction):
        await _switch_panel_view(interaction, "detail",
                                 level=DETAIL_SIMPLE, button="Simple")

    async def _on_advanced(self, interaction: discord.Interaction):
        await _switch_panel_view(interaction, "analytics",
                                 button="Advanced")

    async def _on_follow(self, interaction: discord.Interaction):
        # Follow confirms with a NEW channel message (was
        # ephemeral): the confirmation is visible, like every
        # other tap now. The watch itself starts exactly as
        # before — the live panel keeps ticking in place.
        await interaction.response.defer()
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            _tap_log(interaction, "Follow", None,
                     "failed: unknown game")
            await interaction.followup.send(
                "Could not tell which game this is.", ephemeral=True)
            return
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            _tap_log(interaction, "Follow", game_id,
                     "failed: unknown channel")
            await interaction.followup.send(
                "Could not tell which channel this is.", ephemeral=True)
            return
        existing = _watches.get((channel_id, game_id))
        if existing is not None:
            # Follow controls notifications ONLY, not the live
            # view: a /game panel here keeps ticking, it just
            # starts sending event alerts too.
            if existing.get("notify", True):
                _tap_log(interaction, "Follow", game_id,
                         "already followed")
                await interaction.followup.send(
                    "That game is already watched in this channel.")
                return
            await set_notify(channel_id, game_id, True)
            _tap_log(interaction, "Follow", game_id,
                     "notifications on")
            await interaction.followup.send(
                f"🔔 Now following **{existing['home']} vs "
                f"{existing['away']}** — alerts on goals, wickets "
                f"and cards. The live panel keeps ticking here.")
            return
        detail = await _fetch_live_detail(game_id)
        if detail is None:
            _tap_log(interaction, "Follow", game_id,
                     "failed: game no longer live")
            await interaction.followup.send(
                "That game is no longer live.", ephemeral=True)
            return
        entry = {"id": game_id, "home": detail.get("home", ""),
                 "away": detail.get("away", ""),
                 "league": detail.get("league", ""),
                 "status": detail.get("status", "")}
        started = await start_watch(channel_id, entry, DETAIL_SIMPLE)
        if not started:
            _tap_log(interaction, "Follow", game_id,
                     "failed: could not start watch")
            await interaction.followup.send(
                "Could not start watching that game.", ephemeral=True)
            return
        _tap_log(interaction, "Follow", game_id, "watch started")
        await interaction.followup.send(
            f"👀 Watching **{started['home']} vs {started['away']}** — "
            f"a live panel in this channel ticks every second; simple "
            f"alerts only when the game changes.")

    async def _on_unfollow(self, interaction: discord.Interaction):
        # Unfollow confirms with a NEW channel message (was
        # ephemeral). It stops notifications ONLY — the live
        # panel keeps ticking in place while the game is live,
        # and stops on its own at full-time.
        await interaction.response.defer()
        game_id = _resolve_game_id(interaction)
        if game_id is None:
            _tap_log(interaction, "Unfollow", None,
                     "failed: unknown game")
            await interaction.followup.send(
                "Could not tell which game this is.", ephemeral=True)
            return
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            _tap_log(interaction, "Unfollow", game_id,
                     "failed: unknown channel")
            await interaction.followup.send(
                "Could not tell which channel this is.", ephemeral=True)
            return
        entry = _watches.get((channel_id, game_id))
        if entry is None:
            _tap_log(interaction, "Unfollow", game_id,
                     "not watched in this channel")
            await interaction.followup.send(
                "That game is not watched in this channel.")
            return
        await set_notify(channel_id, game_id, False)
        _tap_log(interaction, "Unfollow", game_id,
                 "notifications off")
        await interaction.followup.send(
            f"🔕 Notifications off for **{entry['home']} vs "
            f"{entry['away']}** — the live panel keeps ticking here "
            f"while the game is live.")

    async def _on_watching(self, interaction: discord.Interaction):
        # The 👀 Watching button: the /watching list for THIS
        # channel as a NEW channel message (was ephemeral) — the
        # same text render_watching builds for the /watching
        # command, so the two can never drift apart — plus one
        # ❌ Remove button per watched game on that message.
        # The button is channel-wide, not per-game, so no game
        # id is resolved here. A db failure never crashes the
        # tap — it says so plainly instead.
        await interaction.response.defer()
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            _tap_log(interaction, "Watching", None,
                     "failed: unknown channel")
            await interaction.followup.send(
                "Could not tell which channel this is.", ephemeral=True)
            return
        try:
            rows = await list_channel_watches(
                channel_id, include_view_only=True)
            text = _watching_text(rows)
        except Exception as exc:
            log.warning("[watch] watching list failed (%s)",
                        type(exc).__name__)
            _tap_log(interaction, "Watching", None,
                     "failed: list load error")
            await interaction.followup.send(
                "Could not load the watching list right now.",
                ephemeral=True)
            return
        await interaction.followup.send(text, view=WatchingListView(rows))
        _tap_log(interaction, "Watching", None,
                 f"sent new message ({len(rows)} watched)")


class RemoveWatchButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"panel:remove:(?P<game_id>\d+)"):
    """One ❌ Remove button on a watching-list message — a
    FULL remove like /unwatch (panel deleted, watch gone).

    A DynamicItem, not a plain button: discord.py only
    dispatches a tap after a restart when the custom_id
    matches a registered template. dispatch_view does an
    EXACT custom_id match — a per-game id built into a
    persistent view never matches the bare id registered in
    setup_hook, so after a restart those taps are silently
    discarded (this is one way "all buttons stop working"
    after a deploy-restart). The template is registered in
    setup_hook via bot.add_dynamic_items; the game id travels
    in the custom_id itself, so no in-memory state is needed
    to resolve it after a restart."""

    def __init__(self, game_id: str, label: str):
        button = discord.ui.Button(
            label=label, style=discord.ButtonStyle.danger,
            custom_id=f"panel:remove:{game_id}")
        super().__init__(button)
        self.game_id = str(game_id)
        button.callback = self._on_remove

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["game_id"], item.label)

    async def interaction_check(self, interaction) -> bool:
        # Maintenance gate, like every other button. A blocked
        # tap logs here (it never reaches the handler).
        if await maintenance.view_blocked(interaction):
            _tap_log(interaction, f"panel:remove:{self.game_id}",
                     self.game_id, "blocked: maintenance")
            return False
        return True

    async def _on_remove(self, interaction: discord.Interaction):
        await _remove_watch_from_list(interaction, self.game_id)


class WatchingListView(discord.ui.View):
    """The view on a watching-list message: one ❌ Remove
    button per watched game, labelled with the teams (short
    enough for Discord's 80-char button label limit).
    timeout=None so the message keeps its buttons; the
    buttons are dynamic items, so they survive a restart.
    At most 25 buttons (Discord's 5 rows x 5 components) —
    the list text still names every game."""

    def __init__(self, rows: list[dict]):
        super().__init__(timeout=None)
        for row in rows[:25]:
            label = ("❌ Remove "
                     f"{_short_team(row.get('home', '?'), 12)} v "
                     f"{_short_team(row.get('away', '?'), 12)}")
            self.add_item(RemoveWatchButton(str(row["game_id"]), label))


async def _remove_watch_from_list(interaction: discord.Interaction,
                                  game_id: str) -> None:
    """❌ Remove on a watching-list message: FULL remove like
    /unwatch — the watch goes from db + memory and its panel
    message is deleted. The list message is edited to the
    fresh list, and a short new channel message confirms
    which game was removed. A game that is already gone is
    answered plainly, never a crash."""
    game_id = str(game_id)
    channel_id = getattr(interaction, "channel_id", None)
    await interaction.response.defer()
    entry = (_watches.get((channel_id, game_id))
             if channel_id is not None else None)
    home = away = None
    if entry is not None:
        home, away = entry.get("home", ""), entry.get("away", "")
    else:
        # After a restart the registry is rebuilt from db, but
        # be safe: read the row for the name.
        try:
            rows = (await list_channel_watches(
                channel_id, include_view_only=True)
                if channel_id is not None else [])
        except Exception:
            rows = []
        row = next((r for r in rows if str(r.get("game_id")) == game_id),
                   None)
        if row is not None:
            home, away = row.get("home", ""), row.get("away", "")
    if home is None:
        # Nothing to remove — refresh the list in place and
        # say so plainly.
        try:
            rows = await list_channel_watches(
                channel_id, include_view_only=True)
            text = _watching_text(rows)
        except Exception:
            text = "No games watched in this channel."
            rows = []
        await interaction.edit_original_response(
            content=text, view=WatchingListView(rows))
        await interaction.followup.send(
            "That game is not watched in this channel any more.")
        _tap_log(interaction, "Remove", game_id,
                 "already gone")
        return
    bot = getattr(interaction, "client", None)
    if bot is not None:
        await delete_panel_message(bot, channel_id, game_id)
    await remove_watch(channel_id, game_id)
    try:
        rows = await list_channel_watches(
            channel_id, include_view_only=True)
        text = _watching_text(rows)
    except Exception:
        text = "No games watched in this channel."
        rows = []
    await interaction.edit_original_response(
        content=text, view=WatchingListView(rows))
    await interaction.followup.send(
        f"❌ Removed **{home} vs {away}** from watching in this "
        f"channel.")
    _tap_log(interaction, "Remove", game_id, "watch removed")


def _ask_game_id(game: dict) -> str | None:
    """The live game id for one grounding entry: the plain
    onex_id when present, else the "onex_<id>" id with the
    prefix stripped. None when the entry carries no numeric
    id — such a game gets no Watch button, never a guessed
    one."""
    raw = (game or {}).get("onex_id") or (game or {}).get("id") or ""
    gid = str(raw).replace("onex_", "")
    return gid if gid.isdigit() else None


class WatchGameButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"panel:watch:(?P<game_id>\d+)"):
    """One ▶ Watch button on an /ask reply — starts watching
    that game in this channel, exactly like /watch: a live
    panel that ticks every second, with simple alerts on
    goals, wickets and cards.

    A DynamicItem, not a plain button, for the same reason as
    ❌ Remove (see RemoveWatchButton): discord.py only
    dispatches a tap after a restart when the custom_id
    matches a registered template. The template is registered
    in setup_hook via bot.add_dynamic_items; the game id
    travels in the custom_id itself, so no in-memory state is
    needed to resolve it after a restart. It works from an
    ephemeral /ask reply because the tap is answered with a
    visible new channel message, not an edit of the reply."""

    def __init__(self, game_id: str, label: str = "▶ Watch"):
        button = discord.ui.Button(
            label=label, style=discord.ButtonStyle.success,
            custom_id=f"panel:watch:{game_id}")
        super().__init__(button)
        self.game_id = str(game_id)
        button.callback = self._on_watch

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["game_id"], item.label)

    async def interaction_check(self, interaction) -> bool:
        # Maintenance gate, like every other button. A blocked
        # tap logs here (it never reaches the handler).
        if await maintenance.view_blocked(interaction):
            _tap_log(interaction, f"panel:watch:{self.game_id}",
                     self.game_id, "blocked: maintenance")
            return False
        return True

    async def _on_watch(self, interaction: discord.Interaction):
        # The /watch path, shared with 🔔 Follow: an existing
        # watch is never double-started, a game that is gone
        # from the live list is answered plainly, and the
        # confirmation is a VISIBLE new channel message — the
        # /ask reply it was tapped from is ephemeral, so an
        # ephemeral answer would be easy to miss.
        await interaction.response.defer()
        game_id = self.game_id
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            _tap_log(interaction, "Watch", game_id,
                     "failed: unknown channel")
            await interaction.followup.send(
                "Could not tell which channel this is.", ephemeral=True)
            return
        existing = _watches.get((channel_id, game_id))
        if existing is not None:
            if existing.get("notify", True):
                _tap_log(interaction, "Watch", game_id,
                         "already watched")
                await interaction.followup.send(
                    "That game is already watched in this channel.")
                return
            # A panel-only watch (from /game): /watch would
            # switch alerts on, so the button does the same.
            await set_notify(channel_id, game_id, True)
            _tap_log(interaction, "Watch", game_id,
                     "notifications on")
            await interaction.followup.send(
                f"🔔 Now following **{existing['home']} vs "
                f"{existing['away']}** — alerts on goals, wickets "
                f"and cards. The live panel keeps ticking here.")
            return
        detail = await _fetch_live_detail(game_id)
        if detail is None:
            _tap_log(interaction, "Watch", game_id,
                     "failed: game no longer live")
            await interaction.followup.send(
                "That game is no longer live.", ephemeral=True)
            return
        entry = {"id": game_id, "home": detail.get("home", ""),
                 "away": detail.get("away", ""),
                 "league": detail.get("league", ""),
                 "status": detail.get("status", "")}
        started = await start_watch(channel_id, entry, DETAIL_SIMPLE)
        if not started:
            _tap_log(interaction, "Watch", game_id,
                     "failed: could not start watch")
            await interaction.followup.send(
                "Could not start watching that game.", ephemeral=True)
            return
        _tap_log(interaction, "Watch", game_id, "watch started")
        await interaction.followup.send(
            f"👀 Watching **{started['home']} vs {started['away']}** — "
            f"a live panel in this channel ticks every second; simple "
            f"alerts only when the game changes.")


class AskWatchView(discord.ui.View):
    """The view on an /ask reply that lists games: one ▶ Watch
    button per listed game, labelled with the teams, max 5
    (Discord allows 25 components, but five labelled buttons
    is the most that stays readable on a pick list). The
    buttons are dynamic items, so they survive a restart.
    timeout=None so the message keeps its buttons."""

    def __init__(self, games: list[dict]):
        super().__init__(timeout=None)
        for game in (games or [])[:5]:
            gid = _ask_game_id(game)
            if gid is None:
                continue
            label = ("▶ Watch "
                     f"{_short_team(game.get('home', '?'), 12)} v "
                     f"{_short_team(game.get('away', '?'), 12)}")
            self.add_item(WatchGameButton(gid, label))


async def _send_to_channel(bot: discord.Client, channel_id: int,
                           text: str, view: discord.ui.View | None = None) -> bool:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await asyncio.wait_for(bot.fetch_channel(channel_id), 20)
        except Exception as exc:
            log.warning("[watch] channel %s unavailable (%s)", channel_id,
                        type(exc).__name__)
            return False
    try:
        await channel.send(text, view=view,
                           allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.Forbidden:
        log.warning("[watch] no permission to send to channel %s", channel_id)
    except discord.HTTPException as exc:
        log.warning("[watch] send failed to %s (%s)", channel_id, exc)
    return False


def _detail_for_tick(raw: dict, fmt: str) -> dict | None:
    """Rich detail from one raw live entry, with the stored format
    so the cricket target can be computed without a gameInfo call
    on every tick."""
    detail = prov._onex_detail_from_raw(raw)
    if detail is None:
        return None
    if fmt:
        detail["format"] = fmt
        cricket = detail.get("cricket")
        if cricket:
            cricket.update(prov._onex_cricket_target_fmt(
                fmt, detail.get("home_score"), detail.get("away_score"),
                cricket.get("team"), cricket.get("over_str")))
    return detail


# One tick at a time: the scoreboard calls on_tick once per
# second, but a slow tick must never overlap the next one.
def _panel_edit_plan(details: dict, changed: set) -> dict[int, list]:
    """Order the panels each channel must refresh this tick:
    content changes first (a goal, a wicket, a new over — the
    snapshot moved), then the quiet ones oldest-edit-first, so
    no panel is skipped and no heartbeat jumps ahead of a real
    score change. Pure function, so the ordering is testable
    without running a tick."""
    plan: dict[int, list] = {}
    for key in details:
        plan.setdefault(key[0], []).append(key)
    for keys in plan.values():
        keys.sort(key=lambda k: (k not in changed,
                                 _watches.get(k, {}).get("last_panel_edit")
                                 or 0.0))
    return plan


def _drop_panel_worker(task: asyncio.Task, key: tuple) -> None:
    _panel_workers.pop(key, None)


def _schedule_panel_edits(bot: discord.Client, details: dict,
                          changed: set) -> None:
    """Hand this tick's panel refreshes to the per-panel workers.
    Every panel gets its edit on this tick — content changes
    first, heartbeats after — instead of the old take-turns
    design where the stagger gate let only one panel per channel
    edit per tick. The edits themselves are paced by the shared
    per-channel scheduler (see channel_edits), so the tick
    itself returns at once and the scoreboard loop keeps its 1s
    cadence. A panel that already has a worker running just gets
    fresher detail handed to it — never a second worker."""
    plan = _panel_edit_plan(details, changed)
    for channel_id, keys in plan.items():
        for key in keys:
            watch = _watches.get(key)
            if watch is None:
                continue
            watch["_pending_detail"] = details[key]
            if key not in _panel_workers:
                task = asyncio.create_task(
                    _panel_edit_worker(bot, channel_id, key))
                _panel_workers[key] = task
                task.add_done_callback(
                    lambda t, k=key: _drop_panel_worker(t, k))


async def _panel_edit_worker(bot: discord.Client, channel_id: int,
                             key: tuple) -> None:
    """One panel's edit loop. Hands each fresh detail to
    _refresh_panel — the shared scheduler (see channel_edits)
    paces the edit against every other edit in the channel, so
    this worker takes no slots and keeps no stagger of its own.
    Loops while fresh work keeps arriving (a newer tick may have
    replaced the detail while the scheduler was pacing the last
    edit) and exits when there is nothing pending, the watch is
    gone, or the edit failed (the next tick retries it). Never
    raises: a failed edit is retried on the next tick, never a
    crash. A channel-wide 429 pauses the scheduler, not this
    panel — the other panels wait out the same pause instead of
    firing into a drained bucket, which is what used to cascade."""
    game_id = key[1]
    global _panel_log_edits
    try:
        while True:
            watch = _watches.get(key)
            if watch is None:
                return
            detail = watch.get("_pending_detail")
            if detail is None:
                return
            edited = await _refresh_panel(bot, channel_id, game_id,
                                          watch, detail)
            if not edited:
                # Nothing changed, superseded, or failed — leave
                # the detail pending; the next tick retries it.
                return
            _panel_log_edits += 1
            # Only clear the pending detail if it is still the
            # one we just edited — a newer tick may have replaced
            # it while we waited; then loop and edit the fresh one.
            if watch.get("_pending_detail") is detail:
                watch["_pending_detail"] = None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[watch] panel worker failed for %s (%s)",
                    game_id, type(exc).__name__)


def _maybe_log_panel_cadence() -> None:
    """Once a minute, log how many panels are registered and how
    many tick edits actually landed in the last 60s — the number
    the VM journal can prove the refresh cadence from. The
    divide is about ATTEMPTS: N panels share the channel's
    edit budget evenly, so the total ATTEMPTED stays ~60/min
    whatever N is — but the LANDED number is Discord's call.
    Honest numbers (VM journal, 18 Nov 2026): 1 panel landed
    only ~27–33 edits/min live, never 60 — the 60/min figure
    is a fake-bucket test number, not a live promise. A low
    landed count with zero 429s means Discord's real budget
    is below the 5-per-5s model, not that the bot stopped
    trying: every live panel still attempts an edit on every
    tick it is due (gap floor 1.0s, heartbeat 0.9s)."""
    global _panel_log_window_start, _panel_log_edits
    if not _watches:
        _panel_log_window_start = 0.0
        _panel_log_edits = 0
        return
    now = time.monotonic()
    if _panel_log_window_start == 0.0:
        _panel_log_window_start = now
        return
    if now - _panel_log_window_start < _PANEL_LOG_EVERY_S:
        return
    log.info("[watch] panels: %d registered, %d edits in the last 60s",
             len(_watches), _panel_log_edits)
    _panel_log_window_start = now
    _panel_log_edits = 0


_tick_lock = asyncio.Lock()

# Momentum snapshots: one stat sample per watched game per 30s.
_MOMENTUM_EVERY_S = 30.0
_last_momentum_snapshot: dict[tuple[int, str], float] = {}


async def on_tick(bot: discord.Client,
                  raw_games: list[dict] | None) -> None:
    """One shared tick: diff every watched game against its stored
    snapshot and send ONLY on a real change. Called from
    scoreboard_loop — never raises, never sends duplicates, and
    never re-sends an event recorded in the history table (so a
    restart cannot repeat an old alert). raw_games=None means the
    tick's fetch failed: the watches are left untouched, since a
    failed fetch says nothing about any game."""
    # Maintenance mode: silent — no panels, no alerts, no new
    # watches (the scoreboard loop also checks, this covers a
    # direct tick).
    if await maintenance.is_on():
        return
    async with _tick_lock:
        await _on_tick_locked(bot, raw_games)


async def _on_tick_locked(bot: discord.Client,
                          raw_games: list[dict] | None) -> None:
    # Barça auto-watch rides this same tick — no second poller.
    # It runs before the early return below so it also fires when
    # no game is watched yet.
    await maybe_auto_watch_barca()
    await maybe_team_follow_check()
    _maybe_log_panel_cadence()
    if not _watches:
        return
    by_id: dict[str, dict] = {}
    for g in raw_games or []:
        gid = str(g.get("id") or g.get("mainGameId") or "")
        if gid:
            by_id[gid] = g
    # This loop collects each live game's fresh detail (and
    # detects its events); the panel refreshes themselves happen
    # in _schedule_panel_edits below, via the per-panel workers
    # — every panel gets its edit on this tick, content changes
    # first, spaced 1s apart per channel.
    details: dict[tuple, dict] = {}
    # Keys whose snapshot moved this tick (a goal, a wicket, a
    # new over — and a football clock, which advances every
    # second): their panels go first in the edit order.
    changed: set = set()
    for key, watch in list(_watches.items()):
        channel_id, game_id = key
        try:
            raw = by_id.get(game_id)
            if raw is None:
                if raw_games is None:
                    # The tick's fetch failed — an empty list here
                    # says nothing about the game, so this tick
                    # must never count toward ending the watch.
                    continue
                # Absent from a live list that DID arrive: the
                # games1x2 feed caps at 50 entries, so a game can
                # flicker off the list for minutes while still
                # live. Hold the panel and end the watch only
                # after MISSING_END_AFTER_S of continuous absence
                # (an explicit terminal status ends it at once,
                # handled where the detail lands below).
                now = time.monotonic()
                if not watch.get("missing_since"):
                    watch["missing_since"] = now
                    watch["missing"] = 1
                    log.info("[watch] %s vs %s absent from live list — "
                             "holding panel (ends after %.0fs away)",
                             watch["home"], watch["away"],
                             MISSING_END_AFTER_S)
                else:
                    watch["missing"] = watch.get("missing", 0) + 1
                if now - watch["missing_since"] >= MISSING_END_AFTER_S:
                    # The panel (if one was posted) shows the ended
                    # state; the message below is the notification
                    # that the watch itself has ended. Priority: a
                    # one-off final state, it should land at once.
                    await _refresh_panel(
                        bot, channel_id, game_id, watch,
                        text=(f"🏁 **{watch['home']} vs {watch['away']}**"
                              f" — no longer live\nGame ID: {game_id}"),
                        post_if_missing=False, priority=True)
                    # A view-only /game panel ends silently — its
                    # panel already says the game is over.
                    if watch.get("notify", True):
                        await _send_to_channel(
                            bot, channel_id,
                            f"🏁 **{watch['home']} vs {watch['away']}** is no longer live — "
                            f"the watch has ended.")
                    await history.record_match_result(
                        game_id, watch["home"], watch["away"],
                        watch.get("league", ""), "",
                        (watch.get("snapshot") or {}).get("home_score"),
                        (watch.get("snapshot") or {}).get("away_score"),
                        "ended")
                    await remove_watch(channel_id, game_id)
                    log.info("[watch] ended %s vs %s (absent %.0fs)",
                             watch["home"], watch["away"],
                             now - watch["missing_since"])
                continue
            watch["missing"] = 0
            if watch.get("missing_since"):
                # Back in the live list after an absence: the SAME
                # panel resumes below — no /game needed.
                log.info("[watch] %s vs %s back in live list after %.0fs — "
                         "same panel resumes",
                         watch["home"], watch["away"],
                         time.monotonic() - watch["missing_since"])
                watch["missing_since"] = 0.0
            detail = _detail_for_tick(raw, watch.get("fmt") or "")
            if detail is None:
                continue
            # Momentum sample: real stat snapshots, one per 30s.
            if detail.get("sport") == "Football" and detail.get("stats"):
                last = _last_momentum_snapshot.get(key, 0.0)
                now = time.monotonic()
                if now - last >= _MOMENTUM_EVERY_S:
                    _last_momentum_snapshot[key] = now
                    await history.record_stat_snapshot(
                        game_id, {str(s.get("name")): (str(s.get("home", "")),
                                                       str(s.get("away", "")))
                                  for s in detail["stats"]})
            snapshot = snapshot_from_detail(detail)
            snapshot["_home"], snapshot["_away"] = detail.get("home", ""), detail.get("away", "")
            old = watch.get("snapshot") or {}
            old = dict(old)
            old.setdefault("_home", watch.get("home", ""))
            old.setdefault("_away", watch.get("away", ""))
            allowed, paused, detail_override = await _prefs_for(channel_id, game_id)
            level = detail_override or watch.get("detail") or DETAIL_SIMPLE
            events = detect_events(old, snapshot, level, game_id)
            status = str(snapshot.get("status") or "")
            terminal = TERMINAL_STATUSES.get(status.strip().lower())
            is_terminal = status == "FT" or terminal is not None
            # Prefs filter: paused stops everything; an event-kind
            # list drops the kinds the user turned off. Terminal
            # events (full-time, postponed...) always pass the
            # kind filter — the watch is ending and the user needs
            # to know — but a paused watch stays silent.
            sendable = []
            for ev in events:
                if paused:
                    continue
                if allowed is not None and ev["kind"] not in allowed \
                        and ev["kind"] not in ("ft", "postponed",
                                               "suspended", "abandoned",
                                               "cancelled"):
                    continue
                if await history.event_seen(ev["key"]):
                    continue
                sendable.append(ev)
            if sendable and watch.get("notify", True):
                text = "\n".join(ev["text"] for ev in sendable)
                # Automatic alert stays exactly as before: event line(s)
                # PLUS the full stats table for advanced watches. The
                # buttons below are only for on-demand detail / live.
                if level == DETAIL_ADVANCED:
                    table = format_stats_table(detail.get("stats"))
                    if table:
                        text += "\n\n**Match stats**\n" + "\n".join(table)
                # The buttons resolve the game from the message, so the
                # alert carries its id like the detail card does.
                text += f"\nGame ID: {game_id}"
                if await _send_to_channel(
                        bot, channel_id, text,
                        view=WatchAlertView(
                            live_count=live_watch_count(channel_id))):
                    for ev in sendable:
                        await history.record_sent_event(
                            ev["key"], game_id, channel_id, ev["kind"], ev["text"])
                        await history.record_notification(
                            game_id, channel_id, ev["kind"], ev["text"], True)
                    log.info("[watch] alert for %s vs %s: %s",
                             watch["home"], watch["away"], sendable[0]["text"][:80])
                else:
                    # Send failed: do not record, do not advance the
                    # snapshot — the next tick retries the same events.
                    continue
            # Live panel: routine changes (clock, score, overs) only
            # ever edit the panel in place — never a new message.
            # A paused watch stays silent, panel included. The
            # edit itself happens in the panel's worker (see
            # _schedule_panel_edits below) — this loop only hands
            # it the fresh detail and notes whether the game
            # actually changed, so content changes go first.
            if not paused:
                details[key] = detail
                if snapshot != old:
                    changed.add(key)
            watch["snapshot"] = snapshot
            await db.update_watch_snapshot(channel_id, game_id, snapshot)
            if is_terminal:
                # Final state first, in place — the watch is
                # removed below, so its worker would never run.
                # Priority: a one-off final state, it should land
                # at once rather than behind queued heartbeats.
                # (Same inline edit as before the worker rework.)
                await _refresh_panel(bot, channel_id, game_id, watch,
                                     detail, heartbeat=False,
                                     priority=True)
                await history.record_match_result(
                    game_id, watch["home"], watch["away"],
                    watch.get("league", ""), str(detail.get("sport") or ""),
                    snapshot.get("home_score"), snapshot.get("away_score"),
                    "FT" if status == "FT" else status)
                await remove_watch(channel_id, game_id)
                log.info("[watch] ended %s vs %s (%s)",
                         watch["home"], watch["away"],
                         "full-time" if status == "FT" else status)
        except Exception as exc:
            log.warning("[watch] tick failed for %s (%s)", game_id,
                        type(exc).__name__)

    # Panel refreshes: every panel gets its edit on this tick —
    # content changes first, heartbeats after — via the per-panel
    # workers. The edits themselves are paced by the shared
    # per-channel scheduler (see channel_edits), which also paces
    # the scoreboard's edits in the same channel.
    # (The old phase 2 let only one panel per channel edit per
    # tick, so four panels in one channel took turns and each
    # panel's Updated footer only advanced about every 4s.)
    # NPL cricket panels: one shared scorecard fetch for the
    # tick (cached 10s in the provider) so Advanced/analytics
    # panels show striker, bowler and partnership live. The
    # fetch happens AFTER the event/snapshot loop above, so the
    # scorecard never enters a snapshot or an event.
    npl_keys = [k for k, d in details.items()
                if d.get("league") == "Nepal Premier League"
                and d.get("cricket") is not None]
    if npl_keys:
        try:
            card = await prov.onex_npl_scorecard(
                details[npl_keys[0]].get("home", ""),
                details[npl_keys[0]].get("away", ""))
        except Exception:
            card = None
        if card:
            for k in npl_keys:
                details[k]["cricket"]["scorecard"] = card
    _schedule_panel_edits(bot, details, changed)
