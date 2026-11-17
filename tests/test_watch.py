"""Game search/detail, AI watch resolution, watch storage + alerts.

No Discord token, no network, no real Gemini — hc.get is stubbed
with the fixtures in tests/fixtures/watch/ (modelled on the real
1xBet games1x2 / topGamesByStat / gameInfo / leftMenuSports
shapes), and Gemini is a fake model object.

Run:  python3 -m pytest tests/test_watch.py -q
from the footballbot directory.
"""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import providers as prov
import watch as wmod

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "watch"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeModel:
    """Stand-in for the Gemini model: generate_content returns
    whatever text it was constructed with."""

    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    def generate_content(self, prompt: str):
        self.prompts.append(prompt)
        response = Mock()
        response.text = self.text
        return response


class StubHttp:
    """Fake hc.get routing by URL, like test_scoreboard.py."""

    def __init__(self, test):
        self.test = test
        self.calls: list[str] = []

    async def get(self, url, headers, params=None, cache_key="",
                  provider="x", refresh_fn=None):
        if "games1x2" in url:
            self.calls.append("games1x2")
            return self.test.games1x2
        if "topGamesByStat" in url:
            self.calls.append("top")
            return self.test.top
        if "gameInfo" in url:
            gid = str((params or {}).get("gameId"))
            self.calls.append(f"gameInfo:{gid}")
            return self.test.gameinfo.get(gid, {})
        if "leftMenuSports" in url:
            self.calls.append("line")
            if "2791573" in str((params or {}).get("selectedMs", "")):
                return self.test.line_npl
            return []
        raise AssertionError(f"unexpected URL in test: {url}")


def _detail_home_away(home_score, away_score, **kw):
    d = {"home": "Home", "away": "Away", "home_score": home_score,
         "away_score": away_score, "status": "2H", "minute": 67,
         "clock": "67'", "league": "Test League", "onex_id": 1,
         "stats": None, "cricket": None, "format": "",
         "venue": None, "weather": None, "sport": "Football"}
    d.update(kw)
    return d


def _snap(home_score, away_score, **kw):
    d = {"home_score": str(home_score), "away_score": str(away_score),
         "status": "2H", "minute": 67, "clock": "67'", "is_break": False,
         "stats": {}, "cricket": {}, "_home": "Home", "_away": "Away"}
    d.update(kw)
    return d


class TestTabloStats(unittest.TestCase):
    def test_order_and_values(self):
        g = _load("games1x2.json")[0]
        stats = prov._onex_tabular_stats(g["scores"])
        names = [s["name"] for s in stats]
        self.assertEqual(names[:3], ["Yellow cards", "Red card", "Corner"])
        yellow = next(s for s in stats if s["name"] == "Yellow cards")
        self.assertEqual((yellow["home"], yellow["away"]), ("3", "1"))

    def test_none_when_absent(self):
        self.assertIsNone(prov._onex_tabular_stats({}))
        self.assertIsNone(prov._onex_tabular_stats({"tabloStats": {"0": []}}))


class TestRenderDetail(unittest.TestCase):
    def test_simple_has_score_clock_id_and_cards(self):
        g = _load("games1x2.json")[0]
        detail = prov._onex_detail_from_raw(g)
        detail["venue"] = "Stade de Marrakech"
        detail["weather"] = "Clear skies · +22°C"
        text = wmod.render_detail(detail, "simple")
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", text)
        self.assertIn("Stade de Marrakech", text)
        self.assertIn("Clear skies", text)
        self.assertIn("Game ID: 771747666", text)
        self.assertIn("🟨 3–1", text)
        self.assertNotIn("Match stats", text)
        self.assertNotIn("Possession", text)

    def test_advanced_has_full_stats_table(self):
        g = _load("games1x2.json")[0]
        detail = prov._onex_detail_from_raw(g)
        text = wmod.render_detail(detail, "advanced")
        self.assertIn("Match stats", text)
        self.assertIn("Possession %", text)
        self.assertIn("Shots on target", text)
        self.assertIn("Substitutions", text)

    def test_cricket_break_and_target_advanced_only(self):
        raw = _load("games1x2.json")[3]  # Farah Fighters, mid-innings T10
        detail = prov._onex_detail_from_raw(raw)
        detail["format"] = "T10"
        detail["cricket"].update(prov._onex_cricket_target_fmt(
            "T10", detail["home_score"], detail["away_score"],
            detail["cricket"]["team"], detail["cricket"]["over_str"]))
        simple = wmod.render_detail(detail, "simple")
        advanced = wmod.render_detail(detail, "advanced")
        self.assertNotIn("Target:", simple)
        self.assertIn("Target: 153", advanced)
        self.assertIn("Need: 69 off 23 balls", advanced)
        self.assertIn("Run rate: 13.62", advanced)

    def test_cricket_break_label(self):
        raw = _load("games1x2.json")[1]  # JYC, innings break
        detail = prov._onex_detail_from_raw(raw)
        self.assertEqual(detail["clock"], "Innings break")
        self.assertIn("Innings break", wmod.render_detail(detail, "simple"))


class TestSearch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_get = prov.hc.get
        self.games1x2 = _load("games1x2.json")
        self.top = _load("top.json")
        self.line_npl = _load("line_npl.json")
        self.gameinfo = {
            "771747666": _load("gameinfo_771747666.json"),
            "771747795": _load("gameinfo_alias.json"),
        }
        prov.hc.get = StubHttp(self).get

    def tearDown(self):
        prov.hc.get = self._orig_get

    async def test_search_by_live_id(self):
        result = await prov.onex_search("771747666")
        detail = result["detail"]
        self.assertIsNotNone(detail)
        self.assertEqual(detail["home_score"], 0)
        self.assertEqual(detail["away_score"], 1)
        self.assertEqual(detail["venue"], "Stade de Marrakech")
        self.assertEqual(detail["const_id"], "387423700")
        self.assertTrue(detail["has_lineups"])
        self.assertEqual(len(detail["stats"]), 10)

    async def test_search_by_alias_id_resolves_to_live_game(self):
        result = await prov.onex_search("771747795")
        detail = result["detail"]
        self.assertIsNotNone(detail)
        self.assertEqual(detail["home"], "Sierra Leone")
        self.assertEqual(detail["away_score"], 1)
        self.assertEqual(str(detail["onex_id"]), "771747706")

    async def test_search_by_name_multiple_returns_list(self):
        result = await prov.onex_search("Bulls")
        self.assertIsNone(result["detail"])
        names = {g["home"] for g in result["games"]} | {g["away"] for g in result["games"]}
        self.assertIn("UAE Bulls", names)
        self.assertIn("Rohtak Bulls", names)

    async def test_search_by_name_finds_npl_fixture(self):
        result = await prov.onex_search("Lumbini")
        self.assertTrue(any(g["home"] == "Lumbini Lions"
                            for g in result["games"]))
        fixture = next(g for g in result["games"]
                       if g["home"] == "Lumbini Lions")
        self.assertEqual(fixture["status"], "upcoming")
        self.assertEqual(fixture["format"], "T20")

    async def test_search_no_match(self):
        result = await prov.onex_search("Zzqxj No Such Team")
        self.assertEqual(result["games"], [])
        self.assertIsNone(result["detail"])

    async def test_search_unknown_id(self):
        result = await prov.onex_search("999999999")
        self.assertEqual(result["games"], [])
        self.assertIsNone(result["detail"])


class TestAiParse(unittest.IsolatedAsyncioTestCase):
    GROUNDING = [
        {"id": "771747666", "home": "Republic of the Gambia",
         "away": "Ivory Coast", "league": "Africa Cup of Nations",
         "status": "live"},
        {"id": "771800001", "home": "Lumbini Lions",
         "away": "Sudurpaschim Royals", "league": "Nepal Premier League",
         "status": "upcoming"},
    ]

    async def test_valid_json_follow(self):
        model = FakeModel('{"game_id": "771800001", "action": "follow", '
                          '"detail": "simple", "note": "Lumbini game"}')
        out = await wmod.parse_watch_ai("tell me when Lumbini scores",
                                        self.GROUNDING, model)
        self.assertEqual(out["game_id"], "771800001")
        self.assertEqual(out["action"], "follow")

    async def test_unknown_id_rejected(self):
        model = FakeModel('{"game_id": "123456789", "action": "follow", '
                          '"detail": "simple", "note": "x"}')
        out = await wmod.parse_watch_ai("some game", self.GROUNDING, model)
        self.assertIsNone(out)

    async def test_invalid_json_rejected(self):
        model = FakeModel("I think the Gambia game looks good!")
        out = await wmod.parse_watch_ai("gambia", self.GROUNDING, model)
        self.assertIsNone(out)

    async def test_no_model_returns_none(self):
        out = await wmod.parse_watch_ai("gambia", self.GROUNDING, None)
        self.assertIsNone(out)

    async def test_unfollow_and_advanced_actions(self):
        model = FakeModel('{"game_id": "771747666", "action": "unfollow", '
                          '"detail": "advanced", "note": "stop"}')
        out = await wmod.parse_watch_ai("stop watching gambia",
                                        self.GROUNDING, model)
        self.assertEqual(out["action"], "unfollow")
        self.assertEqual(out["detail"], "advanced")


class TestResolve(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_get = prov.hc.get
        self.games1x2 = _load("games1x2.json")
        self.top = _load("top.json")
        self.line_npl = _load("line_npl.json")
        self.gameinfo = {}
        prov.hc.get = StubHttp(self).get

    def tearDown(self):
        prov.hc.get = self._orig_get

    async def test_numeric_id_direct(self):
        out = await wmod.resolve_watch("771747666", None)
        self.assertEqual(out["game_id"], "771747666")
        self.assertEqual(out["source"], "id")

    async def test_exact_team_name(self):
        out = await wmod.resolve_watch("Lumbini Lions", None)
        self.assertEqual(out["game_id"], "771800001")
        self.assertEqual(out["source"], "name")

    async def test_unique_substring(self):
        out = await wmod.resolve_watch("Gambia", None)
        self.assertEqual(out["game_id"], "771747666")
        self.assertEqual(out["source"], "name")

    async def test_ambiguous_name_lists_candidates(self):
        out = await wmod.resolve_watch("Bulls", None)
        self.assertIsNone(out["game_id"])
        self.assertEqual(len(out["candidates"]), 2)

    async def test_ai_fallback_when_name_fails(self):
        model = FakeModel('{"game_id": "771800001", "action": "follow", '
                          '"detail": "simple", "note": "Lumbini"}')
        out = await wmod.resolve_watch("the lions game tomorrow", model)
        self.assertEqual(out["game_id"], "771800001")
        self.assertEqual(out["source"], "ai")

    async def test_no_match_no_model(self):
        out = await wmod.resolve_watch("Zzqxj No Such Team", None)
        self.assertIsNone(out["game_id"])
        self.assertEqual(out["candidates"], [])


class TestDbWatches(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        db._DB = self._orig_db
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    async def test_add_list_remove_roundtrip(self):
        snap = {"home_score": "0", "away_score": "1", "status": "2H"}
        await db.add_watch(111, "771747666", "Gambia", "Ivory Coast",
                           "AFCON", "simple", "", snap)
        rows = await db.list_watches(111)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["game_id"], "771747666")
        self.assertEqual(rows[0]["detail"], "simple")
        await db.update_watch_snapshot(111, "771747666",
                                       {**snap, "home_score": "1"})
        rows = await db.list_watches(111)
        self.assertIn('"home_score": "1"', rows[0]["snapshot"])
        self.assertEqual(await db.get_all_watches(), rows)
        await db.remove_watch(111, "771747666")
        self.assertEqual(await db.list_watches(111), [])


class TestDetectChanges(unittest.TestCase):
    def test_goal(self):
        old = _snap("0", "0")
        new = _snap("1", "0", minute=67)
        events = wmod.detect_changes(old, new, "simple")
        self.assertEqual(len(events), 1)
        self.assertIn("GOAL", events[0])
        self.assertIn("1–0", events[0])

    def test_first_sighting_is_not_a_goal(self):
        old = _snap("", "", status="upcoming")
        new = _snap("0", "0", status="live")
        events = wmod.detect_changes(old, new, "simple")
        self.assertTrue(all("GOAL" not in e for e in events))
        self.assertTrue(any("Kickoff" in e for e in events))

    def test_no_change_no_events(self):
        old = _snap("1", "0")
        new = _snap("1", "0")
        self.assertEqual(wmod.detect_changes(old, new, "simple"), [])

    def test_card_count_change(self):
        old = _snap("0", "0", stats={"Yellow cards": ("1", "1"),
                                     "Red card": ("0", "0")})
        new = _snap("0", "0", stats={"Yellow cards": ("2", "1"),
                                     "Red card": ("0", "0")})
        events = wmod.detect_changes(old, new, "simple")
        self.assertEqual(len(events), 1)
        self.assertIn("Yellow card", events[0])

    def test_red_card(self):
        old = _snap("0", "0", stats={"Red card": ("0", "0")})
        new = _snap("0", "0", stats={"Red card": ("0", "1")})
        events = wmod.detect_changes(old, new, "simple")
        self.assertTrue(any("RED CARD" in e for e in events))

    def test_wicket(self):
        old = _snap("120/2", "0/0", status="live")
        new = _snap("125/3", "0/0", status="live")
        events = wmod.detect_changes(old, new, "simple")
        self.assertTrue(any("WICKET" in e for e in events))

    def test_cricket_runs_only_no_event(self):
        old = _snap("120/2", "0/0", status="live")
        new = _snap("125/2", "0/0", status="live")
        self.assertEqual(wmod.detect_changes(old, new, "simple"), [])

    def test_innings_break(self):
        old = _snap("157", "0/0", status="live", is_break=False)
        new = _snap("157", "0/0", status="live", is_break=True)
        events = wmod.detect_changes(old, new, "simple")
        self.assertTrue(any("Innings break" in e for e in events))

    def test_full_time(self):
        old = _snap("1", "0", status="2H")
        new = _snap("1", "0", status="FT")
        events = wmod.detect_changes(old, new, "simple")
        self.assertTrue(any("Full-time" in e for e in events))

    def test_advanced_sub_and_penalty_events(self):
        old = _snap("0", "0", stats={"Substitutions": ("1", "1"),
                                     "Penalty": ("0", "0")})
        new = _snap("0", "0", stats={"Substitutions": ("2", "1"),
                                     "Penalty": ("0", "0")})
        self.assertEqual(
            wmod.detect_changes(old, new, "simple"), [])
        events = wmod.detect_changes(old, new, "advanced")
        self.assertTrue(any("Substitutions" in e for e in events))


class FakeMessage:
    def __init__(self, mid, content, view=None):
        self.id = mid
        self.content = content
        self.view = view
        self.edits = []
        self.edit_error = None

    async def edit(self, content=None, allowed_mentions=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.content = content
        self.edits.append(content)


class FakeChannel:
    def __init__(self):
        self.sent: list[FakeMessage] = []
        self._next_id = 2000

    async def send(self, content, allowed_mentions=None, view=None):
        self._next_id += 1
        msg = FakeMessage(self._next_id, content, view=view)
        self.sent.append(msg)
        return msg


class FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, cid):
        return self._channel

    async def fetch_channel(self, cid):
        return self._channel


class TestOnTick(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp2"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True
        self.games1x2 = _load("games1x2.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _seed_watch(self, game_id="771747666", level="simple"):
        snap = wmod.snapshot_from_detail(
            {"home": "Republic of the Gambia", "away": "Ivory Coast",
             "home_score": 0, "away_score": 1, "status": "2H", "minute": 67,
             "clock": "67'", "stats": [{"name": "Yellow cards", "home": "3", "away": "1"},
                                       {"name": "Red card", "home": "0", "away": "0"}],
             "cricket": None})
        snap["_home"], snap["_away"] = "Republic of the Gambia", "Ivory Coast"
        wmod._watches[(777, game_id)] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": level, "fmt": "",
            "snapshot": snap, "missing": 0,
        }

    async def test_goal_sends_one_message(self):
        self._seed_watch()
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)
        self.assertIn("1–1", self.channel.sent[0].content)

    async def test_no_change_sends_nothing(self):
        self._seed_watch()
        await wmod.on_tick(self.bot, self.games1x2)
        self.assertEqual(len(self.channel.sent), 0)

    async def test_advanced_alert_keeps_stats_table_and_has_buttons(self):
        # Automatic alert stays exactly as before: event line PLUS
        # the full stats table for advanced watches — the buttons
        # are only for on-demand detail / live.
        self._seed_watch(level="advanced")
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        self.assertEqual(len(self.channel.sent), 1)
        msg = self.channel.sent[0]
        self.assertIn("GOAL", msg.content)
        self.assertIn("Match stats", msg.content)
        self.assertIn("Possession", msg.content)
        self.assertIsNotNone(msg.view)
        labels = [b.label for b in msg.view.children]
        self.assertEqual(labels, ["📊 Detail", "🔄 Live"])
        self.assertTrue(all("771747666" in b.custom_id
                            for b in msg.view.children))

    async def test_event_line_shows_mmss_clock(self):
        # The fixture carries timerSec 4020 (67:00) — the old code
        # showed no clock at all on football alerts.
        self._seed_watch(level="advanced")
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("(67:00)", self.channel.sent[0].content)

    async def test_missing_game_ends_watch_after_ten_ticks(self):
        self._seed_watch()
        for _ in range(9):
            await wmod.on_tick(self.bot, [])
            self.assertEqual(len(self.channel.sent), 0)
        await wmod.on_tick(self.bot, [])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("no longer live", self.channel.sent[0].content)
        self.assertEqual(wmod._watches, {})


class FakeResponse:
    async def defer(self, ephemeral=False):
        return None


class FakeFollowup:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content, ephemeral=False):
        self.sent.append(content)


class FakeGuild:
    """Stand-in for a Discord guild with two text channels."""

    def __init__(self, channels):
        self.text_channels = [SimpleNamespace(id=cid, name=name)
                              for cid, name in channels]

    def get_channel(self, cid):
        for ch in self.text_channels:
            if ch.id == cid:
                return ch
        return None


class FakeInteraction:
    """Stand-in for a /watch slash-command interaction."""

    def __init__(self, channel_id=777, guild=None):
        self.channel_id = channel_id
        self.channel = SimpleNamespace(id=channel_id)
        self.guild = guild
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class SlowModel:
    """Gemini that takes 0.5s to answer (too slow for the timeout)."""

    def generate_content(self, prompt):
        time.sleep(0.5)
        response = Mock()
        response.text = ('{"game_id": "771800001", "action": "follow", '
                         '"detail": "simple", "note": "Lumbini"}')
        return response


class TestLatencyFixes(unittest.IsolatedAsyncioTestCase):
    """The /watch slowness fix: the scoreboard's 1s tick cache is
    shared with /game and /watch, fixtures are cached ~60s, a slow
    Gemini call times out, and the user's Advanced dropdown wins."""

    def setUp(self):
        self._orig_get = prov.hc.get
        self.games1x2 = _load("games1x2.json")
        self.top = _load("top.json")
        self.line_npl = _load("line_npl.json")
        self.gameinfo = {
            "771747666": _load("gameinfo_771747666.json"),
            "771747795": _load("gameinfo_alias.json"),
        }
        self.stub = StubHttp(self)
        prov.hc.get = self.stub.get
        prov._onex_reset_caches()

    def tearDown(self):
        prov.hc.get = self._orig_get
        prov._onex_reset_caches()

    async def test_live_raw_second_call_uses_tick_cache(self):
        first = await prov.onex_live_raw()
        self.assertEqual(sorted(self.stub.calls), ["games1x2", "top"])
        second = await prov.onex_live_raw()
        self.assertEqual(self.stub.calls, ["games1x2", "top"])
        self.assertEqual(second, first)

    async def test_grounding_second_call_makes_no_http_call(self):
        await prov.onex_grounding()
        calls_after_first = list(self.stub.calls)
        # First grounding: live list + the NPL line menu only
        # (UEFA champ discovery is served from the live cache).
        self.assertIn("line", calls_after_first)
        await prov.onex_grounding()
        self.assertEqual(self.stub.calls, calls_after_first)

    async def test_fixture_cache_reused_within_60s(self):
        await prov.onex_npl_fixtures()
        self.assertEqual(self.stub.calls.count("line"), 1)
        await prov.onex_npl_fixtures()
        self.assertEqual(self.stub.calls.count("line"), 1)
        # Forcing the entry stale makes the next call re-fetch.
        ts, data = prov._fix_cache["npl"]
        prov._fix_cache["npl"] = (time.monotonic() - 61, data)
        await prov.onex_npl_fixtures()
        self.assertEqual(self.stub.calls.count("line"), 2)

    async def test_live_cache_expires_after_tick_window(self):
        await prov.onex_live_raw()
        self.assertEqual(len(self.stub.calls), 2)
        prov._live_cache["ts"] = time.monotonic() - 4
        await prov.onex_live_raw()
        self.assertEqual(len(self.stub.calls), 4)

    async def test_resolve_id_and_name_return_no_detail(self):
        # Deterministic paths must not hardcode "simple" — the
        # /watch dropdown decides the detail level.
        out = await wmod.resolve_watch("771747666", None)
        self.assertEqual(out["game_id"], "771747666")
        self.assertIsNone(out["detail"])
        out = await wmod.resolve_watch("Gambia", None)
        self.assertEqual(out["game_id"], "771747666")
        self.assertIsNone(out["detail"])

    async def test_dropdown_advanced_wins_over_resolved_simple(self):
        import bot as botmod

        resolved = {"game_id": "771747666", "action": "follow",
                    "detail": "simple", "candidates": [],
                    "entry": {"id": "771747666",
                              "home": "Republic of the Gambia",
                              "away": "Ivory Coast",
                              "league": "Africa Cup of Nations",
                              "status": "2H"},
                    "source": "id"}
        started = {"game_id": "771747666",
                   "home": "Republic of the Gambia",
                   "away": "Ivory Coast",
                   "league": "Africa Cup of Nations",
                   "status": "2H", "detail": "advanced"}
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(wmod, "start_watch",
                          new=AsyncMock(return_value=started)) as mock_start:
            await botmod.cmd_watch.callback(interaction, "771747666",
                                            detail="advanced")
        mock_start.assert_awaited_once()
        args, _kwargs = mock_start.call_args
        self.assertEqual(args[2], "advanced")
        self.assertTrue(any("advanced alerts" in s
                            for s in interaction.followup.sent))

    async def test_slow_gemini_times_out_and_returns_none(self):
        with patch.object(wmod, "AI_TIMEOUT_S", 0.05):
            start = time.monotonic()
            out = await wmod.parse_watch_ai("the lions game",
                                            TestAiParse.GROUNDING, SlowModel())
            elapsed = time.monotonic() - start
        self.assertIsNone(out)
        self.assertLess(elapsed, 0.4)


class TestAiWatchAll(unittest.IsolatedAsyncioTestCase):
    """One plain sentence does everything: the AI/words pick the
    game, the channel and the detail level — while the user's own
    dropdown picks always win."""

    CHANNELS = [{"id": 111, "name": "general"},
                {"id": 222, "name": "test"}]
    GUILD = FakeGuild([(111, "general"), (222, "test")])

    def setUp(self):
        self._orig_get = prov.hc.get
        self.games1x2 = _load("games1x2.json")
        self.top = _load("top.json")
        self.line_npl = _load("line_npl.json")
        self.gameinfo = {}
        prov.hc.get = StubHttp(self).get
        prov._onex_reset_caches()

    def tearDown(self):
        prov.hc.get = self._orig_get
        prov._onex_reset_caches()

    async def test_ai_channel_id_from_list_is_used(self):
        model = FakeModel('{"game_id": "771800001", "action": "follow", '
                          '"detail": "simple", "channel_id": "111", '
                          '"note": "Lumbini to general"}')
        out = await wmod.resolve_watch(
            "the lions game tomorrow, send alerts to the chat",
            model, self.CHANNELS)
        self.assertEqual(out["game_id"], "771800001")
        self.assertEqual(out["channel_id"], "111")
        self.assertEqual(out["channel"]["name"], "general")
        self.assertIn("111 | #general", model.prompts[0])

    async def test_ai_invented_channel_id_rejected(self):
        model = FakeModel('{"game_id": "771800001", "action": "follow", '
                          '"detail": "simple", "channel_id": "999999999", '
                          '"note": "x"}')
        out = await wmod.resolve_watch("the lions game tomorrow",
                                       model, self.CHANNELS)
        self.assertEqual(out["game_id"], "771800001")
        self.assertIsNone(out["channel_id"])
        self.assertIsNone(out["channel"])

    async def test_hash_channel_matches_deterministically(self):
        channel, named = wmod._channel_from_words(
            "follow Lumbini, send to #general", self.CHANNELS)
        self.assertEqual(channel["name"], "general")
        self.assertIsNone(named)
        # And through resolve_watch with the game coming from the AI.
        model = FakeModel('{"game_id": "771800001", "action": "follow", '
                          '"detail": "simple", "channel_id": null, '
                          '"note": "Lumbini"}')
        out = await wmod.resolve_watch(
            "the lions game tomorrow in #general", model, self.CHANNELS)
        self.assertEqual(out["channel_id"], "111")
        self.assertEqual(out["channel"]["name"], "general")

    def test_detail_keywords_from_words(self):
        self.assertEqual(
            wmod._detail_from_words("send advanced alerts"), "advanced")
        self.assertEqual(
            wmod._detail_from_words("full stats please"), "advanced")
        self.assertEqual(
            wmod._detail_from_words("just goals please"), "simple")
        self.assertIsNone(wmod._detail_from_words("follow lumbini"))

    async def test_words_advanced_beats_ai_simple(self):
        model = FakeModel('{"game_id": "771800001", "action": "follow", '
                          '"detail": "simple", "channel_id": null, '
                          '"note": "Lumbini"}')
        out = await wmod.resolve_watch(
            "the lions game tomorrow, full stats please",
            model, self.CHANNELS)
        self.assertEqual(out["detail"], "advanced")

    async def test_dropdown_advanced_beats_ai_simple(self):
        import bot as botmod

        resolved = {"game_id": "771800001", "action": "follow",
                    "detail": "simple", "candidates": [],
                    "entry": {"id": "771800001", "home": "Lumbini Lions",
                              "away": "Sudurpaschim Royals",
                              "league": "Nepal Premier League",
                              "status": "upcoming"},
                    "source": "ai", "channel": None, "channel_id": None,
                    "channel_named": None, "note": ""}
        started = {"game_id": "771800001", "home": "Lumbini Lions",
                   "away": "Sudurpaschim Royals",
                   "league": "Nepal Premier League",
                   "status": "upcoming", "detail": "advanced"}
        interaction = FakeInteraction(guild=self.GUILD)
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)),              patch.object(wmod, "start_watch",
                          new=AsyncMock(return_value=started)) as mock_start:
            await botmod.cmd_watch.callback(interaction, "Lumbini",
                                            detail="advanced")
        args, _kwargs = mock_start.call_args
        self.assertEqual(args[2], "advanced")

    async def test_dropdown_unset_words_advanced_is_used(self):
        import bot as botmod

        resolved = {"game_id": "771800001", "action": "follow",
                    "detail": "advanced", "candidates": [],
                    "entry": {"id": "771800001", "home": "Lumbini Lions",
                              "away": "Sudurpaschim Royals",
                              "league": "Nepal Premier League",
                              "status": "upcoming"},
                    "source": "ai", "channel": None, "channel_id": None,
                    "channel_named": None, "note": ""}
        started = {"game_id": "771800001", "home": "Lumbini Lions",
                   "away": "Sudurpaschim Royals",
                   "league": "Nepal Premier League",
                   "status": "upcoming", "detail": "advanced"}
        interaction = FakeInteraction(guild=self.GUILD)
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)),              patch.object(wmod, "start_watch",
                          new=AsyncMock(return_value=started)) as mock_start:
            await botmod.cmd_watch.callback(
                interaction, "follow Lumbini, advanced stats")
        args, _kwargs = mock_start.call_args
        self.assertEqual(args[2], "advanced")
        self.assertTrue(any("advanced alerts" in s
                            for s in interaction.followup.sent))

    async def test_ai_channel_used_at_command_level(self):
        import bot as botmod

        resolved = {"game_id": "771800001", "action": "follow",
                    "detail": "simple", "candidates": [],
                    "entry": {"id": "771800001", "home": "Lumbini Lions",
                              "away": "Sudurpaschim Royals",
                              "league": "Nepal Premier League",
                              "status": "upcoming"},
                    "source": "ai",
                    "channel": {"id": 111, "name": "general"},
                    "channel_id": "111", "channel_named": None, "note": ""}
        started = {"game_id": "771800001", "home": "Lumbini Lions",
                   "away": "Sudurpaschim Royals",
                   "league": "Nepal Premier League",
                   "status": "upcoming", "detail": "simple"}
        interaction = FakeInteraction(channel_id=777, guild=self.GUILD)
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)),              patch.object(wmod, "start_watch",
                          new=AsyncMock(return_value=started)) as mock_start:
            await botmod.cmd_watch.callback(
                interaction, "the lions game tomorrow")
        args, _kwargs = mock_start.call_args
        self.assertEqual(args[0], 111)
        self.assertTrue(any("<#111>" in s
                            for s in interaction.followup.sent))

    async def test_nonexistent_channel_falls_back_and_says_so(self):
        import bot as botmod

        resolved = {"game_id": "771800001", "action": "follow",
                    "detail": "simple", "candidates": [],
                    "entry": {"id": "771800001", "home": "Lumbini Lions",
                              "away": "Sudurpaschim Royals",
                              "league": "Nepal Premier League",
                              "status": "upcoming"},
                    "source": "ai", "channel": None, "channel_id": None,
                    "channel_named": "random", "note": ""}
        started = {"game_id": "771800001", "home": "Lumbini Lions",
                   "away": "Sudurpaschim Royals",
                   "league": "Nepal Premier League",
                   "status": "upcoming", "detail": "simple"}
        interaction = FakeInteraction(channel_id=777, guild=self.GUILD)
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)),              patch.object(wmod, "start_watch",
                          new=AsyncMock(return_value=started)) as mock_start:
            await botmod.cmd_watch.callback(
                interaction, "follow Lumbini, send to #random")
        args, _kwargs = mock_start.call_args
        self.assertEqual(args[0], 777)
        self.assertTrue(any("could not find a channel called 'random'" in s
                            for s in interaction.followup.sent))

    async def test_dropdown_channel_beats_ai_channel(self):
        import bot as botmod

        resolved = {"game_id": "771800001", "action": "follow",
                    "detail": "simple", "candidates": [],
                    "entry": {"id": "771800001", "home": "Lumbini Lions",
                              "away": "Sudurpaschim Royals",
                              "league": "Nepal Premier League",
                              "status": "upcoming"},
                    "source": "ai",
                    "channel": {"id": 111, "name": "general"},
                    "channel_id": "111", "channel_named": None, "note": ""}
        started = {"game_id": "771800001", "home": "Lumbini Lions",
                   "away": "Sudurpaschim Royals",
                   "league": "Nepal Premier League",
                   "status": "upcoming", "detail": "simple"}
        interaction = FakeInteraction(channel_id=777, guild=self.GUILD)
        picked = SimpleNamespace(id=222, name="test")
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)),              patch.object(wmod, "start_watch",
                          new=AsyncMock(return_value=started)) as mock_start:
            await botmod.cmd_watch.callback(
                interaction, "the lions game tomorrow", channel=picked)
        args, _kwargs = mock_start.call_args
        self.assertEqual(args[0], 222)
        self.assertTrue(any("<#222>" in s
                            for s in interaction.followup.sent))


class TestAlertButtons(unittest.IsolatedAsyncioTestCase):
    """The 📊 Detail / 🔄 Live buttons on watch alerts (mocked
    providers + fake Discord interactions, no network)."""

    def setUp(self):
        self.games1x2 = _load("games1x2.json")
        self.detail = prov._onex_detail_from_raw(self.games1x2[0])

    def _view(self):
        return wmod.WatchAlertView("771747666")

    def _mock_search(self, detail):
        return patch.object(
            prov, "onex_search",
            new=AsyncMock(return_value={"games": [], "detail": detail}))

    async def test_detail_button_renders_advanced_ephemerally(self):
        view = self._view()
        interaction = FakeInteraction()
        with self._mock_search(self.detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Match stats", text)
        self.assertIn("Possession", text)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", text)

    async def test_live_button_replies_score_and_mmss(self):
        view = self._view()
        interaction = FakeInteraction()
        with self._mock_search(self.detail):
            await view.children[1].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("0 – 1", text)
        self.assertIn("67:00", text)
        self.assertNotIn("Match stats", text)

    async def test_live_button_cricket_shows_overs(self):
        cricket_detail = {
            "home": "Lumbini Lions", "away": "Sudurpaschim Royals",
            "home_score": "108/4", "away_score": "105/8",
            "status": "live", "minute": None, "clock": "16.4 ov",
            "stats": None, "format": "T20",
            "cricket": {"is_break": False, "team": 2, "over_str": "16.4",
                        "target": 106, "need": 1, "balls_left": 20,
                        "run_rate": "6.55"},
        }
        view = self._view()
        interaction = FakeInteraction()
        with self._mock_search(cricket_detail):
            await view.children[1].callback(interaction)
        self.assertIn("16.4 ov", interaction.followup.sent[0])
        self.assertIn("108/4", interaction.followup.sent[0])

    async def test_gone_game_says_no_longer_live(self):
        view = self._view()
        for child in view.children:
            interaction = FakeInteraction()
            with self._mock_search(None):
                await child.callback(interaction)
            self.assertEqual(interaction.followup.sent,
                             ["That game is no longer live on 1xBet."])

    async def test_gone_game_score_less_detail_says_no_longer_live(self):
        gone = {"home": "", "away": "", "home_score": None,
                "away_score": None, "status": "?", "minute": None,
                "clock": "Score not available right now", "stats": None,
                "cricket": None}
        view = self._view()
        interaction = FakeInteraction()
        with self._mock_search(gone):
            await view.children[0].callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["That game is no longer live on 1xBet."])


class TestMmssClock(unittest.TestCase):
    def test_format_mmss(self):
        self.assertEqual(prov.format_mmss(4354), "72:34")
        self.assertEqual(prov.format_mmss(4020), "67:00")
        self.assertEqual(prov.format_mmss(61), "1:01")
        self.assertEqual(prov.format_mmss(0), "0:00")
        self.assertIsNone(prov.format_mmss(None))
        self.assertIsNone(prov.format_mmss(-5))

    def test_detail_from_raw_sets_mmss_clock(self):
        # The screenshot bug: football detail had timer_sec but no
        # clock string, so alerts showed no clock at all.
        detail = prov._onex_detail_from_raw(_load("games1x2.json")[0])
        self.assertEqual(detail["timer_sec"], 4020)
        self.assertEqual(detail["clock"], "67:00")

    def test_snapshot_derives_clock_from_timer_sec(self):
        snap = wmod.snapshot_from_detail(
            {"home_score": 2, "away_score": 0, "status": "2H",
             "minute": 72, "clock": "", "timer_sec": 4354,
             "stats": [], "cricket": None})
        self.assertEqual(snap["clock"], "72:34")
        self.assertEqual(snap["timer_sec"], 4354)


if __name__ == "__main__":
    unittest.main()
