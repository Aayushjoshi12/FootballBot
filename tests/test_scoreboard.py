"""Scoreboard tests — render, edit rule, NPL parsing, delivery.

Pure rendering/edit-rule tests need no network. Provider tests stub
hc.get with the fixtures in tests/fixtures/scoreboard/ (modelled on
the real 1xBet games1x2 shape. Delivery tests use fake channel/message
objects — no Discord token, no network.

Run:  python3 -m pytest tests/test_scoreboard.py -q
from the footballbot directory.
"""
import asyncio
import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord

import providers as prov
import scoreboard as sb

FIX = Path(__file__).resolve().parent / "fixtures" / "scoreboard"
NEPAL = ZoneInfo("Asia/Kathmandu")
FIXED_NOW = datetime(2026, 11, 17, 22, 30, 0, tzinfo=NEPAL)


def _load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _npl_match(**over):
    m = {"home": "Lumbini Lions", "away": "Sudurpaschim Royals",
         "home_score": "157", "away_score": "76/2",
         "status": "live", "minute": None,
         "league": "Nepal Premier League", "provider": "onexbet",
         "finished": False, "clock": "2nd innings · 10.5 ov",
         "cricket": {"is_break": False, "team": 2, "over_str": "10.5",
                     "target": 158, "need": 82, "balls_left": 55}}
    m.update(over)
    return m


def _foot_match(**over):
    m = {"home": "Iceland", "away": "Luxembourg",
         "home_score": 3, "away_score": 0,
         "status": "2H", "minute": 64,
         "league": "UEFA Nations League", "provider": "onexbet",
         "finished": False, "clock": None, "cricket": None}
    m.update(over)
    return m


class TestRender(unittest.TestCase):
    def test_npl_section(self):
        text = sb.render_scoreboard([_npl_match()], [], now=FIXED_NOW)
        self.assertIn("🏏 **Nepal Premier League**", text)
        self.assertIn("Lumbini Lions 157", text)
        self.assertIn("76/2", text)
        self.assertIn("10.5 ov", text)
        self.assertIn("Target 158", text)
        self.assertIn("need 82 off 55 balls", text)

    def test_football_line(self):
        text = sb.render_scoreboard([], [_foot_match()], now=FIXED_NOW)
        self.assertIn("⚽ **Top games**", text)
        self.assertIn("Iceland 3–0 Luxembourg", text)
        self.assertIn("64'", text)
        self.assertIn("UEFA Nations League", text)

    def test_innings_break_label(self):
        m = _npl_match(home_score="157", away_score="0/0",
                       clock="Innings break",
                       cricket={"is_break": True, "team": 2, "over_str": "0.0",
                                "target": 158, "need": 158, "balls_left": 120})
        text = sb.render_scoreboard([m], [], now=FIXED_NOW)
        self.assertIn("Innings break", text)
        self.assertNotIn("0.0 ov", text)
        self.assertIn("Target 158", text)
        self.assertIn("need 158 off 120 balls", text)

    def test_half_time_football(self):
        # No minute at half-time: the status reads out instead of a clock.
        text = sb.render_scoreboard(
            [], [_foot_match(status="HT", minute=None)], now=FIXED_NOW)
        self.assertIn("Half-time", text)

    def test_empty(self):
        text = sb.render_scoreboard([], [], now=FIXED_NOW)
        self.assertIn("No live matches right now.", text)
        self.assertNotIn("Nepal Premier League", text)

    def test_footer_timestamp(self):
        text = sb.render_scoreboard([], [], now=FIXED_NOW)
        self.assertIn("Updated 22:30:00 NPT · edits only when the score changes",
                      text)

    def test_body_excludes_footer(self):
        # The edit rule compares bodies: two renders a second apart
        # must have equal bodies but different full text (footer clock).
        t1 = sb.render_scoreboard([_npl_match()], [], now=FIXED_NOW)
        t2 = sb.render_scoreboard(
            [_npl_match()], [],
            now=datetime(2026, 11, 17, 22, 30, 1, tzinfo=NEPAL))
        self.assertNotEqual(t1, t2)
        self.assertEqual(sb._split_body(t1)[0], sb._split_body(t2)[0])
        self.assertNotIn("Updated", sb._split_body(t1)[0])


class TestEditRule(unittest.TestCase):
    def test_spec_constants(self):
        # The watch/edit cadence, pinned: 1s watch, 5s min edit gap,
        # 5-min forced refresh.
        self.assertEqual(sb.WATCH_SECONDS, 1)
        self.assertEqual(sb.MIN_EDIT_GAP, 5)
        self.assertEqual(sb.FORCE_REFRESH_SECONDS, 300)

    def test_first_post(self):
        self.assertTrue(sb.should_edit(None, "body", 0, 100))

    def test_same_body_recent_no_edit(self):
        self.assertFalse(sb.should_edit("body", "body", 100, 104))

    def test_changed_body_after_gap_edits(self):
        self.assertTrue(sb.should_edit("old", "new", 100, 105))
        self.assertTrue(sb.should_edit("old", "new", 100, 106))

    def test_changed_body_inside_gap_skips(self):
        self.assertFalse(sb.should_edit("old", "new", 100, 102))
        self.assertFalse(sb.should_edit("old", "new", 100, 104))

    def test_forced_refresh_after_5_min(self):
        self.assertTrue(sb.should_edit("body", "body", 100, 400))
        self.assertFalse(sb.should_edit("body", "body", 100, 399))

    def test_rate_limit_block(self):
        self.assertFalse(
            sb.should_edit("old", "new", 100, 200, blocked_until=500))
        self.assertTrue(
            sb.should_edit("old", "new", 100, 501, blocked_until=500))


class TestNplParsing(unittest.TestCase):
    def test_games1x2_filter_and_target(self):
        data = _load("games1x2_npl.json")
        matches = prov._onex_npl_from_games1x2(data)
        self.assertEqual(len(matches), 1)  # the AFCON game is filtered out
        m = matches[0]
        self.assertEqual(m["home"], "Lumbini Lions")
        self.assertEqual(m["home_score"], "157")
        self.assertEqual(m["away_score"], "76/2")
        self.assertEqual(m["clock"], "2nd innings · 10.5 ov")
        self.assertEqual(m["league"], "Nepal Premier League")
        self.assertEqual(m["cricket"]["target"], 158)
        self.assertEqual(m["cricket"]["need"], 82)
        self.assertEqual(m["cricket"]["balls_left"], 55)

    def test_innings_break(self):
        g = _load("games1x2_npl.json")[0]
        g = json.loads(json.dumps(g))  # deep copy
        g["scores"]["isBreak"] = True
        g["scores"]["currentPeriodName"] = "Half-time"
        g["scores"]["extendedScore"] = {
            "innsStats": {"team": 2, "overStr": "0.0"},
            "cricketScore": {"wicketScore": {"opponent1": "157",
                                             "opponent2": "0/0"}},
        }
        m = prov._onex_norm_npl_match(g)
        self.assertEqual(m["clock"], "Innings break")
        self.assertTrue(m["cricket"]["is_break"])
        self.assertEqual(m["cricket"]["target"], 158)
        self.assertEqual(m["cricket"]["need"], 158)
        self.assertEqual(m["cricket"]["balls_left"], 120)

class TestNplLiveFetch(unittest.IsolatedAsyncioTestCase):
    """onex_npl_live with hc.get stubbed: 1xBet only, no other source."""

    def setUp(self):
        self._orig_get = prov.hc.get
        self.calls: list[str] = []
        self.games1x2 = _load("games1x2_npl.json")

        async def fake_get(url, headers, params=None, cache_key="",
                           provider="x", refresh_fn=None):
            if "games1x2" in url:
                self.calls.append("games1x2")
                return self.games1x2
            raise AssertionError(f"unexpected URL in test: {url}")

        prov.hc.get = fake_get

    def tearDown(self):
        prov.hc.get = self._orig_get

    async def test_onexbet_score_wins(self):
        matches = await prov.onex_npl_live()
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["provider"], "onexbet")
        self.assertEqual(matches[0]["home_score"], "157")
        self.assertEqual(self.calls, ["games1x2"])  # no other source fetched

    async def test_empty_when_onexbet_has_no_npl(self):
        # 1xBet only: no second source is fetched, the NPL section
        # simply stays empty.
        self.games1x2 = []
        matches = await prov.onex_npl_live()
        self.assertEqual(matches, [])
        self.assertEqual(self.calls, ["games1x2"])

    async def test_scoreless_onexbet_game_returned_as_is(self):
        g = json.loads(json.dumps(self.games1x2[0]))
        g["scores"] = {"scoreOpp1": None, "scoreOpp2": None}
        self.games1x2 = [g]
        matches = await prov.onex_npl_live()
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["provider"], "onexbet")
        self.assertIsNone(matches[0]["home_score"])
        self.assertEqual(self.calls, ["games1x2"])


class FakeMessage:
    def __init__(self, mid: int, content: str):
        self.id = mid
        self.content = content
        self.edits: list[str] = []
        self.edit_error = None

    async def edit(self, content=None, allowed_mentions=None):
        if self.edit_error is not None:
            err, self.edit_error = self.edit_error, None
            raise err
        self.content = content
        self.edits.append(content)


class FakeChannel:
    def __init__(self):
        self.sent: list[FakeMessage] = []
        self.messages: dict[int, FakeMessage] = {}
        self._next_id = 1000

    async def send(self, content, allowed_mentions=None):
        self._next_id += 1
        msg = FakeMessage(self._next_id, content)
        self.messages[msg.id] = msg
        self.sent.append(msg)
        return msg

    async def fetch_message(self, mid: int):
        if mid not in self.messages:
            raise discord.NotFound(Mock(status=404), "Unknown Message")
        return self.messages[mid]


class FakeBot:
    def __init__(self, channel):
        self._channel = channel
        self._ready = asyncio.Event()
        self._ready.set()

    def get_channel(self, cid):
        return self._channel

    async def fetch_channel(self, cid):
        return self._channel

    async def wait_until_ready(self):
        await self._ready.wait()

    def is_closed(self):
        return False


class TestDelivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_state_path = sb.STATE_PATH
        self._orig_state = dict(sb._state)
        self._tmp = Path(__file__).resolve().parent / ".sb-test-tmp"
        self._tmp.mkdir(exist_ok=True)
        sb.STATE_PATH = self._tmp / "scoreboard.json"
        sb._state = {}
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    def tearDown(self):
        sb.STATE_PATH = self._orig_state_path
        sb._state = self._orig_state
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _text(self, score="157"):
        return sb.render_scoreboard(
            [_npl_match(home_score=score)], [], now=FIXED_NOW)

    async def test_first_refresh_posts(self):
        self.assertTrue(await sb.refresh_channel(self.bot, 123, self._text()))
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.channel.sent[0].content, self._text())
        self.assertEqual(sb._state[123]["message_id"],
                         self.channel.sent[0].id)

    async def test_identical_text_no_edit(self):
        await sb.refresh_channel(self.bot, 123, self._text())
        self.assertFalse(await sb.refresh_channel(self.bot, 123, self._text()))
        self.assertEqual(self.channel.sent[0].edits, [])

    async def test_changed_text_edits_after_gap(self):
        await sb.refresh_channel(self.bot, 123, self._text("157"))
        # Inside the 5s gap: no edit yet.
        self.assertFalse(
            await sb.refresh_channel(self.bot, 123, self._text("158")))
        # After the gap: one edit with the latest text.
        sb._state[123]["last_edit"] -= 10
        self.assertTrue(
            await sb.refresh_channel(self.bot, 123, self._text("158")))
        self.assertEqual(self.channel.sent[0].edits, [self._text("158")])

    async def test_deleted_message_reposts(self):
        await sb.refresh_channel(self.bot, 123, self._text("157"))
        old_id = sb._state[123]["message_id"]
        del self.channel.messages[old_id]
        sb._state[123]["last_edit"] -= 10
        self.assertTrue(
            await sb.refresh_channel(self.bot, 123, self._text("158")))
        self.assertEqual(len(self.channel.sent), 2)
        self.assertNotEqual(sb._state[123]["message_id"], old_id)

    async def test_rate_limit_backs_off(self):
        await sb.refresh_channel(self.bot, 123, self._text("157"))
        sb._state[123]["last_edit"] -= 10
        err = discord.HTTPException(Mock(status=429), "rate limited")
        err.retry_after = 30
        self.channel.sent[0].edit_error = err
        self.assertFalse(
            await sb.refresh_channel(self.bot, 123, self._text("158")))
        # Blocked: no retry while the pause lasts, even with new text.
        self.assertFalse(
            await sb.refresh_channel(self.bot, 123, self._text("159")))
        self.assertGreater(sb._state[123]["blocked_until"], time.time())

    async def test_loop_posts_once_and_skips_identical(self):
        sb._state[123] = sb._new_entry()
        orig_fetch = sb._fetch_all
        orig_watch = sb.WATCH_SECONDS
        sb.WATCH_SECONDS = 0.05
        sb._fetch_all = lambda: asyncio.sleep(0, result=([], [_foot_match()]))
        try:
            task = asyncio.create_task(sb.scoreboard_loop(self.bot))
            await asyncio.sleep(0.35)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            sb._fetch_all = orig_fetch
            sb.WATCH_SECONDS = orig_watch
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.channel.sent[0].edits, [])


if __name__ == "__main__":
    unittest.main()
