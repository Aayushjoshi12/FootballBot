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


def _load_scoreboard(name: str):
    return json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "scoreboard" / name
         ).read_text(encoding="utf-8"))


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

    def test_render_top_games_stays_under_discord_limit(self):
        # 18 Nov 2026: /ask failed with "Something went wrong"
        # because a long list pushed the reply over Discord's
        # 2000-character limit. _live_line_list got a budget in
        # the first fix; render_top_games (the pick list used by
        # search suggestions) had none — ten games with long
        # team/league names passed 2000 chars on their own.
        games = []
        for i in range(10):
            games.append({
                "home": f"Persatuan Sepakbola Indonesia Raya Bersatu {i} United FC",
                "away": f"Klub Sepakbola Muda Bersatu Selatan {i} Football Club",
                "home_score": 1, "away_score": 0,
                "league": "Indonesia. Liga Nusantara Championship Elite Division",
                "onex_id": 771000000 + i, "status": "2H",
                "clock": "67:12", "minute": 67})
        msg = wmod.render_top_games(games)
        self.assertLess(len(msg), 2000)
        self.assertIn("…and", msg)


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
        self.deleted = False

    async def edit(self, content=None, allowed_mentions=None, view=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.content = content
        if view is not None:
            self.view = view
        self.edits.append(content)

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self):
        self.sent: list[FakeMessage] = []
        self.messages: dict[int, FakeMessage] = {}
        # Sends start at 3001: tests seed panels at 2001, and a
        # send must never reuse a seeded panel's id (the panel
        # refresh fetches by id — a collision would edit the alert
        # into a panel).
        self._next_id = 3000

    async def send(self, content, allowed_mentions=None, view=None):
        self._next_id += 1
        msg = FakeMessage(self._next_id, content, view=view)
        self.sent.append(msg)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        msg = self.messages.get(mid)
        if msg is None:
            import discord
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"),
                "Unknown Message")
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
        wmod._channel_last_edit.clear()
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
        # The watch already has its live panel (posted by an earlier
        # tick): the panel body matches the tick's render, so a
        # no-change tick edits nothing and sends nothing.
        detail = prov._onex_detail_from_raw(self.games1x2[0])
        panel_body = wmod.render_panel(detail, "67:00")
        msg = FakeMessage(2001, panel_body)
        self.channel.messages[2001] = msg
        wmod._watches[(777, game_id)] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": level, "fmt": "",
            "snapshot": snap, "missing": 0,
            "panel_message_id": 2001, "panel_body": panel_body,
            # The base is the body without the footer (the seed
            # has no footer) and the edit is recent, so a
            # no-change tick edits nothing — no heartbeat either.
            "panel_base_body": panel_body,
            "panel_static": {}, "panel_momentum": None,
            "panel_momentum_ts": 0.0,
            "last_panel_edit": time.monotonic(),
            "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
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
        # And the panel itself is not edited either — no change,
        # no heartbeat yet (the seed's edit is seconds old).
        self.assertEqual(len(self.channel.messages[2001].edits), 0)

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
        self.assertEqual(labels, ["📊 Detail", "🔄 Live", "🔄 Refresh",
                                  "📜 Timeline", "🟢 Simple", "🔵 Advanced",
                                  "🔔 Follow", "🔕 Unfollow"])
        self.assertEqual([b.custom_id for b in msg.view.children],
                         ["watch:detail", "watch:live", "panel:refresh",
                          "panel:timeline", "panel:simple", "panel:advanced",
                          "panel:follow", "panel:unfollow"])
        self.assertIn("Game ID: 771747666", msg.content)

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
    def __init__(self):
        self.sent: list[str] = []

    async def defer(self, ephemeral=False, thinking=False, **kwargs):
        return None

    async def send_message(self, content=None, ephemeral=False,
                           view=None, **kwargs):
        self.sent.append(content)


class FakeFollowup:
    def __init__(self):
        self.sent: list[str] = []
        self.sent_views: list = []
        self._next_id = 4000

    async def send(self, content=None, ephemeral=False, view=None, **kwargs):
        self.sent.append(content)
        self.sent_views.append(view)
        # followup.send(wait=True) returns the sent message in
        # discord.py — give the panel-as-reply an id to record.
        self._next_id += 1
        return SimpleNamespace(id=self._next_id, content=content, view=view)


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

    def __init__(self, channel_id=777, guild=None, message=None, data=None):
        self.channel_id = channel_id
        self.channel = SimpleNamespace(id=channel_id)
        self.guild = guild
        self.message = message
        self.data = data
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.user = SimpleNamespace(id=42)
        self.edited: list[tuple] = []

    async def edit_original_response(self, content=None, view=None,
                                     **kwargs):
        self.edited.append((content, view))


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
        # A tap adopts the card as a live panel in _watches — keep
        # each test isolated.
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True

    def tearDown(self):
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded

    def _view(self, game_id=None):
        return wmod.WatchAlertView(game_id)

    def _interaction(self, custom_id="watch:detail", with_game_id=True):
        # A button click on a detail reply: the message carries the
        # "Game ID:" line, the custom_id is the static one.
        content = wmod.render_detail(self.detail, wmod.DETAIL_ADVANCED)
        if not with_game_id:
            content = content.replace("Game ID: 771747666", "Game ID: ?")
        return FakeInteraction(
            message=SimpleNamespace(content=content),
            data={"custom_id": custom_id})

    def _mock_search(self, detail):
        return patch.object(
            prov, "onex_search",
            new=AsyncMock(return_value={"games": [], "detail": detail}))

    async def test_view_has_static_custom_ids(self):
        view = self._view()
        self.assertEqual([b.custom_id for b in view.children],
                         ["watch:detail", "watch:live", "panel:refresh",
                          "panel:timeline", "panel:simple", "panel:advanced",
                          "panel:follow", "panel:unfollow"])

    async def test_detail_button_resolves_game_from_message(self):
        # Static custom_id: the game comes from the message's
        # "Game ID:" line, and the fetch is for that id. The tap
        # edits the card in place — no fresh snapshot message.
        view = self._view()
        interaction = self._interaction("watch:detail")
        mock_search = AsyncMock(
            return_value={"games": [], "detail": self.detail})
        with patch.object(prov, "onex_search", mock_search):
            await view.children[0].callback(interaction)
        mock_search.assert_awaited_once_with("771747666")
        self.assertEqual(len(interaction.edited), 1)
        text = interaction.edited[0][0]
        self.assertIn("Match stats", text)
        self.assertIn("Possession", text)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", text)
        self.assertEqual(interaction.followup.sent, [])

    async def test_detail_button_updates_card_in_place(self):
        view = self._view()
        interaction = self._interaction("watch:detail")
        with self._mock_search(self.detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        text = interaction.edited[0][0]
        self.assertIn("Match stats", text)
        self.assertIn("Possession", text)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", text)
        self.assertEqual(interaction.followup.sent, [])

    async def test_live_button_switches_card_to_live_line_in_place(self):
        view = self._view()
        interaction = self._interaction("watch:live")
        with self._mock_search(self.detail):
            await view.children[1].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        text = interaction.edited[0][0]
        self.assertIn("0 – 1", text)
        self.assertIn("67:00", text)
        self.assertNotIn("Match stats", text)
        self.assertEqual(interaction.followup.sent, [])

    async def test_live_button_cricket_switches_card_in_place(self):
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
        interaction = self._interaction("watch:live")
        with self._mock_search(cricket_detail):
            await view.children[1].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        text = interaction.edited[0][0]
        self.assertIn("16.4 ov", text)
        self.assertIn("108/4", text)
        self.assertEqual(interaction.followup.sent, [])

    async def test_gone_game_says_no_longer_live(self):
        view = self._view()
        for child in view.children:
            # Unfollow never fetches the game — it only flips the
            # notification flag, and with no watch here it says so.
            if child.custom_id == "panel:unfollow":
                continue
            interaction = self._interaction(child.custom_id)
            with self._mock_search(None):
                await child.callback(interaction)
            self.assertEqual(interaction.followup.sent,
                             ["That game is no longer live."])

    async def test_unfollow_with_no_watch_says_not_watched(self):
        view = self._view()
        interaction = self._interaction("panel:unfollow")
        await view.children[7].callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["That game is not watched in this channel."])

    async def test_gone_game_score_less_detail_says_no_longer_live(self):
        gone = {"home": "", "away": "", "home_score": None,
                "away_score": None, "status": "?", "minute": None,
                "clock": "Score not available right now", "stats": None,
                "cricket": None}
        view = self._view()
        interaction = self._interaction("watch:detail")
        with self._mock_search(gone):
            await view.children[0].callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["That game is no longer live."])

    async def test_per_game_custom_id_fallback(self):
        # A button built with a game id still resolves from the
        # custom_id even when the message has no "Game ID:" line.
        view = self._view("771747666")
        interaction = FakeInteraction(
            message=SimpleNamespace(content="no id here"),
            data={"custom_id": "watch:detail:771747666"})
        mock_search = AsyncMock(
            return_value={"games": [], "detail": self.detail})
        with patch.object(prov, "onex_search", mock_search):
            await view.children[0].callback(interaction)
        mock_search.assert_awaited_once_with("771747666")
        self.assertIn("Match stats", interaction.followup.sent[0])

    async def test_no_id_anywhere_says_could_not_tell(self):
        view = self._view()
        interaction = self._interaction("watch:detail",
                                        with_game_id=False)
        mock_search = AsyncMock(
            return_value={"games": [], "detail": self.detail})
        with patch.object(prov, "onex_search", mock_search):
            await view.children[0].callback(interaction)
        mock_search.assert_not_awaited()
        self.assertEqual(interaction.followup.sent,
                         ["Could not tell which game this is."])


class TestDetailRepliesHaveButtons(unittest.IsolatedAsyncioTestCase):
    """Detail followups — /game and the /watch detail action — carry
    the 📊 Detail / 🔄 Live buttons too, not just on_tick alerts."""

    def setUp(self):
        # /game registers its live panel in sqlite, so this class
        # needs a database like the other command tests.
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp7"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True
        self.games1x2 = _load("games1x2.json")
        self.detail = prov._onex_detail_from_raw(self.games1x2[0])

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

    @staticmethod
    def _labels(view):
        return [b.label for b in view.children]

    @staticmethod
    def _ids(view):
        return [b.custom_id for b in view.children]

    async def test_game_detail_reply_has_buttons(self):
        import bot as botmod

        interaction = FakeInteraction()
        with patch.object(
                botmod, "onex_search",
                new=AsyncMock(return_value={"detail": self.detail})):
            await botmod.cmd_game.callback(interaction, "Gambia")
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn("Game ID: 771747666", interaction.followup.sent[0])
        view = interaction.followup.sent_views[0]
        self.assertIsNotNone(view)
        self.assertEqual(self._labels(view),
                         ["📊 Detail", "🔄 Live", "🔄 Refresh", "📜 Timeline",
                          "🟢 Simple", "🔵 Advanced", "🔔 Follow",
                          "🔕 Unfollow"])
        self.assertEqual(self._ids(view),
                         ["watch:detail", "watch:live", "panel:refresh",
                          "panel:timeline", "panel:simple", "panel:advanced",
                          "panel:follow", "panel:unfollow"])

    async def test_watch_detail_action_reply_has_buttons(self):
        import bot as botmod

        resolved = {"game_id": "771747666", "action": "detail",
                    "detail": "simple", "candidates": [],
                    "entry": {"id": "771747666",
                              "home": "Republic of the Gambia",
                              "away": "Ivory Coast",
                              "league": "Africa Cup of Nations",
                              "status": "2H"},
                    "source": "id", "channel": None, "channel_id": None,
                    "channel_named": None, "note": ""}
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(
                 botmod, "onex_search",
                 new=AsyncMock(return_value={"detail": self.detail})):
            await botmod.cmd_watch.callback(
                interaction, "show me the Gambia game")
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn("Game ID: 771747666", interaction.followup.sent[0])
        view = interaction.followup.sent_views[0]
        self.assertIsNotNone(view)
        self.assertEqual(self._labels(view),
                         ["📊 Detail", "🔄 Live", "🔄 Refresh", "📜 Timeline",
                          "🟢 Simple", "🔵 Advanced", "🔔 Follow",
                          "🔕 Unfollow"])
        self.assertEqual(self._ids(view),
                         ["watch:detail", "watch:live", "panel:refresh",
                          "panel:timeline", "panel:simple", "panel:advanced",
                          "panel:follow", "panel:unfollow"])


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

class TestListIntent(unittest.IsolatedAsyncioTestCase):
    """A browse request ("show any game") lists the live games
    instead of dead-ending, and a failed search suggests them."""

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

    async def test_typo_browse_lists_live_games(self):
        # The user's exact words (typo "amy" for "any"), no model.
        out = await wmod.resolve_watch("show amy new game to test", None)
        self.assertEqual(out["action"], "list")
        self.assertIsNone(out["game_id"])
        self.assertGreaterEqual(len(out["games"]), 2)
        self.assertTrue(any(g["onex_id"] == 771747666
                            for g in out["games"]))

    async def test_ai_list_action_honoured(self):
        model = FakeModel('{"game_id": null, "action": "list", '
                          '"detail": "simple", "channel_id": null, '
                          '"note": "show games"}')
        out = await wmod.resolve_watch("what is live right now", model)
        self.assertEqual(out["action"], "list")
        self.assertGreaterEqual(len(out["games"]), 2)

    async def test_nonsense_name_suggests_live_games(self):
        out = await wmod.resolve_watch("Zzqxj No Such Team", None)
        self.assertIsNone(out["game_id"])
        self.assertEqual(out["candidates"], [])
        self.assertGreaterEqual(len(out["suggestions"]), 2)
        self.assertTrue(any(g["onex_id"] == 771747666
                            for g in out["suggestions"]))

    async def test_specific_team_still_resolves_not_list(self):
        # "show me the Lumbini game" names a team — the AI path
        # still wins over the browse intent.
        model = FakeModel('{"game_id": "771800001", "action": "follow", '
                          '"detail": "simple", "channel_id": null, '
                          '"note": "Lumbini"}')
        out = await wmod.resolve_watch("show me the Lumbini game", model)
        self.assertEqual(out["action"], "follow")
        self.assertEqual(out["game_id"], "771800001")

    async def test_top_live_games_have_score_and_clock(self):
        games = await wmod.top_live_games()
        self.assertGreaterEqual(len(games), 2)
        gambia = next(g for g in games if g["onex_id"] == 771747666)
        self.assertEqual((gambia["home_score"], gambia["away_score"]),
                         (0, 1))
        self.assertEqual(gambia["clock"], "67:00")

    async def test_cmd_watch_list_reply(self):
        import bot as botmod

        resolved = {"game_id": None, "action": "list", "detail": None,
                    "candidates": [], "entry": None, "source": "list",
                    "channel": None, "channel_id": None,
                    "channel_named": None, "note": "",
                    "games": await wmod.top_live_games(),
                    "suggestions": []}
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)):
            await botmod.cmd_watch.callback(interaction, "show any game")
        text = "\n".join(interaction.followup.sent)
        self.assertIn("Live games right now", text)
        self.assertIn("771747666", text)
        self.assertIn("/watch", text)

    async def test_cmd_watch_no_match_suggests_games(self):
        import bot as botmod

        resolved = {"game_id": None, "action": "follow", "detail": None,
                    "candidates": [], "entry": None, "source": "none",
                    "channel": None, "channel_id": None,
                    "channel_named": None, "note": "",
                    "games": [], "suggestions": await wmod.top_live_games()}
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)):
            await botmod.cmd_watch.callback(
                interaction, "Zzqxj No Such Team")
        text = "\n".join(interaction.followup.sent)
        self.assertIn("could not find a game", text.lower())
        self.assertIn("771747666", text)

    async def test_cmd_game_no_match_suggests_games(self):
        import bot as botmod

        interaction = FakeInteraction()
        with patch.object(
                botmod, "onex_search",
                new=AsyncMock(return_value={"games": [], "detail": None})), \
             patch.object(botmod, "_defer",
                          new=AsyncMock(return_value=True)):
            await botmod.cmd_game.callback(
                interaction, "Zzqxj No Such Team")
        text = "\n".join(interaction.followup.sent)
        self.assertIn("No game found", text)
        self.assertIn("live games right now", text.lower())
        self.assertIn("771747666", text)


class TestSportFilter(unittest.IsolatedAsyncioTestCase):
    """Asking for football lists football only — no cricket in
    the reply. Fixtures: 2 football + 1 featured football game
    and 3 cricket games live (sport keys verified against the
    real games1x2 shape, which carries sport.name per entry)."""

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

    def test_sport_words(self):
        self.assertEqual(wmod._sport_from_words("show any football game"),
                         "Football")
        self.assertEqual(wmod._sport_from_words("soccer tonight"),
                         "Football")
        self.assertEqual(wmod._sport_from_words("any cricket live?"),
                         "Cricket")
        self.assertEqual(wmod._sport_from_words("hockey"), "Ice Hockey")
        self.assertEqual(wmod._sport_from_words("table tennis"),
                         "Table Tennis")
        # "football" must not steal "american football".
        self.assertEqual(
            wmod._sport_from_words("show any american football game"),
            "American Football")
        self.assertIsNone(wmod._sport_from_words("show amy new game"))
        self.assertIsNone(wmod._sport_from_words("Lumbini Lions"))

    def test_canonical_sport(self):
        self.assertEqual(wmod._canonical_sport("Football"), "Football")
        self.assertEqual(wmod._canonical_sport("soccer"), "Football")
        self.assertEqual(wmod._canonical_sport("Ice hockey"),
                         "Ice Hockey")
        self.assertIsNone(wmod._canonical_sport("Quidditch"))
        self.assertIsNone(wmod._canonical_sport(None))

    def test_is_sport_only_query(self):
        self.assertTrue(wmod._is_sport_only_query("football", "Football"))
        self.assertTrue(wmod._is_sport_only_query(
            "show any cricket game", "Cricket"))
        self.assertFalse(wmod._is_sport_only_query(
            "football team X", "Football"))

    async def test_football_list_only_football(self):
        out = await wmod.resolve_watch("show any football game", None)
        self.assertEqual(out["action"], "list")
        self.assertEqual(out["sport"], "Football")
        self.assertGreaterEqual(len(out["games"]), 2)
        self.assertTrue(all(g.get("sport") == "Football"
                            for g in out["games"]))
        self.assertTrue(any(g["onex_id"] == 771747666
                            for g in out["games"]))
        self.assertFalse(any(g["onex_id"] in (771739998, 771750037,
                                              771750038)
                            for g in out["games"]))

    async def test_cricket_list_only_cricket(self):
        out = await wmod.resolve_watch("show me cricket", None)
        self.assertEqual(out["action"], "list")
        self.assertEqual(out["sport"], "Cricket")
        self.assertTrue(all(g.get("sport") == "Cricket"
                            for g in out["games"]))
        self.assertTrue(any(g["onex_id"] == 771739998
                            for g in out["games"]))
        self.assertFalse(any(g["onex_id"] in (771747666, 771747706)
                            for g in out["games"]))

    async def test_sport_with_nothing_live(self):
        out = await wmod.resolve_watch("show any basketball game", None)
        self.assertEqual(out["action"], "list")
        self.assertEqual(out["sport"], "Basketball")
        self.assertEqual(out["games"], [])

    async def test_cmd_watch_list_empty_sport_says_so(self):
        import bot as botmod

        resolved = {"game_id": None, "action": "list", "detail": None,
                    "candidates": [], "entry": None, "source": "list",
                    "channel": None, "channel_id": None,
                    "channel_named": None, "note": "",
                    "games": [], "suggestions": [],
                    "sport": "Basketball"}
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)):
            await botmod.cmd_watch.callback(
                interaction, "show any basketball game")
        text = "\n".join(interaction.followup.sent)
        self.assertEqual(text, "No basketball games live right now.")

    async def test_cmd_watch_list_sport_header(self):
        import bot as botmod

        resolved = {"game_id": None, "action": "list", "detail": None,
                    "candidates": [], "entry": None, "source": "list",
                    "channel": None, "channel_id": None,
                    "channel_named": None, "note": "",
                    "games": await wmod.top_live_games(sport="Football"),
                    "suggestions": [], "sport": "Football"}
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)):
            await botmod.cmd_watch.callback(
                interaction, "show any football game")
        text = "\n".join(interaction.followup.sent)
        self.assertIn("Live football right now", text)
        self.assertIn("771747666", text)
        self.assertNotIn("771739998", text)

    async def test_ai_list_sport_honoured(self):
        model = FakeModel('{"game_id": null, "action": "list", '
                          '"sport": "Football", "detail": "simple", '
                          '"channel_id": null, "note": "football"}')
        out = await wmod.resolve_watch("show me football games", model)
        self.assertEqual(out["action"], "list")
        self.assertEqual(out["sport"], "Football")
        self.assertTrue(all(g.get("sport") == "Football"
                            for g in out["games"]))

    async def test_ai_unknown_sport_means_no_filter(self):
        model = FakeModel('{"game_id": null, "action": "list", '
                          '"sport": "Quidditch", "detail": "simple", '
                          '"channel_id": null, "note": "games"}')
        out = await wmod.resolve_watch("show me some games", model)
        self.assertEqual(out["action"], "list")
        self.assertIsNone(out["sport"])
        sports = {g.get("sport") for g in out["games"]}
        self.assertIn("Football", sports)
        self.assertIn("Cricket", sports)

    async def test_ai_prompt_grounds_sport(self):
        grounding = await prov.onex_grounding()
        model = FakeModel('{"game_id": "771747666", "action": "follow", '
                          '"detail": "simple", "note": "Gambia"}')
        out = await wmod.parse_watch_ai("gambia", grounding, model)
        self.assertEqual(out["game_id"], "771747666")
        prompt = model.prompts[0]
        self.assertIn("771747666 | Republic of the Gambia vs Ivory Coast "
                      "| Africa Cup of Nations | Football |", prompt)
        self.assertIn("| Cricket |", prompt)

    async def test_game_bare_sport_returns_pick_list(self):
        import bot as botmod

        interaction = FakeInteraction()
        with patch.object(botmod, "_defer",
                          new=AsyncMock(return_value=True)):
            await botmod.cmd_game.callback(interaction, "football")
        text = "\n".join(interaction.followup.sent)
        self.assertIn("Live football right now", text)
        self.assertIn("771747666", text)
        self.assertNotIn("771739998", text)

    async def test_game_bare_sport_nothing_live(self):
        import bot as botmod

        interaction = FakeInteraction()
        with patch.object(botmod, "_defer",
                          new=AsyncMock(return_value=True)):
            await botmod.cmd_game.callback(interaction, "basketball")
        text = "\n".join(interaction.followup.sent)
        self.assertEqual(text, "No basketball games live right now.")

    async def test_game_no_match_suggestions_filtered_by_sport(self):
        import bot as botmod

        interaction = FakeInteraction()
        with patch.object(
                botmod, "onex_search",
                new=AsyncMock(return_value={"games": [], "detail": None})), \
             patch.object(botmod, "_defer",
                          new=AsyncMock(return_value=True)):
            await botmod.cmd_game.callback(
                interaction, "Zzqxj football team")
        text = "\n".join(interaction.followup.sent)
        self.assertIn("live football games right now", text)
        self.assertIn("771747666", text)
        self.assertNotIn("771739998", text)

    async def test_watch_no_match_sport_empty_says_so(self):
        import bot as botmod

        out = await wmod.resolve_watch(
            "follow the basketball team Zzqxj", None)
        self.assertIsNone(out["game_id"])
        self.assertEqual(out["sport"], "Basketball")
        self.assertEqual(out["suggestions"], [])
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=out)):
            await botmod.cmd_watch.callback(
                interaction, "follow the basketball team Zzqxj")
        text = "\n".join(interaction.followup.sent)
        self.assertEqual(text, "No basketball games live right now.")

    async def test_plain_browse_still_mixed(self):
        out = await wmod.resolve_watch("show amy new game to test", None)
        self.assertEqual(out["action"], "list")
        self.assertIsNone(out["sport"])
        sports = {g.get("sport") for g in out["games"]}
        self.assertIn("Football", sports)
        self.assertIn("Cricket", sports)


def _live_match(home, away, hs=None, aws=None, minute=None,
                provider="onexbet"):
    """A /live Match dict, as _onex_norm_match builds it."""
    return {"id": f"m_{home}", "sofa_id": None, "apf_id": None,
            "onex_id": None, "home": home, "away": away,
            "home_norm": home.lower(), "away_norm": away.lower(),
            "home_score": hs, "away_score": aws,
            "home_logo": "", "away_logo": "",
            "status": "2H" if minute is not None else "?",
            "status_code": 0, "minute": minute,
            "league": "Test League", "league_id": 0, "sofa_lid": 0,
            "date": "", "stadium": "", "referee": "",
            "provider": provider, "finished": False}


class TestAllLiveMatches(unittest.IsolatedAsyncioTestCase):
    """Feature B, provider side: onex_all_live_matches returns the
    FULL live list (games1x2 + topGamesByStat), not just featured."""

    def setUp(self):
        self._orig_get = prov.hc.get
        self.games1x2 = _load("games1x2.json")
        self.top = _load("top.json")
        self.line_npl = _load("line_npl.json")
        self.gameinfo = {"771747666": _load("gameinfo_771747666.json")}
        self.stub = StubHttp(self)
        prov.hc.get = self.stub.get
        prov._onex_reset_caches()

    def tearDown(self):
        prov.hc.get = self._orig_get
        prov._onex_reset_caches()

    async def test_full_list_beats_featured(self):
        all_live = await prov.onex_all_live_matches()
        featured = await prov.onex_live_matches()
        ids = {str(m["onex_id"]) for m in all_live}
        # 5 games1x2 + 1 game only in topGamesByStat.
        self.assertEqual(len(all_live), 6)
        self.assertIn("771747999", ids)
        self.assertEqual(len(featured), 2)
        self.assertTrue(len(all_live) > len(featured))

    async def test_empty_when_nothing_live(self):
        self.games1x2 = []
        self.top = []
        prov._onex_reset_caches()
        self.assertEqual(await prov.onex_all_live_matches(), [])


class TestLiveMerge(unittest.TestCase):
    """Feature B, merge side: dedup kept, scored games first."""

    def test_dedup_keeps_first_provider(self):
        first = _live_match("Alpha", "Beta", 1, 0, 55,
                            provider="sportsdb")
        dup = _live_match("Alpha", "Beta", 1, 0, 55,
                          provider="onexbet")
        import bot as botmod
        out = botmod.merge_live_matches([first], [], [], [dup], [])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["provider"], "sportsdb")

    def test_scored_games_first(self):
        plain = _live_match("Plain", "Team")
        scored = _live_match("Scored", "Team", 2, 1, 70)
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], [plain, scored], [])
        self.assertEqual([m["home"] for m in out],
                         ["Scored", "Plain"])

    def test_zero_zero_counts_as_scored(self):
        plain = _live_match("Plain", "Team")
        nil = _live_match("Nil", "Draw", 0, 0, 12)
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], [plain, nil], [])
        self.assertEqual(out[0]["home"], "Nil")

    # ── 18 Nov 2026 regression: /live showed 15 scheduled fixtures
    # (Man City vs Fulham, Bournemouth vs Sunderland, ...) with
    # "...and 95 more live matches" — every one dated Nov 20–24.
    # /live must list ONLY genuinely live games.
    def test_scheduled_match_dropped(self):
        sched = _live_match("Man City", "Fulham", provider="espn")
        sched["status"] = "Scheduled"
        sched["date"] = "2026-11-22"
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], [], [sched])
        self.assertEqual(out, [])

    def test_scheduled_date_status_dropped(self):
        # ESPN's shortDetail for a pre-game event is a date string,
        # not the word "Scheduled" — the gate must still drop it.
        sched = _live_match("Bournemouth", "Sunderland", provider="espn")
        sched["status"] = "Sat, Nov 22"
        sched["date"] = "2026-11-22"
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], [], [sched])
        self.assertEqual(out, [])

    def test_finished_match_dropped(self):
        done = _live_match("Done", "Team", 2, 1, 90, provider="apifootball")
        done["finished"] = True
        done["status"] = "FT"
        import bot as botmod
        out = botmod.merge_live_matches([], [], [done], [], [])
        self.assertEqual(out, [])

    def test_live_match_kept(self):
        live = _live_match("Live", "Team", 1, 0, 67, provider="espn")
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], [], [live])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["home"], "Live")

    def test_onex_feed_match_without_minute_kept(self):
        # The 1xBet live feed is live-only by construction, so a
        # match from it passes even before a minute/score exists.
        plain = _live_match("Plain", "Team")
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], [plain], [])
        self.assertEqual(len(out), 1)

    def test_all_scheduled_incident_reproduced(self):
        # The 18 Nov incident: 15 scheduled ESPN fixtures, nothing
        # live anywhere — /live must answer with an empty list (the
        # command then says "No live matches right now").
        scheduled = []
        for i in range(15):
            m = _live_match(f"Home{i:02d}", f"Away{i:02d}", provider="espn")
            m["status"] = "Scheduled"
            m["date"] = "2026-11-22"
            scheduled.append(m)
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], [], scheduled)
        self.assertEqual(out, [])

    def test_football_first_when_feed_is_cricket_heavy(self):
        # 18 Nov 2026, 1:45 PM: /live showed 15 games, ALL cricket,
        # zero football — 1xBet's feed is cricket-heavy and filled
        # the cap. Live football must sort first, then other sports.
        cricket = [_live_match(f"CricketHome{i}", f"CricketAway{i}",
                               100 + i, 90, provider="onexbet")
                   for i in range(15)]
        for m in cricket:
            m["sport"] = "Cricket"
        foot = _live_match("FootHome", "FootAway", 1, 0, 67,
                           provider="onexbet")
        foot["sport"] = "Football"
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], cricket + [foot], [])
        self.assertEqual(out[0]["home"], "FootHome")
        self.assertEqual(
            [m["home"] for m in out[:1]], ["FootHome"])
        # ...and the cap still shows football first, not cricket.
        self.assertEqual(
            [m.get("sport") for m in out[:15]],
            ["Football"] + ["Cricket"] * 14)

    def test_unknown_sport_sorts_after_football(self):
        foot = _live_match("FootHome", "FootAway", 1, 0, 67,
                           provider="onexbet")
        foot["sport"] = "Football"
        unknown = _live_match("UnknownHome", "UnknownAway", 1, 0, 67,
                              provider="onexbet")
        import bot as botmod
        out = botmod.merge_live_matches([], [], [], [unknown, foot], [])
        self.assertEqual([m["home"] for m in out],
                         ["FootHome", "UnknownHome"])


class TestEspnLive(unittest.IsolatedAsyncioTestCase):
    """espn_live returns live games only — the scoreboard's
    scheduled ("pre") and finished ("post") events are dropped at
    the source (18 Nov 2026 regression)."""

    def _espn_event(self, eid, home, away, state, detail, hs=None, aws=None):
        return {"id": str(eid), "name": "Premier League",
                "competitions": [{"date": "2026-11-22T13:00Z",
                    "status": {"type": {"state": state,
                                        "shortDetail": detail,
                                        "completed": state == "post"}},
                    "competitors": [
                        {"homeAway": "home", "score": hs,
                         "team": {"id": "1", "displayName": home,
                                  "logo": ""}},
                        {"homeAway": "away", "score": aws,
                         "team": {"id": "2", "displayName": away,
                                  "logo": ""}}],
                    "venue": {"fullName": ""}, "details": []}]}

    async def test_only_in_progress_returned(self):
        data = {"events": [
            self._espn_event(1, "Man City", "Fulham", "pre", "Sat, Nov 22"),
            self._espn_event(2, "Arsenal", "Chelsea", "in", "67'", 2, 1),
            self._espn_event(3, "Liverpool", "Everton", "post",
                             "Full Time", 3, 0),
        ]}
        orig_get = prov.hc.get

        async def stub(url, headers, params=None, cache_key="",
                       provider="x", refresh_fn=None):
            return data

        prov.hc.get = stub
        try:
            matches, events = await prov.espn_live("eng.1", 39)
        finally:
            prov.hc.get = orig_get
        self.assertEqual([m["home"] for m in matches], ["Arsenal"])
        self.assertEqual(matches[0]["minute"], 67)
        self.assertEqual(matches[0]["home_score"], 2)
        self.assertEqual(matches[0]["away_score"], 1)


class _RecordingFollowup:
    """FakeFollowup that also keeps embeds (FakeFollowup drops them)."""

    def __init__(self):
        self.embeds = []
        self.texts = []
        self.views = []

    async def send(self, content=None, ephemeral=False, view=None,
                   **kwargs):
        if kwargs.get("embed") is not None:
            self.embeds.append(kwargs["embed"])
        if content is not None:
            self.texts.append(content)
        self.views.append(view)


class TestLiveCommand(unittest.IsolatedAsyncioTestCase):
    """Feature B, command side: /live renders the full list, capped
    at 15, and falls back to featured when the full list is empty."""

    def _run_live(self, interaction, onex_all, featured):
        import bot as botmod

        return patch.object(botmod, "sportsdb_live_matches",
                            new=AsyncMock(return_value=[])), \
            patch.object(botmod, "sofa_live_matches",
                         new=AsyncMock(return_value=[])), \
            patch.object(botmod, "apf_live",
                         new=AsyncMock(return_value=[])), \
            patch.object(botmod, "onex_all_live_matches",
                         new=AsyncMock(return_value=onex_all)), \
            patch.object(botmod, "onex_live_matches",
                         new=AsyncMock(return_value=featured)), \
            patch.object(botmod, "espn_live",
                         new=AsyncMock(return_value=([], None)))

    async def test_full_list_capped_at_fifteen(self):
        import bot as botmod

        plains = [_live_match(f"Plain{i:02d}", "Opp") for i in range(10)]
        scored = [_live_match(f"Scored{i:02d}", "Opp", 1, 0, 60)
                  for i in range(10)]
        interaction = FakeInteraction()
        interaction.followup = _RecordingFollowup()
        patches = self._run_live(interaction, plains + scored, [])
        with patches[0], patches[1], patches[2], \
                patches[3], patches[4], patches[5]:
            await botmod.cmd_live.callback(interaction)
        titles = " ".join(e.title for e in interaction.followup.embeds)
        self.assertEqual(len(interaction.followup.embeds), 15)
        self.assertEqual(interaction.followup.texts,
                         ["…and 5 more live matches."])
        # All 10 scored games shown first, then the first 5 plains.
        for i in range(10):
            self.assertIn(f"Scored{i:02d}", titles)
        self.assertIn("Plain04", titles)
        self.assertNotIn("Plain05", titles)

    async def test_empty_full_list_falls_back_to_featured(self):
        import bot as botmod

        featured = [_live_match("Feat", "Team", 1, 1, 33)]
        interaction = FakeInteraction()
        interaction.followup = _RecordingFollowup()
        patches = self._run_live(interaction, [], featured)
        with patches[0], patches[1], patches[2], \
                patches[3], patches[4], patches[5]:
            await botmod.cmd_live.callback(interaction)
        titles = [e.title for e in interaction.followup.embeds]
        self.assertEqual(len(titles), 1)
        self.assertIn("Feat", titles[0])
        self.assertEqual(interaction.followup.texts, [])

    async def test_no_games_at_all(self):
        import bot as botmod

        interaction = FakeInteraction()
        interaction.followup = _RecordingFollowup()
        patches = self._run_live(interaction, [], [])
        with patches[0], patches[1], patches[2], \
                patches[3], patches[4], patches[5]:
            await botmod.cmd_live.callback(interaction)
        self.assertEqual(interaction.followup.embeds, [])
        self.assertEqual(interaction.followup.texts,
                         ["No live matches right now."])


class TestBarcaAutoWatch(unittest.IsolatedAsyncioTestCase):
    """Feature A: a live Barcelona game starts a silent SIMPLE watch
    in #general — no announcement, no pre-match chatter. The first
    message is whatever the normal on-change alerts send."""

    def setUp(self):
        import barcelona_reminder as barca

        self._barca = barca
        self._orig_channel = barca.GENERAL_CHANNEL_ID
        barca.GENERAL_CHANNEL_ID = 777
        self._orig_get = prov.hc.get
        self.games1x2 = _load("games1x2_barca.json")
        self.top = _load("top.json")
        self.line_npl = _load("line_npl.json")
        self.gameinfo = {}
        self.stub = StubHttp(self)
        prov.hc.get = self.stub.get
        prov._onex_reset_caches()
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp3"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        self._orig_check = wmod._barca_last_check
        wmod._watches = {}
        wmod._loaded = True
        wmod._barca_last_check = 0.0
        wmod._channel_last_edit.clear()
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        wmod._barca_last_check = self._orig_check
        self._barca.GENERAL_CHANNEL_ID = self._orig_channel
        prov.hc.get = self._orig_get
        prov._onex_reset_caches()
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    async def test_starts_once_for_live_barca_game(self):
        await wmod.on_tick(self.bot, self.games1x2)
        self.assertEqual(len(wmod._watches), 1)
        key, entry = next(iter(wmod._watches.items()))
        self.assertEqual(key, (777, "771800001"))
        self.assertEqual(entry["detail"], "simple")
        self.assertEqual(entry["home"], "FC Barcelona")
        rows = await db.list_watches(777)
        self.assertEqual(len(rows), 1)
        # Silent: no announcement or alert — but the live panel is
        # posted once when the watch starts (it ticks in place
        # after that, same as any other watch).
        self.assertEqual(len(self.channel.sent), 1)
        panel = self.channel.sent[0]
        self.assertIn("🔴 LIVE", panel.content)
        self.assertIn("FC Barcelona", panel.content)
        self.assertIsNotNone(panel.view)
        # Second tick: still one watch, still one message — the
        # panel edits in place, nothing new is sent.
        wmod._barca_last_check = 0.0
        await wmod.on_tick(self.bot, self.games1x2)
        self.assertEqual(len(wmod._watches), 1)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_no_start_when_barca_not_live(self):
        self.games1x2 = _load("games1x2.json")
        prov._onex_reset_caches()
        await wmod.on_tick(self.bot, self.games1x2)
        self.assertEqual(wmod._watches, {})
        self.assertEqual(len(self.channel.sent), 0)

    async def test_no_double_start_when_already_watched(self):
        # A manual watch for the same game in #general (advanced):
        # the auto-watch must not start a second one.
        snap = wmod.snapshot_from_detail(
            {"home": "FC Barcelona", "away": "Real Madrid",
             "home_score": 1, "away_score": 0, "status": "2H",
             "minute": 65, "clock": "65:00", "stats": None,
             "cricket": None})
        snap["_home"], snap["_away"] = "FC Barcelona", "Real Madrid"
        wmod._watches[(777, "771800001")] = {
            "home": "FC Barcelona", "away": "Real Madrid",
            "league": "Spain. LaLiga", "detail": "advanced", "fmt": "",
            "snapshot": snap, "missing": 0,
        }
        await wmod.on_tick(self.bot, self.games1x2)
        self.assertEqual(len(wmod._watches), 1)
        self.assertEqual(
            wmod._watches[(777, "771800001")]["detail"], "advanced")

    async def test_upcoming_barca_does_not_start(self):
        upcoming = {"onex_id": 771800002, "home": "FC Barcelona",
                    "away": "Villarreal", "league": "Spain. LaLiga",
                    "status": "upcoming", "home_score": None,
                    "away_score": None}
        with patch.object(wmod.prov, "onex_search",
                          new=AsyncMock(return_value={"games": [upcoming],
                                                      "detail": None})):
            await wmod.on_tick(self.bot, [])
        self.assertEqual(wmod._watches, {})

    async def test_barca_b_does_not_start(self):
        b_team = {"onex_id": 771800003, "home": "FC Barcelona B",
                  "away": "Someone", "league": "Spain. Segunda",
                  "status": "2H", "home_score": 1, "away_score": 0}
        with patch.object(wmod.prov, "onex_search",
                          new=AsyncMock(return_value={"games": [b_team],
                                                      "detail": None})):
            await wmod.on_tick(self.bot, [])
        self.assertEqual(wmod._watches, {})

    async def test_check_throttled_to_thirty_seconds(self):
        self.games1x2 = _load("games1x2.json")
        prov._onex_reset_caches()
        with patch.object(wmod.prov, "onex_search",
                          wraps=wmod.prov.onex_search) as spy:
            await wmod.on_tick(self.bot, [])
            await wmod.on_tick(self.bot, [])
            self.assertEqual(spy.call_count, 1)
            wmod._barca_last_check = 0.0
            await wmod.on_tick(self.bot, [])
            self.assertEqual(spy.call_count, 2)

    async def test_no_search_when_channel_not_configured(self):
        self._barca.GENERAL_CHANNEL_ID = 0
        with patch.object(wmod.prov, "onex_search",
                          new=AsyncMock(return_value={"games": [],
                                                      "detail": None})) as spy:
            await wmod.on_tick(self.bot, [])
            spy.assert_not_called()


class TestDetectEventsEngine(unittest.TestCase):
    """Feature 1: detect_events kinds, dedup keys, corrections,
    second-half start, terminal statuses."""

    def test_score_down_is_correction_not_goal(self):
        old = _snap("2", "0")
        new = _snap("1", "0")
        events = wmod.detect_events(old, new, "simple", "111")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "correction")
        self.assertIn("Score corrected", events[0]["text"])
        self.assertNotIn("GOAL", events[0]["text"])

    def test_cricket_runs_down_is_correction(self):
        old = _snap("120/2", "0/0", status="live")
        new = _snap("100/2", "0/0", status="live")
        events = wmod.detect_events(old, new, "simple", "111")
        self.assertTrue(any(e["kind"] == "correction" for e in events))
        self.assertFalse(any(e["kind"] == "goal" for e in events))

    def test_second_half_start(self):
        old = _snap("1", "0", status="HT")
        new = _snap("1", "0", status="2H")
        events = wmod.detect_events(old, new, "simple", "111")
        self.assertTrue(any(e["kind"] == "second_half" for e in events))
        self.assertTrue(any("Second-half" in e["text"] for e in events))

    def test_half_time(self):
        old = _snap("1", "0", status="2H")
        new = _snap("1", "0", status="HT")
        events = wmod.detect_events(old, new, "simple", "111")
        self.assertTrue(any(e["kind"] == "ht" for e in events))

    def test_penalty_fires_at_both_levels(self):
        old = _snap("0", "0", stats={"Penalty": ("0", "0")})
        new = _snap("0", "0", stats={"Penalty": ("1", "0")})
        for level in ("simple", "advanced"):
            events = wmod.detect_events(old, new, level, "111")
            self.assertTrue(any(e["kind"] == "penalty" for e in events),
                            level)

    def test_substitution_advanced_only(self):
        old = _snap("0", "0", stats={"Substitutions": ("1", "1")})
        new = _snap("0", "0", stats={"Substitutions": ("2", "1")})
        self.assertEqual(
            [e for e in wmod.detect_events(old, new, "simple", "111")
             if e["kind"] == "sub"], [])
        self.assertTrue(any(
            e["kind"] == "sub"
            for e in wmod.detect_events(old, new, "advanced", "111")))

    def test_terminal_statuses(self):
        for status, kind, label in (
                ("postponed", "postponed", "Match postponed"),
                ("suspended", "suspended", "Match suspended"),
                ("abandoned", "abandoned", "Match abandoned"),
                ("cancelled", "cancelled", "Match cancelled")):
            old = _snap("0", "0", status="2H")
            new = _snap("0", "0", status=status)
            events = wmod.detect_events(old, new, "simple", "111")
            self.assertTrue(any(e["kind"] == kind for e in events), status)
            self.assertTrue(any(label in e["text"] for e in events), status)

    def test_dedup_keys_stable_and_game_scoped(self):
        old = _snap("0", "0")
        new = _snap("1", "0", minute=67)
        first = wmod.detect_events(old, new, "simple", "111")
        second = wmod.detect_events(old, new, "simple", "111")
        other_game = wmod.detect_events(old, new, "simple", "222")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["key"], second[0]["key"])
        self.assertNotEqual(first[0]["key"], other_game[0]["key"])
        self.assertIn("111", first[0]["key"])

    def test_ft_early_return_suppresses_score_event(self):
        old = _snap("1", "0", status="2H")
        new = _snap("2", "0", status="FT")
        events = wmod.detect_events(old, new, "simple", "111")
        self.assertEqual([e["kind"] for e in events], ["ft"])


class TestOnTickEngine(unittest.IsolatedAsyncioTestCase):
    """Feature 1 on the tick: prefs enforcement, restart-safe
    dedup, terminal ends the watch and records history."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp3"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True
        wmod._channel_last_edit.clear()
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
        # The watch already has its live panel (see TestOnTick).
        detail = prov._onex_detail_from_raw(self.games1x2[0])
        panel_body = wmod.render_panel(detail, "67:00")
        msg = FakeMessage(2001, panel_body)
        self.channel.messages[2001] = msg
        wmod._watches[(777, game_id)] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": level, "fmt": "",
            "snapshot": snap, "missing": 0,
            "panel_message_id": 2001, "panel_body": panel_body,
            "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }

    def _changed_raw(self, **score_changes):
        changed = json.loads(json.dumps(self.games1x2[0]))
        for key, value in score_changes.items():
            changed["scores"][key] = value
        return changed

    async def test_paused_prefs_silence_goal(self):
        self._seed_watch()
        await db.set_prefs(777, "771747666", paused=True)
        await wmod.on_tick(self.bot, [self._changed_raw(scoreOpp1=1)])
        self.assertEqual(len(self.channel.sent), 0)
        # The watch itself survives a paused tick.
        self.assertIn((777, "771747666"), wmod._watches)

    async def test_prefs_event_filter_blocks_yellow_allows_goal(self):
        self._seed_watch()
        await db.set_prefs(777, "771747666", events=["goal"])
        # Yellow cards 3 -> 4 (fixture tabloStats row 0): no send.
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["tabloStats"]["0"][0]["s1"] = "4"
        await wmod.on_tick(self.bot, [changed])
        self.assertEqual(len(self.channel.sent), 0)
        # A goal still sends.
        await wmod.on_tick(self.bot, [self._changed_raw(scoreOpp1=1)])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)

    async def test_event_not_resent_after_restart(self):
        import history
        self._seed_watch()
        await wmod.on_tick(self.bot, [self._changed_raw(scoreOpp1=1)])
        self.assertEqual(len(self.channel.sent), 1)
        # Simulate a restart: the watch reloads with its OLD
        # snapshot, but the event key is already in the history
        # table, so the same tick must not send again.
        self._seed_watch()
        await wmod.on_tick(self.bot, [self._changed_raw(scoreOpp1=1)])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertTrue(await history.event_seen(
            "771747666|goal|1–1|67:00"))

    async def test_ft_ends_watch_and_records_history(self):
        import history
        self._seed_watch()
        changed = self._changed_raw()
        changed["scores"]["statusLineStr"] = "Full time"
        await wmod.on_tick(self.bot, [changed])
        self.assertNotIn((777, "771747666"), wmod._watches)
        matches = await history.recent_matches(limit=5)
        self.assertTrue(any(m["game_id"] == "771747666"
                            for m in matches))

    async def test_score_correction_message_on_tick(self):
        self._seed_watch()
        # Away score 1 -> 0: a correction, never a goal.
        await wmod.on_tick(self.bot, [self._changed_raw(scoreOpp2=0)])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("Score corrected", self.channel.sent[0].content)
        self.assertNotIn("GOAL", self.channel.sent[0].content)

    async def test_gone_game_ends_watch_and_records_history(self):
        import history
        self._seed_watch()
        for _ in range(wmod.MISSING_TICKS_TO_END):
            await wmod.on_tick(self.bot, [])
        self.assertNotIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("no longer live", self.channel.sent[0].content)
        matches = await history.recent_matches(limit=5)
        self.assertTrue(any(m["game_id"] == "771747666"
                            and m["status"] == "ended" for m in matches))


class TestPanelButtons(unittest.IsolatedAsyncioTestCase):
    """Feature 2: Refresh / Timeline / Simple / Advanced /
    Follow / Unfollow on the watch panel."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp4"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True
        wmod._refresh_cooldown.clear()
        self.games1x2 = _load("games1x2.json")
        self.detail = prov._onex_detail_from_raw(self.games1x2[0])

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

    def _view(self, game_id=None):
        return wmod.WatchAlertView(game_id)

    def _interaction(self, custom_id, content=None):
        if content is None:
            content = wmod.render_detail(self.detail, wmod.DETAIL_ADVANCED)
        return FakeInteraction(
            message=SimpleNamespace(content=content),
            data={"custom_id": custom_id})

    def _mock_search(self, detail):
        return patch.object(
            prov, "onex_search",
            new=AsyncMock(return_value={"games": [], "detail": detail}))

    def _seed_watch(self, game_id="771747666"):
        snap = wmod.snapshot_from_detail(
            {"home": "Republic of the Gambia", "away": "Ivory Coast",
             "home_score": 0, "away_score": 1, "status": "2H", "minute": 67,
             "clock": "67'", "stats": [], "cricket": None})
        snap["_home"], snap["_away"] = "Republic of the Gambia", "Ivory Coast"
        wmod._watches[(777, game_id)] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": "simple",
            "fmt": "", "snapshot": snap, "missing": 0,
            "panel_message_id": None, "panel_body": None,
            "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }

    async def test_refresh_edits_detail_card_in_place(self):
        view = self._view()
        interaction = self._interaction("panel:refresh")
        new_detail = dict(self.detail)
        new_detail["home_score"] = 1
        with self._mock_search(new_detail):
            await view.children[2].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        content, edited_view = interaction.edited[0]
        self.assertIn("1 – 1", content)
        self.assertIn("Game ID: 771747666", content)
        self.assertIsInstance(edited_view, wmod.WatchAlertView)
        self.assertEqual(len(interaction.followup.sent), 0)

    async def test_refresh_cooldown_blocks_second_click(self):
        view = self._view()
        first = self._interaction("panel:refresh")
        with self._mock_search(self.detail):
            await view.children[2].callback(first)
        second = self._interaction("panel:refresh")
        with self._mock_search(self.detail):
            await view.children[2].callback(second)
        self.assertEqual(second.response.sent,
                         ["Give it a few seconds before refreshing again."])
        self.assertEqual(len(second.edited), 0)

    async def test_refresh_on_alert_message_sends_live_line(self):
        view = self._view()
        alert = ("⚽ **GOAL** — Republic of the Gambia 1–1 Ivory Coast (67')\n"
                 "Game ID: 771747666")
        interaction = self._interaction("panel:refresh", content=alert)
        with self._mock_search(self.detail):
            await view.children[2].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn("67:00", interaction.followup.sent[0])
        self.assertEqual(len(interaction.edited), 0)

    async def test_timeline_lists_events(self):
        view = self._view()
        interaction = self._interaction("panel:timeline")
        events = [{"minute_int": 23, "minute": "23", "icon": "⚽",
                   "detail": "Goal", "player": "S. Mane",
                   "team": "Ivory Coast"},
                  {"minute_int": 41, "minute": "41", "icon": "🟨",
                   "detail": "Yellow Card", "player": "J. Doe",
                   "team": "Republic of the Gambia"}]
        with self._mock_search(self.detail), \
                patch.object(prov, "onex_events",
                             new=AsyncMock(return_value=events)):
            await view.children[3].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Goal", text)
        self.assertIn("23'", text)
        self.assertIn("Yellow Card", text)

    async def test_timeline_empty_says_none_yet(self):
        view = self._view()
        interaction = self._interaction("panel:timeline")
        with self._mock_search(self.detail), \
                patch.object(prov, "onex_events",
                             new=AsyncMock(return_value=[])):
            await view.children[3].callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["No timeline events yet for this game."])

    async def test_simple_button_switches_card_in_place_no_stats(self):
        view = self._view()
        interaction = self._interaction("panel:simple")
        with self._mock_search(self.detail):
            await view.children[4].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        self.assertNotIn("Match stats", interaction.edited[0][0])
        self.assertEqual(interaction.followup.sent, [])

    async def test_advanced_button_switches_card_to_analytics_in_place(self):
        view = self._view()
        interaction = self._interaction("panel:advanced")
        with self._mock_search(self.detail):
            await view.children[5].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        text = interaction.edited[0][0]
        self.assertIn("Match statistics", text)
        self.assertIn("Possession", text)
        self.assertEqual(interaction.followup.sent, [])

    async def test_follow_starts_watch(self):
        view = self._view()
        interaction = self._interaction("panel:follow")
        with self._mock_search(self.detail):
            await view.children[6].callback(interaction)
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn("Watching", interaction.followup.sent[0])

    async def test_follow_already_watched(self):
        self._seed_watch()
        view = self._view()
        interaction = self._interaction("panel:follow")
        with self._mock_search(self.detail):
            await view.children[6].callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["That game is already watched in this channel."])

    async def test_unfollow_keeps_panel_stops_alerts(self):
        self._seed_watch()
        view = self._view()
        interaction = self._interaction("panel:unfollow")
        await view.children[7].callback(interaction)
        # The watch stays — only the alerts stop; the panel keeps
        # ticking until full-time.
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertFalse(wmod._watches[(777, "771747666")]["notify"])
        self.assertIn("Notifications off", interaction.followup.sent[0])

    async def test_unfollow_not_watched(self):
        view = self._view()
        interaction = self._interaction("panel:unfollow")
        with self._mock_search(self.detail):
            await view.children[7].callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["That game is not watched in this channel."])


class TestWatchPanel(unittest.IsolatedAsyncioTestCase):
    """The live watch panel: one message per watched game, edited
    in place every second; goals and big events still send a
    separate notification; full-time stops the ticking."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp5"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        self._orig_cooldown = dict(wmod._refresh_cooldown)
        wmod._watches = {}
        wmod._loaded = True
        wmod._refresh_cooldown = {}
        wmod._channel_last_edit.clear()
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
        wmod._refresh_cooldown = self._orig_cooldown
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _seed_watch(self, game_id="771747666", with_panel=True):
        snap = wmod.snapshot_from_detail(
            {"home": "Republic of the Gambia", "away": "Ivory Coast",
             "home_score": 0, "away_score": 1, "status": "2H", "minute": 67,
             "clock": "67'", "stats": [{"name": "Yellow cards", "home": "3", "away": "1"},
                                       {"name": "Red card", "home": "0", "away": "0"}],
             "cricket": None})
        snap["_home"], snap["_away"] = "Republic of the Gambia", "Ivory Coast"
        panel_id = 2001 if with_panel else None
        panel_body = None
        if with_panel:
            detail = prov._onex_detail_from_raw(self.games1x2[0])
            panel_body = wmod.render_panel(detail, "67:00")
            self.channel.messages[2001] = FakeMessage(2001, panel_body)
        wmod._watches[(777, game_id)] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": "simple", "fmt": "",
            "snapshot": snap, "missing": 0,
            "panel_message_id": panel_id, "panel_body": panel_body,
            "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }

    async def test_first_tick_posts_one_panel(self):
        self._seed_watch(with_panel=False)
        await wmod.on_tick(self.bot, self.games1x2)
        self.assertEqual(len(self.channel.sent), 1)
        panel = self.channel.sent[0]
        self.assertIn("🔴 LIVE", panel.content)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast",
                      panel.content)
        self.assertIn("67:00", panel.content)
        self.assertIn("Game ID: 771747666", panel.content)
        self.assertIsNotNone(panel.view)
        labels = [b.label for b in panel.view.children]
        self.assertEqual(labels, ["📊 Detail", "🔄 Live", "🔄 Refresh",
                                  "📜 Timeline", "🟢 Simple", "🔵 Advanced",
                                  "🔔 Follow", "🔕 Unfollow"])
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_message_id"], panel.id)
        self.assertEqual(watch["panel_body"], panel.content)

    async def test_clock_only_change_edits_panel_no_new_message(self):
        self._seed_watch(with_panel=False)
        await wmod.on_tick(self.bot, self.games1x2)
        self.assertEqual(len(self.channel.sent), 1)
        panel = self.channel.sent[0]
        self.assertIn("67:00", panel.content)
        watch = wmod._watches[(777, "771747666")]
        # Five seconds pass with no score change: only the clock moves.
        watch["panel_timer_ts"] = time.monotonic() - 5
        watch["last_panel_edit"] = time.monotonic() - 2
        await wmod.on_tick(self.bot, self.games1x2)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(len(panel.edits), 1)
        self.assertIn("67:05", panel.edits[0])

    async def test_goal_sends_alert_and_edits_panel(self):
        self._seed_watch(with_panel=True)
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        # One new message: the GOAL alert. The panel edits in place.
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)
        panel = self.channel.messages[2001]
        self.assertEqual(len(panel.edits), 1)
        self.assertIn("1 – 1", panel.edits[0])
        self.assertIn("67:00", panel.edits[0])

    async def test_full_time_panel_shows_final_and_watch_ends(self):
        self._seed_watch(with_panel=True)
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["statusLineStr"] = "Full time"
        await wmod.on_tick(self.bot, [changed])
        self.assertNotIn((777, "771747666"), wmod._watches)
        panel = self.channel.messages[2001]
        self.assertIn("🏁 Full-time", panel.content)
        self.assertIn("0 – 1", panel.content)
        # The full-time alert is still sent as its own notification.
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("Full-time", self.channel.sent[0].content)

    async def test_panel_429_pauses_edits(self):
        import discord

        self._seed_watch(with_panel=True)
        panel = self.channel.messages[2001]
        resp = SimpleNamespace(status=429, reason="Too Many Requests",
                               headers={"Retry-After": "30"})
        err = discord.HTTPException(resp, "rate limited")
        err.retry_after = 30.0
        panel.edit_error = err
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        # The GOAL alert still goes out (a new message); the panel
        # edit fails with 429 and the panel is paused, not retried.
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)
        self.assertEqual(len(panel.edits), 0)
        watch = wmod._watches[(777, "771747666")]
        self.assertGreater(watch["panel_blocked_until"], time.monotonic())
        # While paused, another tick does not try to edit again.
        panel.edit_error = None
        watch["last_panel_edit"] = time.monotonic() - 2
        await wmod.on_tick(self.bot, [changed])
        self.assertEqual(len(panel.edits), 0)

    async def test_cricket_panel_shows_overs_and_break(self):
        npl = _load_scoreboard("games1x2_npl.json")
        detail = prov._onex_detail_from_raw(npl[0])
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = detail["home"], detail["away"]
        wmod._watches[(777, "771678099")] = {
            "home": detail["home"], "away": detail["away"],
            "league": detail.get("league", ""), "detail": "simple",
            "fmt": "", "snapshot": snap, "missing": 0,
            "panel_message_id": None, "panel_body": None,
            "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        await wmod.on_tick(self.bot, npl)
        self.assertEqual(len(self.channel.sent), 1)
        panel = self.channel.sent[0]
        self.assertIn("Lumbini Lions", panel.content)
        self.assertIn("157", panel.content)
        self.assertIn("76/2", panel.content)
        self.assertIn("10.5 ov", panel.content)
        # Innings break: the panel says so instead of "0.0 ov".
        broken = json.loads(json.dumps(npl[0]))
        broken["scores"]["isBreak"] = True
        watch = wmod._watches[(777, "771678099")]
        watch["last_panel_edit"] = time.monotonic() - 2
        await wmod.on_tick(self.bot, [broken])
        # The break also sends its own notification, as before.
        self.assertEqual(len(self.channel.sent), 2)
        self.assertIn("Innings break", self.channel.sent[1].content)
        self.assertEqual(len(panel.edits), 1)
        self.assertIn("Innings break", panel.edits[0])

    async def test_unfollow_keeps_panel(self):
        self._seed_watch(with_panel=True)
        view = wmod.WatchAlertView("771747666")
        interaction = FakeInteraction(
            message=SimpleNamespace(content=""),
            data={"custom_id": "panel:unfollow:771747666"})
        await view.children[7].callback(interaction)
        # The watch stays — only the alerts stop; the panel is NOT
        # deleted and keeps ticking until full-time.
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertFalse(wmod._watches[(777, "771747666")]["notify"])
        panel = self.channel.messages[2001]
        self.assertFalse(panel.deleted)
        self.assertEqual(
            interaction.followup.sent,
            ["🔕 Notifications off for **Republic of the Gambia vs Ivory Coast** — the live panel keeps ticking here while the game is live."])

    async def test_refresh_on_panel_edits_panel_in_place(self):
        view = wmod.WatchAlertView("771747666")
        detail = prov._onex_detail_from_raw(self.games1x2[0])
        panel_text = wmod.render_panel(detail, "67:00")
        interaction = FakeInteraction(
            message=SimpleNamespace(content=panel_text),
            data={"custom_id": "panel:refresh:771747666"})
        new_detail = dict(detail)
        new_detail["home_score"] = 1
        with patch.object(prov, "onex_search",
                          new=AsyncMock(return_value={"games": [],
                                                      "detail": new_detail})):
            await view.children[2].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        content, edited_view = interaction.edited[0]
        self.assertIn("1 – 1", content)
        self.assertIn("🔴 LIVE", content)
        self.assertIsInstance(edited_view, wmod.WatchAlertView)

    async def test_watch_command_reply_is_the_panel(self):
        # The user confirmed it (18 Nov 2026): when they run
        # /watch, the panel itself is what they see — one public
        # message, ticking in place — not a separate static reply.
        import bot as botmod

        detail = prov._onex_detail_from_raw(self.games1x2[0])
        resolved = {"game_id": "771747666", "action": "follow",
                    "detail": None, "candidates": [],
                    "entry": {"id": "771747666",
                              "home": "Republic of the Gambia",
                              "away": "Ivory Coast",
                              "league": "Africa Cup of Nations",
                              "status": "2H"},
                    "source": "id"}
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(prov, "onex_search",
                          new=AsyncMock(return_value={"games": [],
                                                      "detail": detail})):
            await botmod.cmd_watch.callback(interaction, "771747666")
        # Exactly one message, and it is the live panel.
        self.assertEqual(len(interaction.followup.sent), 1)
        panel = interaction.followup.sent[0]
        self.assertIn("🔴 LIVE", panel)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", panel)
        self.assertIn("Game ID: 771747666", panel)
        self.assertNotIn("Watching **", panel)
        view = interaction.followup.sent_views[0]
        self.assertIsInstance(view, wmod.WatchAlertView)
        # The panel id is recorded, so the 1s tick edits this
        # message instead of posting a new one.
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertEqual(watch["panel_body"], panel)

    async def test_watch_upcoming_game_keeps_text_reply(self):
        # A game that has not started yet has no live panel to
        # show — the text reply stands, and the panel appears on
        # the first tick once the game goes live.
        import bot as botmod

        resolved = {"game_id": "771747666", "action": "follow",
                    "detail": None, "candidates": [],
                    "entry": {"id": "771747666",
                              "home": "Republic of the Gambia",
                              "away": "Ivory Coast",
                              "league": "Africa Cup of Nations",
                              "status": "upcoming"},
                    "source": "id"}
        interaction = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(prov, "onex_search",
                          new=AsyncMock(return_value={"games": [],
                                                      "detail": None})):
            await botmod.cmd_watch.callback(interaction, "771747666")
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Watching **", text)
        self.assertNotIn("🔴 LIVE", text)
        watch = wmod._watches[(777, "771747666")]
        self.assertIsNone(watch["panel_message_id"])


class TestGamePanel(unittest.IsolatedAsyncioTestCase):
    """The /game reply is a live panel: one message, edited in
    place every second (score + MM:SS clock), no new messages for
    routine changes. Follow controls notifications only; Unfollow
    stops the alerts and the panel keeps ticking until full-time.
    Several panels in one channel take turns editing."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp6"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        self._orig_channel_last = dict(wmod._channel_last_edit)
        wmod._watches = {}
        wmod._loaded = True
        wmod._channel_last_edit = {}
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
        wmod._channel_last_edit = self._orig_channel_last
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _detail(self, idx=0):
        return prov._onex_detail_from_raw(self.games1x2[idx])

    def _reset_gaps(self):
        # The 1s per-panel gap and the channel stagger are real
        # time — reset them so a second tick in the test can edit.
        # Two seconds ago, not 0.0: that defeats the 1s gap but
        # keeps the 30s heartbeat quiet, so a second tick with no
        # change does not heartbeat-edit the panel again.
        for watch in wmod._watches.values():
            watch["last_panel_edit"] = time.monotonic() - 2.0
        wmod._channel_last_edit.clear()

    def _seed_game_panel(self, game_id="771747666", idx=0, notify=False):
        """A /game reply already registered as a live panel."""
        detail = self._detail(idx)
        body = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        msg = FakeMessage(2001, body)
        self.channel.messages[2001] = msg
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = (detail.get("home", ""),
                                        detail.get("away", ""))
        wmod._watches[(777, game_id)] = {
            "home": detail.get("home", ""), "away": detail.get("away", ""),
            "league": detail.get("league", ""), "detail": "simple",
            "fmt": str(detail.get("format") or ""), "snapshot": snap,
            "missing": 0, "notify": notify, "panel_kind": "detail",
            "panel_message_id": 2001, "panel_body": body,
            "panel_base_body": body,
            "panel_static": wmod._panel_static_from_detail(detail),
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        return msg

    def _seed_panel_for_raw(self, idx, mid, game_id):
        """A compact watch panel whose body is stale, so the next
        tick wants to edit it (used by the stagger test)."""
        detail = self._detail(idx)
        msg = FakeMessage(mid, "stale")
        self.channel.messages[mid] = msg
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = (detail.get("home", ""),
                                        detail.get("away", ""))
        wmod._watches[(777, game_id)] = {
            "home": detail.get("home", ""), "away": detail.get("away", ""),
            "league": detail.get("league", ""), "detail": "simple",
            "fmt": str(detail.get("format") or ""), "snapshot": snap,
            "missing": 0, "notify": True, "panel_kind": "compact",
            "panel_message_id": mid, "panel_body": "stale",
            "panel_base_body": "stale", "panel_static": {},
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        return msg

    def _raw_with_timer(self, seconds, idx=0):
        raw = json.loads(json.dumps(self.games1x2[idx]))
        raw["scores"]["timer"]["timeSec"] = seconds
        return raw

    def _button_interaction(self, custom_id):
        content = wmod.render_detail(self._detail(), wmod.DETAIL_SIMPLE)
        return FakeInteraction(
            message=SimpleNamespace(content=content),
            data={"custom_id": custom_id})

    async def test_game_reply_is_a_live_panel(self):
        import bot as botmod

        detail = self._detail()
        interaction = FakeInteraction()
        with patch.object(botmod, "onex_search",
                          new=AsyncMock(return_value={"games": [],
                                                      "detail": detail})):
            await botmod.cmd_game.callback(interaction, "Gambia")
        # Exactly one message — the reply IS the panel.
        self.assertEqual(len(interaction.followup.sent), 1)
        body = interaction.followup.sent[0]
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", body)
        self.assertIn("Game ID: 771747666", body)
        view = interaction.followup.sent_views[0]
        self.assertIsInstance(view, wmod.WatchAlertView)
        watch = wmod._watches[(777, "771747666")]
        self.assertFalse(watch["notify"])
        self.assertEqual(watch["panel_kind"], "detail")
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertEqual(watch["panel_body"], body)

    async def test_game_panel_edits_in_place_on_clock_change(self):
        msg = self._seed_game_panel()
        await wmod.on_tick(self.bot, [self._raw_with_timer(4080)])
        # No new message — the reply itself was edited.
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("68:00", msg.content)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast",
                      msg.content)

    async def test_follow_sends_notification_only_on_goal(self):
        msg = self._seed_game_panel(notify=False)
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("panel:follow")
        await view.children[6].callback(interaction)
        self.assertTrue(wmod._watches[(777, "771747666")]["notify"])
        self.assertIn("Now following", interaction.followup.sent[0])
        # A clock tick after Follow: the panel edits, no message.
        await wmod.on_tick(self.bot, [self._raw_with_timer(4080)])
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        # A goal: one notification AND the panel ticks.
        self._reset_gaps()
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        changed["scores"]["timer"]["timeSec"] = 4080
        await wmod.on_tick(self.bot, [changed])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)
        self.assertEqual(len(msg.edits), 2)
        self.assertIn("1 – 1", msg.content)

    async def test_unfollow_stops_notifications_but_panel_keeps_ticking(self):
        msg = self._seed_game_panel(notify=True)
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("panel:unfollow")
        await view.children[7].callback(interaction)
        watch = wmod._watches[(777, "771747666")]
        self.assertFalse(watch["notify"])
        # The panel is NOT deleted — it keeps ticking.
        self.assertEqual(watch["panel_message_id"], 2001)
        self.assertFalse(msg.deleted)
        self.assertIn("Notifications off", interaction.followup.sent[0])
        # A goal now: no notification, but the panel still ticks.
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        changed["scores"]["timer"]["timeSec"] = 4080
        await wmod.on_tick(self.bot, [changed])
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("1 – 1", msg.content)
        # And a clock tick still edits the panel in place.
        self._reset_gaps()
        later = json.loads(json.dumps(changed))
        later["scores"]["timer"]["timeSec"] = 4140
        await wmod.on_tick(self.bot, [later])
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 2)
        self.assertIn("69:00", msg.content)

    async def test_panel_stops_at_full_time(self):
        msg = self._seed_game_panel()
        raw = json.loads(json.dumps(self.games1x2[0]))
        raw["scores"]["statusLineStr"] = "Full time"
        await wmod.on_tick(self.bot, [raw])
        self.assertIn("Full-time", msg.content)
        self.assertEqual(wmod._watches, {})
        self.assertEqual(len(self.channel.sent), 0)
        # Nothing more ticks after full-time.
        await wmod.on_tick(self.bot, [raw])
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)

    async def test_deleted_game_panel_stops_quietly(self):
        self._seed_game_panel()
        del self.channel.messages[2001]
        await wmod.on_tick(self.bot, [self._raw_with_timer(4080)])
        # No re-post, no new message — the panel just stops.
        self.assertEqual(wmod._watches, {})
        self.assertEqual(len(self.channel.sent), 0)

    async def test_channel_edits_are_staggered(self):
        # Two panels ticking in one channel: one tick edits only
        # one of them; the other catches up on the next tick.
        a = self._seed_panel_for_raw(0, 2001, "771747666")
        b = self._seed_panel_for_raw(2, 2002, "771750037")
        await wmod.on_tick(self.bot, [self.games1x2[0], self.games1x2[2]])
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(a.edits) + len(b.edits), 1)
        self._reset_gaps()
        await wmod.on_tick(self.bot, [self.games1x2[0], self.games1x2[2]])
        self.assertEqual(len(a.edits), 1)
        self.assertEqual(len(b.edits), 1)
        self.assertEqual(len(self.channel.sent), 0)

    async def test_game_panel_persists_across_restart(self):
        import bot as botmod

        detail = self._detail()
        interaction = FakeInteraction()
        with patch.object(botmod, "onex_search",
                          new=AsyncMock(return_value={"games": [],
                                                      "detail": detail})):
            await botmod.cmd_game.callback(interaction, "Gambia")
        wmod._watches = {}
        wmod._loaded = False
        await wmod.load_watches()
        watch = wmod._watches[(777, "771747666")]
        self.assertFalse(watch["notify"])
        self.assertEqual(watch["panel_kind"], "detail")
        self.assertEqual(watch["panel_message_id"], 4001)

    async def test_watch_on_game_panel_switches_alerts_on_without_new_panel(self):
        import bot as botmod

        detail = self._detail()
        interaction = FakeInteraction()
        with patch.object(botmod, "onex_search",
                          new=AsyncMock(return_value={"games": [],
                                                      "detail": detail})):
            await botmod.cmd_game.callback(interaction, "Gambia")
        resolved = {"game_id": "771747666", "action": "follow",
                    "detail": None, "candidates": [],
                    "entry": {"id": "771747666",
                              "home": "Republic of the Gambia",
                              "away": "Ivory Coast",
                              "league": "Africa Cup of Nations",
                              "status": "2H"},
                    "source": "id"}
        interaction2 = FakeInteraction()
        with patch.object(wmod, "resolve_watch",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(prov, "onex_search",
                          new=AsyncMock(return_value={"games": [],
                                                      "detail": detail})):
            await botmod.cmd_watch.callback(interaction2, "771747666")
        # One ephemeral confirmation — the /game panel itself is
        # the live view, no second panel was posted.
        self.assertEqual(len(interaction2.followup.sent), 1)
        self.assertIn("Now following", interaction2.followup.sent[0])
        self.assertNotIn("🔴 LIVE", interaction2.followup.sent[0])
        watch = wmod._watches[(777, "771747666")]
        self.assertTrue(watch["notify"])
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertEqual(watch["panel_kind"], "detail")


class TestCricketPanel(unittest.IsolatedAsyncioTestCase):
    """The user's complaint, as a test: a cricket /game panel
    (Pakistan vs Sri Lanka, 4.4 ov) did NOT live-update — the
    user had to press Refresh, and Detail/Live taps sent fresh
    static snapshots. These tests pin the fix: overs/score
    changes edit the panel in place, the Updated footer proves
    it is alive, button taps switch the view in place, and a
    static card is adopted as a live panel when tapped."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp8"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        self._orig_cooldown = dict(wmod._refresh_cooldown)
        wmod._watches = {}
        wmod._loaded = True
        wmod._refresh_cooldown = {}
        wmod._channel_last_edit.clear()
        self.npl = _load_scoreboard("games1x2_npl.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        wmod._refresh_cooldown = self._orig_cooldown
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _detail(self, idx=0):
        return prov._onex_detail_from_raw(self.npl[idx])

    def _seed_cricket_panel(self, game_id="771678099", idx=0):
        """A /game reply already registered as a live panel."""
        detail = self._detail(idx)
        body = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        msg = FakeMessage(2001, body)
        self.channel.messages[2001] = msg
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = detail["home"], detail["away"]
        wmod._watches[(777, game_id)] = {
            "home": detail["home"], "away": detail["away"],
            "league": detail.get("league", ""), "detail": "simple",
            "fmt": str(detail.get("format") or ""), "snapshot": snap,
            "missing": 0, "notify": False, "panel_kind": "detail",
            "panel_message_id": 2001, "panel_body": body,
            "panel_base_body": body,
            "panel_static": wmod._panel_static_from_detail(detail),
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            # Edited 2s ago: the 1s gap passes, the 30s
            # heartbeat does not fire.
            "last_panel_edit": time.monotonic() - 2,
            "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        return msg

    def _raw_overs(self, over_str, score2=None, idx=0):
        raw = json.loads(json.dumps(self.npl[idx]))
        raw["scores"]["extendedScore"]["innsStats"]["overStr"] = over_str
        if score2 is not None:
            raw["scores"]["extendedScore"]["cricketScore"][
                "wicketScore"]["opponent2"] = score2
            raw["scores"]["scoreOpp2"] = int(score2.split("/")[0])
        return raw

    def _button_interaction(self, custom_id, content, mid=4001,
                            ephemeral=False):
        return FakeInteraction(
            message=SimpleNamespace(
                id=mid, content=content,
                flags=SimpleNamespace(ephemeral=ephemeral)),
            data={"custom_id": custom_id})

    def _mock_search(self, detail):
        return patch.object(
            prov, "onex_search",
            new=AsyncMock(return_value={"games": [], "detail": detail}))

    async def test_cricket_overs_change_edits_panel_in_place(self):
        msg = self._seed_cricket_panel()
        await wmod.on_tick(self.bot, [self._raw_overs("11.2")])
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("11.2 ov", msg.edits[0])
        self.assertIn("Lumbini Lions 157 – 76/2 Sudurpaschim Royals",
                      msg.edits[0])
        # The panel proves it is alive: an Updated footer.
        self.assertIn("-# Updated", msg.edits[0])
        self.assertIn("NPT", msg.edits[0])

    async def test_cricket_runs_only_change_edits_panel_no_new_message(self):
        # Runs with no wicket: no alert, just the panel edit.
        msg = self._seed_cricket_panel()
        await wmod.on_tick(self.bot, [self._raw_overs("10.5", "77/2")])
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("77/2", msg.edits[0])

    async def test_cricket_no_change_edits_nothing(self):
        msg = self._seed_cricket_panel()
        await wmod.on_tick(self.bot, self.npl)
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 0)

    async def test_cricket_heartbeat_edits_after_30s_idle(self):
        detail = self._detail()
        base = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        msg = FakeMessage(2001, base + "\n\n-# Updated 00:00:01 NPT")
        self.channel.messages[2001] = msg
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = detail["home"], detail["away"]
        wmod._watches[(777, "771678099")] = {
            "home": detail["home"], "away": detail["away"],
            "league": detail.get("league", ""), "detail": "simple",
            "fmt": "", "snapshot": snap, "missing": 0, "notify": False,
            "panel_kind": "detail", "panel_message_id": 2001,
            "panel_body": msg.content, "panel_base_body": base,
            "panel_static": {}, "panel_momentum": None,
            "panel_momentum_ts": 0.0,
            # Nothing changed for over 30s: the heartbeat fires.
            "last_panel_edit": time.monotonic() - 31,
            "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        await wmod.on_tick(self.bot, self.npl)
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        # The score is unchanged; only the Updated time moved.
        self.assertIn("10.5 ov", msg.edits[0])
        self.assertIn("-# Updated", msg.edits[0])
        self.assertNotIn("00:00:01", msg.edits[0])

    async def test_detail_button_adopts_static_card_and_it_ticks(self):
        # The user's exact case: a /game reply posted before the
        # panel code was never registered, so it could never tick.
        # Tapping Detail adopts it — and then it ticks.
        detail = self._detail()
        card = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        msg = FakeMessage(4001, card)
        self.channel.messages[4001] = msg
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("watch:detail", card)
        with self._mock_search(detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        self.assertEqual(interaction.followup.sent, [])
        entry = wmod._watches[(777, "771678099")]
        self.assertEqual(entry["panel_message_id"], 4001)
        self.assertEqual(entry["panel_kind"], "detail")
        # The tap itself just edited the card, so the 1s gap
        # would defer the tick's edit — move the edit back 2s,
        # the way the other tick tests do.
        entry["last_panel_edit"] = time.monotonic() - 2
        await wmod.on_tick(self.bot, [self._raw_overs("11.2")])
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("11.2 ov", msg.edits[0])
        self.assertEqual(len(self.channel.sent), 0)

    async def test_refresh_button_adopts_static_card_in_place(self):
        detail = self._detail()
        card = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        msg = FakeMessage(4001, card)
        self.channel.messages[4001] = msg
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("panel:refresh", card)
        with self._mock_search(detail):
            await view.children[2].callback(interaction)
        self.assertEqual(len(interaction.edited), 1)
        self.assertIn("10.5 ov", interaction.edited[0][0])
        self.assertIn("-# Updated", interaction.edited[0][0])
        self.assertEqual(interaction.followup.sent, [])
        self.assertIn((777, "771678099"), wmod._watches)

    async def test_live_button_on_alert_switches_real_panel(self):
        msg = self._seed_cricket_panel()
        alert = ("🏏 **WICKET** — Sudurpaschim Royals 76/2 (10.5 ov)\n"
                 "Game ID: 771678099")
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("watch:live", alert,
                                               mid=4002)
        interaction.client = self.bot
        with self._mock_search(self._detail()):
            await view.children[1].callback(interaction)
        # The real panel switched to the compact line, in place.
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("🔴 LIVE", msg.edits[0])
        self.assertIn("10.5 ov", msg.edits[0])
        self.assertNotIn("Match stats", msg.edits[0])
        self.assertEqual(interaction.followup.sent,
                         ["Live panel updated."])
        self.assertEqual(interaction.edited, [])

    async def test_ephemeral_card_is_never_adopted(self):
        # An ephemeral detail card (e.g. a /watch detail reply)
        # must not become a phantom panel the tick can never fetch.
        detail = self._detail()
        card = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("watch:detail", card,
                                               ephemeral=True)
        with self._mock_search(detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        # The one-off advanced snapshot (Target/Need — cricket
        # has no stats table, so no "Match stats" line).
        self.assertIn("Target: 158", interaction.followup.sent[0])
        self.assertIn("Need: 82 off 55 balls", interaction.followup.sent[0])
        self.assertEqual(interaction.edited, [])
        self.assertNotIn((777, "771678099"), wmod._watches)

    async def test_cricket_panel_stops_at_full_time(self):
        msg = self._seed_cricket_panel()
        changed = json.loads(json.dumps(self.npl[0]))
        changed["scores"]["statusLineStr"] = "Full time"
        await wmod.on_tick(self.bot, [changed])
        self.assertNotIn((777, "771678099"), wmod._watches)
        # The detail card shows the final score with a Full-time
        # clock line (the 🏁 header belongs to the compact panel).
        self.assertIn("Full-time", msg.content)
        self.assertIn("157", msg.content)
        # A view-only /game panel ends silently: no new message.
        self.assertEqual(len(self.channel.sent), 0)


if __name__ == "__main__":
    unittest.main()
