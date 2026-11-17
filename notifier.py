"""
Production-grade quad-provider live poller.

Architecture:
  - Polls ESPN + SofaScore + API-Football + 1xBet concurrently every POLL_INTERVAL seconds
  - 1xBet covers featured live games only; its events merge into tracked
    matches like ESPN's, and games no other provider tracks get their own
    state (see the 1xBet section in _tick)
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
from datetime import date
import time
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


# ── Live state ─────────────────────────────────────────────────────────────────────
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


def _score_embed(match: dict) -> discord.Embed:
    home_score = match.get("home_score") if match.get("home_score") is not None else "—"
    away_score = match.get("away_score") if match.get("away_score") is not None else "—"
    return discord.Embed(
        title="⚽ Score update",
        description=(f"**{match['home']}** {home_score} — {away_score} **{match['away']}**\n"
                     f"{match.get('league', 'Football')} · {match.get('status', 'Live')}"),
        color=0x2196F3,
    )


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

    # Public source path: TheSportsDB is reachable from this Azure VM and
    # requires no user key.  It powers the schedule commands and the alerts.
    batches = await asyncio.gather(
        *(prov.sportsdb_day(league_id, date.today().isoformat()) for league_id in active_leagues),
        return_exceptions=True,
    )
    free_matches = [match for batch in batches if isinstance(batch, list) for match in batch]
    seen_free_ids: set[str] = set()
    for match in free_matches:
        if not prov.sportsdb_is_live(match) and not match.get("finished"):
            continue
        state_key = str(match["id"])
        seen_free_ids.add(state_key)
        channels = _channels_for(match["league_id"])
        previous = _state.get(state_key)
        if previous is None:
            _state[state_key] = {
                "status": match.get("status"),
                "home": match.get("home_score"),
                "away": match.get("away_score"),
                "match": match,
            }
            if prov.sportsdb_is_live(match):
                await _send(bot, channels, embed=alert_embed("kickoff", match))
            continue

        score_changed = (
            previous.get("home") != match.get("home_score")
            or previous.get("away") != match.get("away_score")
        )
        previous.update(status=match.get("status"), home=match.get("home_score"),
                        away=match.get("away_score"), match=match)
        if score_changed:
            await _send(bot, channels, embed=_score_embed(match))
        if match.get("finished"):
            await _send(bot, channels, embed=alert_embed("ft", match))
            _state.pop(state_key, None)

    for state_key in list(_state):
        if isinstance(state_key, str) and state_key.startswith("sportsdb_") and state_key not in seen_free_ids:
            _state.pop(state_key, None)
    return

    # ── Build ESPN fetch tasks for every subscribed league that has a slug ──
    espn_slugs: list[tuple[int, str]] = [
        (apf_lid, slug)
        for apf_lid, slug in ESPN_LEAGUE_MAP.items()
        if apf_lid in active_leagues
    ]

    # ── Fetch all four providers concurrently ────────────────────────────────
    results = await asyncio.gather(
        prov.sofa_live_matches(),
        prov.apf_live(),
        prov.onex_live_matches(),
        *[prov.espn_live(slug, apf_lid) for apf_lid, slug in espn_slugs],
        return_exceptions=True,
    )

    sofa_matches = results[0]
    apf_matches  = results[1]
    onex_matches = results[2]
    espn_results = results[3:]

    if isinstance(sofa_matches, Exception):
        log.warning("[notifier] SofaScore fetch failed: %s", sofa_matches)
        sofa_matches = []
    if isinstance(apf_matches, Exception):
        log.warning("[notifier] API-Football fetch failed: %s", apf_matches)
        apf_matches = []
    if isinstance(onex_matches, Exception):
        log.warning("[notifier] 1xBet fetch failed: %s", onex_matches)
        onex_matches = []

    # ── Build ESPN events index keyed by "home_norm|away_norm" ─────────────────
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

    # ── Build 1xBet events index (featured games in subscribed leagues) ───────
    # chronoLine is fetched per featured game; featured games are few, so
    # this is a handful of calls per tick at most. A failed fetch just
    # means no 1xBet events for that game this tick.
    onex_events_index: dict[str, list[dict]] = {}
    onex_tasks: list = []
    onex_keys: list[str] = []
    for om in onex_matches:
        if om["league_id"] in active_leagues:
            key = f"{om['home_norm']}|{om['away_norm']}"
            onex_keys.append(key)
            onex_tasks.append(prov.onex_events(om["onex_id"], om["home"], om["away"]))
    if onex_tasks:
        onex_results = await asyncio.gather(*onex_tasks, return_exceptions=True)
        for key, res in zip(onex_keys, onex_results):
            if isinstance(res, Exception):
                log.warning("[notifier] 1xBet events failed for %s: %s", key, res)
                continue
            onex_events_index[key] = res or []

    # ── Index APF matches for cross-linking ────────────────────────────────
    apf_index: dict[str, dict] = {
        f"{m['home_norm']}|{m['away_norm']}": m for m in apf_matches
    }

    seen_sofa_ids: set[int] = set()

    # ── Process SofaScore matches (primary source) ───────────────────────────
    for sm in sofa_matches:
        apf_lid = sm["league_id"]
        if apf_lid not in active_leagues:
            continue

        sofa_id = sm["sofa_id"]
        seen_sofa_ids.add(sofa_id)
        channels = _channels_for(apf_lid)

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

        espn_evs = espn_events_index.get(f"{sm['home_norm']}|{sm['away_norm']}", [])
        onex_evs = onex_events_index.get(f"{sm['home_norm']}|{sm['away_norm']}", [])

        fps = prev["fingerprints"]
        for ev in list(espn_evs) + list(onex_evs) + list(sofa_events) + list(apf_events):
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
        prev["match"] = am
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
        onex_evs = onex_events_index.get(f"{am['home_norm']}|{am['away_norm']}", [])
        try:
            apf_events_list = await prov.apf_events(apf_fid)
        except Exception as exc:
            log.warning("[notifier] APF events error for fixture %s: %s", apf_fid, exc)
            apf_events_list = []

        fps = prev["fingerprints"]
        for ev in list(espn_evs) + list(onex_evs) + list(apf_events_list):
            fp = ev.get("fingerprint", "")
            if not fp or fp in fps:
                continue
            fps.add(fp)
            await _send(bot, channels, embed=event_embed(ev, am))

    # ── Process 1xBet-only matches (featured games no other provider tracks) ──
    # Same shape as the APF-only section: kickoff alert on first sighting,
    # status alerts on transitions, events from the 1xBet + ESPN indexes.
    # A game that stops being featured keeps its state (it may still be
    # live) — stale onex entries are pruned by age in the cleanup below.
    for om in onex_matches:
        apf_lid = om["league_id"]
        if apf_lid not in active_leagues:
            continue
        if any(
            names_match(s["match"]["home"], om["home"]) and
            names_match(s["match"]["away"], om["away"])
            for s in _state.values() if "match" in s
        ):
            continue

        onex_id   = om["onex_id"]
        state_key = f"onex_{onex_id}"
        channels  = _channels_for(apf_lid)

        if state_key not in _state:
            _state[state_key] = {"fingerprints": set(), "status": om["status"],
                                 "apf_id": None, "match": om,
                                 "last_seen": time.time()}
            await _send(bot, channels, embed=alert_embed("kickoff", om))
            log.info("[notifier] Kickoff [1xBet]: %s vs %s", om["home"], om["away"])
            continue

        prev = _state[state_key]
        prev["match"] = om
        prev["last_seen"] = time.time()

        old_status, new_status = prev["status"], om["status"]
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
                await _send(bot, channels, embed=alert_embed(alert_type, om))
                log.info("[notifier] Status %s→%s [1xBet]: %s vs %s",
                         old_status, new_status, om["home"], om["away"])
            if om.get("finished"):
                _state.pop(state_key, None)
                continue

        espn_evs = espn_events_index.get(f"{om['home_norm']}|{om['away_norm']}", [])
        onex_evs = onex_events_index.get(f"{om['home_norm']}|{om['away_norm']}", [])
        fps = prev["fingerprints"]
        for ev in list(espn_evs) + list(onex_evs):
            fp = ev.get("fingerprint", "")
            if not fp or fp in fps:
                continue
            fps.add(fp)
            await _send(bot, channels, embed=event_embed(ev, om))
            log.info("[notifier] Event [%s] %s' %s (%s)",
                     ev["provider"], ev["minute"], ev["player"], ev["detail"])

    # ── Cleanup stale state entries ───────────────────────────────────────────
    now = time.time()
    seen_apf_ids = {m["apf_id"] for m in apf_matches if m.get("apf_id")}
    for key in list(_state):
        if isinstance(key, int) and key not in seen_sofa_ids:
            if _state[key].get("match", {}).get("finished", False):
                _state.pop(key, None)
        elif isinstance(key, str):
            if key.startswith("onex_"):
                # A featured game can vanish from 1xBet's list while still
                # live (it just stopped being featured), so never drop it
                # for absence alone — prune only after 3h unseen.
                if now - _state[key].get("last_seen", 0) > 3 * 3600:
                    _state.pop(key, None)
                continue
            apf_fid = _state[key].get("apf_id")
            if apf_fid and apf_fid not in seen_apf_ids:
                if _state[key].get("match", {}).get("finished", False):
                    _state.pop(key, None)
