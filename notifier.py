"""
Production-grade tri-provider live poller.

Architecture:
  - Polls ESPN + SofaScore + API-Football concurrently every POLL_INTERVAL seconds
  - ESPN is fetched first within the merged event list because it bundles events
    with the scoreboard (one HTTP call per league) — no per-fixture follow-up.
    This makes it structurally the fastest source, so it claims fingerprints
    before the slower per-match requests from SofaScore / API-Football arrive.
  - Only tracks leagues that have at least one subscriber (skips unsubscribed)
  - Always polls events for every tracked fixture (not just on score change)
  - Event fingerprint = SHA-1 of (team_norm + minute + category) — provider-agnostic,
    so the same real-world event reported by multiple sources collapses to one alert
  - First provider to report an event wins; duplicates from other sources are dropped
  - Status transitions (HT, FT, ET, PST, etc.) generate typed alert embeds
  - Cross-links SofaScore ↔ API-Football matches by normalized team names
  - Automatically removes state for finished matches to keep RAM low
  - Never crashes: every exception is caught and logged
"""
import asyncio
import logging
from typing import Any

import discord

import providers as prov
from config import POLL_INTERVAL, ESPN_LEAGUE_MAP
from embeds import event_embed, alert_embed
from normalizer import names_match

log = logging.getLogger("notifier")

# { channel_id: set(apf_league_id) } — populated from db on startup, updated by commands
subscriptions: dict[int, set[int]] = {}


async def _no_events() -> list:
    """Stand-in awaitable used when there's no APF fixture ID to query yet."""
    return []


# ── Live state ─────────────────────────────────────────────────────────────
# { sofa_id | "apf_{id}": FixtureState }
_state: dict[Any, dict[str, Any]] = {}


def _subscribed_apf_leagues() -> set[int]:
    result: set[int] = set()
    for leagues in subscriptions.values():
        result |= leagues
    return result


def _channels_for(apf_league_id: int) -> list[int]:
    return [cid for cid, leagues in subscriptions.items() if apf_league_id in leagues]


async def _send(bot: discord.Client, channel_ids: list[int], **kwargs) -> None:
    for cid in channel_ids:
        ch = bot.get_channel(cid)
        if ch:
            try:
                await ch.send(**kwargs)
            except discord.Forbidden:
                log.warning("[notifier] No permission to send to channel %d", cid)
            except Exception as exc:
                log.error("[notifier] Send error to %d: %s", cid, exc)


async def poll_loop(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    log.info("[notifier] Poll loop started (interval=%ds)", POLL_INTERVAL)
    while not bot.is_closed():
        try:
            await _tick(bot)
        except Exception as exc:
            log.error("[notifier] Tick error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)


async def _tick(bot: discord.Client) -> None:
    active_leagues = _subscribed_apf_leagues()
    if not active_leagues:
        return

    # ── Build ESPN fetch tasks for every subscribed league that has a slug ──
    # espn_live() returns (matches, events_by_home_norm|away_norm) in one HTTP
    # call per league — no per-fixture follow-up needed.
    espn_slugs: list[tuple[int, str]] = [
        (apf_lid, slug)
        for apf_lid, slug in ESPN_LEAGUE_MAP.items()
        if apf_lid in active_leagues
    ]

    # ── Fetch all three providers concurrently ──────────────────────────────
    results = await asyncio.gather(
        prov.sofa_live_matches(),
        prov.apf_live(),
        *[prov.espn_live(slug, apf_lid) for apf_lid, slug in espn_slugs],
        return_exceptions=True,
    )

    sofa_matches = results[0]
    apf_matches  = results[1]
    espn_results = results[2:]

    if isinstance(sofa_matches, Exception):
        log.warning("[notifier] SofaScore fetch failed: %s", sofa_matches)
        sofa_matches = []
    if isinstance(apf_matches, Exception):
        log.warning("[notifier] API-Football fetch failed: %s", apf_matches)
        apf_matches = []

    # ── Build ESPN events index keyed by "home_norm|away_norm" ─────────────
    # This is what lets us look up pre-fetched ESPN events per match without
    # any extra HTTP calls during the per-fixture loop below.
    espn_events_index: dict[str, list[dict]] = {}
    for i, (apf_lid, slug) in enumerate(espn_slugs):
        result = espn_results[i]
        if isinstance(result, Exception):
            log.warning("[notifier] ESPN fetch failed for %s: %s", slug, result)
            continue
        _espn_matches, events_by_key = result
        espn_events_index.update(events_by_key)

    # ── Index APF matches for cross-linking ────────────────────────────────
    apf_index: dict[str, dict] = {
        f"{m['home_norm']}|{m['away_norm']}": m for m in apf_matches
    }

    seen_sofa_ids: set[int] = set()

    # ── Process SofaScore matches (primary source) ─────────────────────────
    for sm in sofa_matches:
        apf_lid = sm["league_id"]
        if apf_lid not in active_leagues:
            continue

        sofa_id = sm["sofa_id"]
        seen_sofa_ids.add(sofa_id)
        channels = _channels_for(apf_lid)

        # Cross-link to APF match for stadium/referee metadata
        apf_m = apf_index.get(f"{sm['home_norm']}|{sm['away_norm']}")
        if apf_m:
            sm["apf_id"]  = apf_m["apf_id"]
            sm["stadium"] = sm["stadium"] or apf_m.get("stadium", "")
            sm["referee"] = sm["referee"] or apf_m.get("referee", "")

        if sofa_id not in _state:
            _state[sofa_id] = {
                "fingerprints": set(),
                "status":       sm["status"],
                "apf_id":       sm.get("apf_id"),
                "match":        sm,
            }
            await _send(bot, channels, embed=alert_embed("kickoff", sm))
            log.info("[notifier] Kickoff: %s vs %s", sm["home"], sm["away"])
            continue

        prev = _state[sofa_id]
        prev["match"] = sm

        # ── Status transitions ──────────────────────────────────────────
        old_status, new_status = prev["status"], sm["status"]
        if new_status != old_status:
            prev["status"] = new_status
            status_map = {
                "HT": "ht",  "2H": "2h",    "ET":   "et",
                "PEN": "pens", "FT": "ft",  "AET":  "ft",
                "PST": "pst", "CANC": "canc", "ABD": "abd",
                "INT": "int",
            }
            alert_type = status_map.get(new_status)
            if alert_type:
                await _send(bot, channels, embed=alert_embed(alert_type, sm))
                log.info("[notifier] Status %s→%s: %s vs %s",
                         old_status, new_status, sm["home"], sm["away"])
            if sm.get("finished"):
                _state.pop(sofa_id, None)
                continue

        # ── Fetch SofaScore + APF events concurrently ──────────────────
        apf_id = prev.get("apf_id") or sm.get("apf_id")
        sofa_events, apf_events = await asyncio.gather(
            prov.sofa_incidents(sofa_id, sm["home"], sm["away"]),
            prov.apf_events(apf_id) if apf_id else _no_events(),
            return_exceptions=True,
        )
        if isinstance(sofa_events, Exception):
            sofa_events = []
        if isinstance(apf_events, Exception):
            apf_events = []

        # ESPN events for this match pair (already in memory — no extra HTTP call)
        espn_evs = espn_events_index.get(f"{sm['home_norm']}|{sm['away_norm']}", [])

        # ── Merge: ESPN first so it claims fingerprints before slower sources ──
        fps = prev["fingerprints"]
        for ev in list(espn_evs) + list(sofa_events) + list(apf_events):
            fp = ev.get("fingerprint", "")
            if not fp or fp in fps:
                continue
            fps.add(fp)
            await _send(bot, channels, embed=event_embed(ev, sm))
            log.info("[notifier] Event [%s] %s' %s (%s)",
                     ev["provider"], ev["minute"], ev["player"], ev["detail"])

    # ── Process APF-only matches (not seen via SofaScore) ──────────────────
    for am in apf_matches:
        apf_lid = am["league_id"]
        if apf_lid not in active_leagues:
            continue
        # Skip if already handled via SofaScore
        if any(
            names_match(s["match"]["home"], am["home"]) and
            names_match(s["match"]["away"], am["away"])
            for s in _state.values() if "match" in s
        ):
            continue

        apf_fid   = am["apf_id"]
        state_key = f"apf_{apf_fid}"
        channels  = _channels_for(apf_lid)

        if state_key not in _state:
            _state[state_key] = {"fingerprints": set(), "status": am["status"],
                                  "apf_id": apf_fid, "match": am}
            await _send(bot, channels, embed=alert_embed("kickoff", am))
            continue

        prev       = _state[state_key]
        old_status = prev["status"]
        new_status = am["status"]
        if new_status != old_status:
            prev["status"] = new_status
            status_map = {"HT": "ht", "FT": "ft", "AET": "ft", "ET": "et",
                          "PEN": "pens", "PST": "pst", "CANC": "canc", "ABD": "abd"}
            alert_type = status_map.get(new_status)
            if alert_type:
                await _send(bot, channels, embed=alert_embed(alert_type, am))
            if am.get("finished"):
                _state.pop(state_key, None)
                continue

        espn_evs = espn_events_index.get(f"{am['home_norm']}|{am['away_norm']}", [])
        apf_events = await prov.apf_events(apf_fid)
        if isinstance(apf_events, Exception):
            apf_events = []

        fps = prev["fingerprints"]
        for ev in list(espn_evs) + list(apf_events):
            fp = ev.get("fingerprint", "")
            if not fp or fp in fps:
                continue
            fps.add(fp)
            await _send(bot, channels, embed=event_embed(ev, am))

    # ── Cleanup stale state entries ─────────────────────────────────────────
    for key in list(_state):
        if isinstance(key, int) and key not in seen_sofa_ids:
            if _state[key].get("match", {}).get("finished", False):
                _state.pop(key, None)
