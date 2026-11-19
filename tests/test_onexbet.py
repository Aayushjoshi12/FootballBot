"""1xBet provider tests — pure parsing against captured fixtures, no network.

Fixtures are real responses captured from the user's Chrome via the Muse
Network Inspector on 17 Nov 2026 (lite mirror 1xlite-6229051.com):
  - top_games_by_stat.json      — 5 featured live games (Iceland-Luxembourg 3-0, 2nd half)
  - chrono_line_771495784.json  — Iceland vs Luxembourg timeline (goals, corners, subs, penalty)
  - game_info_771495784.json    — per-match detail (no scores — scores live in topGamesByStat)
  - left_menu_sports.json       — full live menu (reference; 41 live football games,
                                  only the expanded champ inlines its games)

Run:  python3 -m unittest tests.test_onexbet -v
from the footballbot directory.
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers as prov
from providers import (
    _onex_league_id,
    _onex_norm_event,
    _onex_norm_match,
    _onex_parse_minute,
    _onex_status,
    canonical_fingerprint,
    onex_events,
    onex_live_matches,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "onexbet"


def _load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class TestLeagueMapping(unittest.TestCase):
    def test_tracked_leagues(self):
        self.assertEqual(_onex_league_id("England. Premier League"), 39)
        self.assertEqual(_onex_league_id("Premier League"), 39)
        self.assertEqual(_onex_league_id("Spain. La Liga"), 140)
        self.assertEqual(_onex_league_id("Italy. Serie A"), 135)
        self.assertEqual(_onex_league_id("Germany. Bundesliga"), 78)
        self.assertEqual(_onex_league_id("France. Ligue 1"), 61)
        self.assertEqual(_onex_league_id("UEFA Champions League"), 2)
        self.assertEqual(_onex_league_id("UEFA Europa League"), 3)
        # Conference League must win over the Europa League alias.
        self.assertEqual(_onex_league_id("UEFA Europa Conference League"), 848)
        self.assertEqual(_onex_league_id("FIFA Club World Cup"), 15)

    def test_excluded_variants(self):
        self.assertEqual(_onex_league_id("Spain. La Liga 2"), 0)
        self.assertEqual(_onex_league_id("Germany. 2. Bundesliga"), 0)
        self.assertEqual(_onex_league_id("Germany. Bundesliga. Women"), 0)
        self.assertEqual(_onex_league_id("UEFA European U21 Championship Qualification"), 0)
        self.assertEqual(_onex_league_id("UEFA Nations League"), 0)
        self.assertEqual(_onex_league_id("Friendlies. National Teams"), 0)
        self.assertEqual(_onex_league_id(""), 0)


class TestStatus(unittest.TestCase):
    def test_second_half_running(self):
        status, minute = _onex_status({
            "currentPeriod": 2, "currentPeriodName": "2nd half",
            "timer": {"timeSec": 3891, "timeRun": True},
            "statusLineStr": "64 minutes",
        })
        self.assertEqual((status, minute), ("2H", 64))

    def test_second_half_paused_clock(self):
        # One fixture game has timeRun False in the 2nd half — still 2H.
        status, minute = _onex_status({
            "currentPeriod": 2, "currentPeriodName": "2nd half",
            "timer": {"timeSec": 3960, "timeRun": False},
            "statusLineStr": "66 minutes",
        })
        self.assertEqual((status, minute), ("2H", 66))

    def test_first_half(self):
        status, minute = _onex_status({
            "currentPeriod": 1, "currentPeriodName": "1st half",
            "timer": {"timeSec": 1560, "timeRun": True},
            "statusLineStr": "26 minutes",
        })
        self.assertEqual((status, minute), ("1H", 26))

    def test_half_time_line(self):
        status, minute = _onex_status({
            "currentPeriod": 1, "currentPeriodName": "1st half",
            "timer": {"timeSec": 2700, "timeRun": False},
            "statusLineStr": "Half time",
        })
        self.assertEqual((status, minute), ("HT", 45))

    def test_no_scores(self):
        self.assertEqual(_onex_status({}), ("?", None))
        self.assertEqual(_onex_status(None), ("?", None))


class TestMinuteParsing(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_onex_parse_minute("53'"), (53, None))

    def test_added_time(self):
        self.assertEqual(_onex_parse_minute("45+2'"), (45, 2))

    def test_garbage(self):
        self.assertEqual(_onex_parse_minute(""), (None, None))
        self.assertEqual(_onex_parse_minute("HT"), (None, None))


class TestMatchNorm(unittest.TestCase):
    def setUp(self):
        self.games = _load("top_games_by_stat.json")

    def test_all_five_parse(self):
        matches = [_onex_norm_match(g) for g in self.games]
        self.assertEqual(len(matches), 5)
        self.assertTrue(all(m is not None for m in matches))

    def test_iceland_luxembourg(self):
        m = _onex_norm_match(self.games[0])
        self.assertEqual(m["id"], "onex_771495784")
        self.assertEqual(m["onex_id"], 771495784)
        self.assertEqual(m["home"], "Iceland")
        self.assertEqual(m["away"], "Luxembourg")
        self.assertEqual(m["home_score"], 3)
        self.assertEqual(m["away_score"], 0)
        self.assertEqual(m["status"], "2H")
        self.assertEqual(m["minute"], 64)
        self.assertEqual(m["league"], "UEFA Nations League")
        self.assertEqual(m["league_id"], 0)  # not a tracked league
        self.assertEqual(m["provider"], "onexbet")
        self.assertEqual(m["stadium"], "Laugardalsvollur (Reykjavik)")
        self.assertEqual(m["date"], "2026-11-16")
        self.assertFalse(m["finished"])
        self.assertTrue(m["home_logo"].endswith("/sfiles/logo_teams/12647.png"))

    def test_missing_teams_rejected(self):
        self.assertIsNone(_onex_norm_match({"id": 1}))
        self.assertIsNone(_onex_norm_match({"opponent1": {"fullName": "A"}}))


class TestEventNorm(unittest.TestCase):
    def setUp(self):
        self.chrono = _load("chrono_line_771495784.json")

    def _all_raw(self):
        for period in self.chrono["chronoLine"]:
            yield from period["events"]

    def test_goals_and_subs_only(self):
        # 2 goals + 1 penalty goal + 2 subs = 5 bot events; 6 corners skipped.
        events = [_onex_norm_event(e, "Iceland", "Luxembourg")
                  for e in self._all_raw()]
        events = [e for e in events if e is not None]
        self.assertEqual(len(events), 5)
        kinds = sorted(e["type"] for e in events)
        self.assertEqual(kinds, ["goal", "goal", "goal", "sub", "sub"])

    def test_goal_50(self):
        ev = next(e for e in self._all_raw()
                  if e.get("eventType") == 200 and e.get("minute") == "50'")
        n = _onex_norm_event(ev, "Iceland", "Luxembourg")
        self.assertEqual(n["detail"], "Goal")
        self.assertEqual(n["icon"], "⚽")
        self.assertEqual(n["player"], "Mikael Egill Ellertsson")
        self.assertEqual(n["assist"], "Orri Steinn Oskarsson")
        self.assertEqual(n["team"], "Iceland")
        self.assertEqual(n["minute"], "50")
        self.assertEqual(n["minute_int"], 50)
        self.assertEqual(n["fingerprint"],
                         canonical_fingerprint("iceland", 50, "goal"))

    def test_penalty_goal_37(self):
        ev = next(e for e in self._all_raw() if e.get("eventType") == 218)
        n = _onex_norm_event(ev, "Iceland", "Luxembourg")
        self.assertEqual(n["detail"], "Penalty Goal")
        self.assertEqual(n["icon"], "⚽🎯")
        self.assertEqual(n["type"], "goal")
        self.assertEqual(n["player"], "Andri Gudjohnsen")

    def test_substitution_direction(self):
        # Andri Gudjohnsen scored at 37', so he was already on the pitch:
        # in the 46' sub he must be the OUTGOING player.
        ev = next(e for e in self._all_raw()
                  if e.get("eventType") == 204
                  and (e.get("player") or {}).get("fullName") == "Kristian Hlynsson")
        n = _onex_norm_event(ev, "Iceland", "Luxembourg")
        self.assertEqual(n["type"], "sub")
        self.assertIn("Kristian Hlynsson", n["player"])
        self.assertIn("Andri Gudjohnsen", n["player"])
        self.assertEqual(n["assist"], "")

    def test_unknown_types_skipped(self):
        self.assertIsNone(_onex_norm_event(
            {"eventType": 201, "eventName": "Corner", "minute": "53'", "side": 1},
            "Iceland", "Luxembourg"))
        self.assertIsNone(_onex_norm_event(
            {"eventType": 999, "eventName": "???", "minute": "10'", "side": 2},
            "Iceland", "Luxembourg"))


class TestPublicFunctions(unittest.IsolatedAsyncioTestCase):
    """onex_live_matches / onex_events with hc.get stubbed to the fixtures."""

    def setUp(self):
        self._orig_get = prov.hc.get
        self.fixtures = {
            "topGamesByStat": _load("top_games_by_stat.json"),
            "chronoLine": _load("chrono_line_771495784.json"),
        }

        async def fake_get(url, headers, params=None, cache_key="", provider="x",
                           refresh_fn=None):
            if "topGamesByStat" in url:
                return self.fixtures["topGamesByStat"]
            if "chronoLine" in url:
                self.assertEqual(params.get("gameId"), 771495784)
                return self.fixtures["chronoLine"]
            raise AssertionError(f"unexpected URL in test: {url}")

        prov.hc.get = fake_get

    def tearDown(self):
        prov.hc.get = self._orig_get

    async def test_live_matches(self):
        matches = await onex_live_matches()
        self.assertEqual(len(matches), 5)
        by_id = {m["onex_id"]: m for m in matches}
        self.assertEqual(by_id[771495784]["home_score"], 3)
        self.assertEqual(by_id[771495784]["status"], "2H")

    async def test_events(self):
        events = await onex_events(771495784, "Iceland", "Luxembourg")
        self.assertEqual(len(events), 5)
        goals = [e for e in events if e["type"] == "goal"]
        self.assertEqual(len(goals), 3)
        # Same fingerprint scheme as the other providers: a 50' Iceland
        # goal from 1xBet collapses with the same goal from ESPN/SofaScore.
        self.assertIn(canonical_fingerprint("iceland", 50, "goal"),
                      {e["fingerprint"] for e in goals})

    async def test_api_error_returns_empty(self):
        async def failing_get(url, headers, params=None, cache_key="",
                              provider="x", refresh_fn=None):
            return None
        prov.hc.get = failing_get
        self.assertEqual(await onex_live_matches(), [])
        self.assertEqual(await onex_events(1, "A", "B"), [])

    async def test_live_raw_skips_non_dict_entries(self):
        # One non-dict entry in the feed used to raise
        # AttributeError in the merge loop and drop the WHOLE live
        # list (the /ask handler then answered "no games live").
        # Bad entries are skipped; the good games still come back.
        good = _load("top_games_by_stat.json")[:2]
        prov._onex_reset_caches()

        async def fake_get(url, headers, params=None, cache_key="",
                           provider="x", refresh_fn=None):
            if "games1x2" in url:
                return [None, "x", 123, good[0]]
            if "topGamesByStat" in url:
                return [good[1]]
            raise AssertionError(f"unexpected URL in test: {url}")

        prov.hc.get = fake_get
        raw = await prov.onex_live_raw()
        self.assertEqual([g["id"] for g in raw],
                         [good[0]["id"], good[1]["id"]])
        prov._onex_reset_caches()

    async def test_fetch_tick_reports_failed_fetch(self):
        # Both live lists failing is a FETCH ERROR, not "nothing
        # live": onex_fetch_tick says so, so the scoreboard can
        # pass a failed tick (raw None) to the watches instead of
        # an empty list that would age every watch toward ending.
        prov._onex_reset_caches()

        async def failing_get(url, headers, params=None, cache_key="",
                              provider="x", refresh_fn=None):
            raise ConnectionError("boom")

        prov.hc.get = failing_get
        npl, featured, raw, ok = await prov.onex_fetch_tick()
        self.assertEqual((npl, featured, raw), ([], [], []))
        self.assertFalse(ok)
        prov._onex_reset_caches()

    async def test_fetch_tick_reports_successful_empty_fetch(self):
        # The mirror image: both lists arriving empty IS real data
        # (nothing live) — ok stays True, so a genuinely finished
        # game still ages out of its watch.
        prov._onex_reset_caches()

        async def empty_get(url, headers, params=None, cache_key="",
                            provider="x", refresh_fn=None):
            return []

        prov.hc.get = empty_get
        npl, featured, raw, ok = await prov.onex_fetch_tick()
        self.assertEqual((npl, featured, raw), ([], [], []))
        self.assertTrue(ok)
        prov._onex_reset_caches()

    def test_npl_filter_skips_non_dict_entries(self):
        # Same family, in the NPL filter the scoreboard tick uses:
        # a non-dict entry (or a non-dict liga) must not kill the
        # whole tick.
        npl = json.loads((Path(__file__).resolve().parent
                          / "fixtures" / "scoreboard"
                          / "games1x2_npl.json").read_text(encoding="utf-8"))
        out = prov._onex_npl_from_games1x2(
            [None, "x", {"id": 1, "liga": "Nepal. Premier league"},
             {"id": 2, "liga": {"name": 123}}, *npl])
        # Only the true NPL entry survives (the fixture's second
        # entry is AFCON, not NPL) — and no bad entry raised.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["home"], "Lumbini Lions")
        self.assertTrue(all(m["league"] == "Nepal Premier League"
                            for m in out))


class TestOnexSearchBarca(unittest.IsolatedAsyncioTestCase):
    """18 Nov 2026: onex_search("Barcelona") returned the live
    Barcelona II game, and the auto-watch posted its panel in
    #general. A bare base name never matches a reserve side."""

    BARCA_II_RAW = {
        "id": 772065621, "mainGameId": 772065621,
        "liga": {"id": 1, "name": "Spain. Copa Catalunya"},
        "opponent1": {"fullName": "Can Vidalet"},
        "opponent2": {"fullName": "Barcelona II"},
        "startTs": 1795000000,
        "scores": {"scoreOpp1": 0, "scoreOpp2": 0,
                   "timer": {"timeSec": 2400},
                   "currentPeriod": 1, "currentPeriodName": "1st half"},
        "sport": {"id": 1, "name": "Football"},
    }

    def _stubs(self):
        return (
            patch.object(prov, "onex_live_raw",
                         new=AsyncMock(return_value=[self.BARCA_II_RAW])),
            patch.object(prov, "onex_npl_fixtures",
                         new=AsyncMock(return_value=[])),
            patch.object(prov, "onex_uefa_fixtures",
                         new=AsyncMock(return_value=[])),
        )

    async def test_barcelona_search_skips_barcelona_ii(self):
        a, b, c = self._stubs()
        with a, b, c:
            out = await prov.onex_search("Barcelona")
        self.assertEqual(out["games"], [])
        self.assertIsNone(out["detail"])

    async def test_barcelona_ii_search_finds_it(self):
        a, b, c = self._stubs()
        with a, b, c:
            out = await prov.onex_search("Barcelona II")
        self.assertEqual(len(out["games"]), 1)
        self.assertEqual(out["games"][0]["away"], "Barcelona II")
        self.assertEqual(out["games"][0]["onex_id"], 772065621)

    async def test_barca_search_skips_barcelona_ii(self):
        a, b, c = self._stubs()
        with a, b, c:
            out = await prov.onex_search("barca")
        self.assertEqual(out["games"], [])


class TestNplScorecard(unittest.IsolatedAsyncioTestCase):
    """NPL official-API scorecard: striker / bowler / partnership
    for Advanced. Fixture is the real live-scores.php response
    for Qualifier 2, Sudurpaschim vs Chitwan, 19 Nov 2026
    (photo URLs stripped — the parser never reads them)."""

    def setUp(self):
        self.data = _load("npl_live_scorecard.json")
        prov._onex_reset_caches()

    def tearDown(self):
        prov._onex_reset_caches()

    def test_parse_live_card(self):
        card = prov._npl_scorecard_from_api(
            self.data, "Sudurpaschim Royals", "Chitwan Rhinos")
        self.assertIsNotNone(card)
        self.assertEqual(card["striker"]["name"], "Tharindu Ratnayake")
        self.assertEqual(card["striker"]["runs"], "44")
        self.assertEqual(card["non_striker"]["name"], "Fabian Allen")
        self.assertEqual(card["bowler"]["name"], "Rijan Dhakal")
        self.assertEqual(card["bowler"]["wickets"], "1")
        self.assertEqual(card["partnership"], "22 (16)")
        self.assertEqual(card["last_wicket"]["name"], "Aarif Sheikh")
        self.assertEqual(card["last_wicket"]["fow"], "102")
        self.assertEqual(card["crr"], "6.7")
        self.assertEqual(card["last_over"], "6/0")
        self.assertEqual(len(card["batting"]), 8)
        self.assertEqual(len(card["bowling"]), 6)
        marked = [b for b in card["batting"]
                  if b.get("on_strike") is True]
        self.assertEqual([b["name"] for b in marked],
                         ["Tharindu Ratnayake"])

    def test_teams_match_either_order(self):
        card = prov._npl_scorecard_from_api(
            self.data, "Chitwan Rhinos", "Sudurpaschim Royals")
        self.assertIsNotNone(card)
        self.assertEqual(card["striker"]["name"], "Tharindu Ratnayake")

    def test_wrong_teams_is_none(self):
        self.assertIsNone(prov._npl_scorecard_from_api(
            self.data, "Lumbini Lions", "Biratnagar Kings"))

    def test_no_live_block_is_none(self):
        import copy
        data = copy.deepcopy(self.data)
        del data["matches"][0]["live"]
        self.assertIsNone(prov._npl_scorecard_from_api(
            data, "Sudurpaschim Royals", "Chitwan Rhinos"))

    async def test_fetch_caches_for_10s(self):
        calls = []

        async def fake_get(url, headers, params=None, cache_key="",
                           provider="onexbet", refresh_fn=None):
            calls.append(url)
            return self.data

        orig = prov.hc.get
        prov.hc.get = fake_get
        try:
            card = await prov.onex_npl_scorecard(
                "Sudurpaschim Royals", "Chitwan Rhinos")
            self.assertIsNotNone(card)
            await prov.onex_npl_scorecard(
                "Sudurpaschim Royals", "Chitwan Rhinos")
            self.assertEqual(len(calls), 1)
        finally:
            prov.hc.get = orig

    async def test_fetch_failure_is_none_not_raise(self):
        async def failing_get(url, headers, params=None, cache_key="",
                              provider="onexbet", refresh_fn=None):
            raise ConnectionError("down")

        orig = prov.hc.get
        prov.hc.get = failing_get
        try:
            self.assertIsNone(await prov.onex_npl_scorecard(
                "Sudurpaschim Royals", "Chitwan Rhinos"))
        finally:
            prov.hc.get = orig


if __name__ == "__main__":
    unittest.main()
