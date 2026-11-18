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

import channel_edits as cemod
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
        self.deleted = False
        self.channel = None

    async def edit(self, content=None, allowed_mentions=None):
        if self.edit_error is not None:
            err, self.edit_error = self.edit_error, None
            raise err
        self.content = content
        self.edits.append(content)

    async def delete(self):
        self.deleted = True
        if self.channel is not None:
            self.channel.messages.pop(self.id, None)


class FakeChannel:
    def __init__(self):
        self.sent: list[FakeMessage] = []
        self.messages: dict[int, FakeMessage] = {}
        self._next_id = 1000
        self.send_error = None

    async def send(self, content, allowed_mentions=None):
        if self.send_error is not None:
            err, self.send_error = self.send_error, None
            raise err
        self._next_id += 1
        msg = FakeMessage(self._next_id, content)
        msg.channel = self
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
        self._orig_stopped = set(sb._stopped)
        self._tmp = Path(__file__).resolve().parent / ".sb-test-tmp"
        self._tmp.mkdir(exist_ok=True)
        sb.STATE_PATH = self._tmp / "scoreboard.json"
        sb._state = {}
        sb._stopped = set()
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    def tearDown(self):
        sb.STATE_PATH = self._orig_state_path
        sb._state = self._orig_state
        sb._stopped = self._orig_stopped
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    async def asyncTearDown(self):
        # Edits go through the shared scheduler now — its
        # per-channel worker would otherwise outlive the test's
        # event loop.
        await cemod.reset_for_tests()

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
        # The scheduler paused the WHOLE channel for 30s: a retry
        # with new text is refused at once (no 30s wait) and the
        # scoreboard tries again on a later tick.
        self.assertFalse(
            await sb.refresh_channel(self.bot, 123, self._text("159")))
        self.assertGreater(cemod.channel_blocked_until(123),
                           time.monotonic())

    async def test_loop_posts_once_and_skips_identical(self):
        sb._state[sb.TEST_CHANNEL_ID] = sb._new_entry()
        orig_fetch = sb._fetch_all
        orig_watch = sb.WATCH_SECONDS
        sb.WATCH_SECONDS = 0.05
        sb._fetch_all = lambda: asyncio.sleep(0, result=([], [_foot_match()], []))
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


GENERAL_CHANNEL_ID = 966731286884532257


class TestGuardrails(unittest.IsolatedAsyncioTestCase):
    """18 Nov 2026 incident: /scoreboard in #general reported failure
    yet a scoreboard appeared there anyway. The old code registered
    the channel before posting, and the background loop then posted
    to every registered channel on its next tick. These tests pin
    the fix: test channel only, register-after-post, boot purge,
    and a Discord-side stop."""

    def setUp(self):
        self._orig_state_path = sb.STATE_PATH
        self._orig_state = dict(sb._state)
        self._orig_stopped = set(sb._stopped)
        self._orig_fetch = sb._fetch_all
        self._orig_purged = sb._purged_disallowed
        self._tmp = Path(__file__).resolve().parent / ".sb-guard-test-tmp"
        self._tmp.mkdir(exist_ok=True)
        sb.STATE_PATH = self._tmp / "scoreboard.json"
        sb._state = {}
        sb._stopped = set()
        sb._purged_disallowed = False
        # No network: start_for_channel's fetch returns nothing live.
        sb._fetch_all = lambda: asyncio.sleep(0, result=([], [], []))
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    def tearDown(self):
        sb.STATE_PATH = self._orig_state_path
        sb._state = self._orig_state
        sb._stopped = self._orig_stopped
        sb._fetch_all = self._orig_fetch
        sb._purged_disallowed = self._orig_purged
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _saved(self):
        if not sb.STATE_PATH.exists():
            return {}
        return json.loads(sb.STATE_PATH.read_text(encoding="utf-8"))

    async def test_start_refused_in_general(self):
        self.assertFalse(
            await sb.start_for_channel(self.bot, GENERAL_CHANNEL_ID))
        self.assertNotIn(GENERAL_CHANNEL_ID, sb._state)
        self.assertEqual(self.channel.sent, [])
        self.assertNotIn(str(GENERAL_CHANNEL_ID), self._saved())

    async def test_failed_first_post_registers_nothing(self):
        # A transient transport error (not an HTTPException) during
        # the first send — the incident's most likely failure mode.
        self.channel.send_error = ConnectionError("connection reset")
        self.assertFalse(
            await sb.start_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        self.assertNotIn(sb.TEST_CHANNEL_ID, sb._state)
        self.assertEqual(self.channel.sent, [])
        self.assertNotIn(str(sb.TEST_CHANNEL_ID), self._saved())
        # A retry afterwards works and registers exactly once.
        self.assertTrue(
            await sb.start_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        self.assertIn(sb.TEST_CHANNEL_ID, sb._state)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_load_drops_general_from_saved_state(self):
        payload = {
            str(GENERAL_CHANNEL_ID): {"message_id": 555, "body": "x",
                                      "last_edit": 0, "blocked_until": 0},
            str(sb.TEST_CHANNEL_ID): {"message_id": 777, "body": "y",
                                      "last_edit": 0, "blocked_until": 0},
        }
        sb.STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")
        loaded = sb._load_state()
        self.assertNotIn(GENERAL_CHANNEL_ID, loaded)
        self.assertIn(sb.TEST_CHANNEL_ID, loaded)
        self.assertTrue(sb._purged_disallowed)
        # Persisting the loaded state cleans the file for good.
        sb._state = loaded
        sb._save_state()
        self.assertNotIn(str(GENERAL_CHANNEL_ID), self._saved())
        self.assertIn(str(sb.TEST_CHANNEL_ID), self._saved())

    async def test_ensure_running_persists_purge(self):
        payload = {str(GENERAL_CHANNEL_ID): {"message_id": 555, "body": "x",
                                             "last_edit": 0,
                                             "blocked_until": 0}}
        sb.STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")
        sb._state = sb._load_state()
        self.assertTrue(sb._purged_disallowed)
        # A live dummy task so ensure_running does not start the loop.
        dummy = asyncio.create_task(asyncio.sleep(3600))
        self.bot.scoreboard_task = dummy
        try:
            sb.ensure_running(self.bot)
            self.assertNotIn(str(GENERAL_CHANNEL_ID), self._saved())
            self.assertFalse(sb._purged_disallowed)
        finally:
            dummy.cancel()
            await asyncio.gather(dummy, return_exceptions=True)

    async def test_loop_ignores_general_even_if_registered(self):
        sb._state[GENERAL_CHANNEL_ID] = sb._new_entry()
        sb._state[sb.TEST_CHANNEL_ID] = sb._new_entry()
        orig_fetch = sb._fetch_all
        orig_watch = sb.WATCH_SECONDS
        sb.WATCH_SECONDS = 0.05
        sb._fetch_all = lambda: asyncio.sleep(
            0, result=([], [_foot_match()], []))
        try:
            task = asyncio.create_task(sb.scoreboard_loop(self.bot))
            await asyncio.sleep(0.35)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            sb._fetch_all = orig_fetch
            sb.WATCH_SECONDS = orig_watch
        # Only the test channel got a post; general was skipped.
        self.assertEqual(len(self.channel.sent), 1)

    async def test_start_when_already_running_returns_true(self):
        # 18 Nov 2026 (bug 2): /scoreboard reported "Could not post"
        # three times while the scoreboard was already live — the
        # start returned refresh_channel's False (no edit needed:
        # text unchanged, 5s gap not passed). A running scoreboard
        # is a successful start.
        body = sb._split_body(sb.render_scoreboard([], []))[0]
        msg = FakeMessage(555, "old body")
        msg.channel = self.channel
        self.channel.messages[555] = msg
        sb._state[sb.TEST_CHANNEL_ID] = {
            "message_id": 555, "body": body,
            "last_edit": time.time(), "blocked_until": 0.0}
        self.assertTrue(
            await sb.start_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        # No edit was needed, so nothing was sent.
        self.assertEqual(len(self.channel.sent), 0)

    async def test_stop_blocks_env_channel_repost(self):
        # 18 Nov 2026 (bug 3): /stopscoreboard confirmed "stopped"
        # and a new Live Scores message appeared right after —
        # the loop re-added SCOREBOARD_CHANNEL_ID (set at deploy)
        # on its next tick and refresh_channel resurrected the
        # state. With the env channel set, a stop must stick.
        orig_env = sb.SCOREBOARD_CHANNEL_ID
        sb.SCOREBOARD_CHANNEL_ID = sb.TEST_CHANNEL_ID
        orig_watch = sb.WATCH_SECONDS
        sb.WATCH_SECONDS = 0.05
        try:
            self.assertTrue(
                await sb.start_for_channel(self.bot, sb.TEST_CHANNEL_ID))
            self.assertTrue(
                await sb.stop_for_channel(self.bot, sb.TEST_CHANNEL_ID))
            sent_before = len(self.channel.sent)
            task = asyncio.create_task(sb.scoreboard_loop(self.bot))
            await asyncio.sleep(0.25)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            sb.SCOREBOARD_CHANNEL_ID = orig_env
            sb.WATCH_SECONDS = orig_watch
        self.assertEqual(len(self.channel.sent), sent_before)
        self.assertNotIn(sb.TEST_CHANNEL_ID, sb._state)

    async def test_stop_persists_across_reload(self):
        self.assertTrue(
            await sb.start_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        self.assertTrue(
            await sb.stop_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        self.assertIn(sb.TEST_CHANNEL_ID, sb._stopped)
        # Simulate a restart: reload the saved file fresh.
        sb._stopped = set()
        sb._state = sb._load_state()
        self.assertIn(sb.TEST_CHANNEL_ID, sb._stopped)
        self.assertNotIn(sb.TEST_CHANNEL_ID, sb._state)
        # And a refresh in a stopped channel does nothing.
        self.assertFalse(await sb.refresh_channel(
            self.bot, sb.TEST_CHANNEL_ID, "📊 **Live Scores**"))
        self.assertEqual(len(self.channel.sent), 1)

    async def test_start_after_stop_clears_stop(self):
        self.assertTrue(
            await sb.start_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        self.assertTrue(
            await sb.stop_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        self.assertTrue(
            await sb.start_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        self.assertNotIn(sb.TEST_CHANNEL_ID, sb._stopped)
        self.assertIn(sb.TEST_CHANNEL_ID, sb._state)
        self.assertEqual(len(self.channel.sent), 2)

    async def test_stop_removes_channel_and_deletes_message(self):
        self.assertTrue(
            await sb.start_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        mid = sb._state[sb.TEST_CHANNEL_ID]["message_id"]
        self.assertIn(mid, self.channel.messages)
        self.assertTrue(
            await sb.stop_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        self.assertNotIn(sb.TEST_CHANNEL_ID, sb._state)
        self.assertNotIn(mid, self.channel.messages)
        self.assertNotIn(str(sb.TEST_CHANNEL_ID), self._saved())
        # A second stop reports nothing to stop.
        self.assertFalse(
            await sb.stop_for_channel(self.bot, sb.TEST_CHANNEL_ID))
        # And the loop sends nothing afterwards.
        sent_before = len(self.channel.sent)
        orig_watch = sb.WATCH_SECONDS
        sb.WATCH_SECONDS = 0.05
        try:
            task = asyncio.create_task(sb.scoreboard_loop(self.bot))
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            sb.WATCH_SECONDS = orig_watch
        self.assertEqual(len(self.channel.sent), sent_before)


if __name__ == "__main__":
    unittest.main()
