"""Game search/detail, AI watch resolution, watch storage + alerts.

No Discord token, no network, no real Gemini — hc.get is stubbed
with the fixtures in tests/fixtures/watch/ (modelled on the real
1xBet games1x2 / topGamesByStat / gameInfo / leftMenuSports
shapes), and Gemini is a fake model object.

Run:  python3 -m pytest tests/test_watch.py -q
from the footballbot directory.
"""
import asyncio
import discord
import json
import logging
import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import channel_edits as cemod
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


@contextmanager
def _capture_watch_log():
    """Collect 'watch' logger records at INFO for one block.
    (pytest's caplog fixture is not injected into unittest
    TestCase methods, so the tap-logging tests use this.)"""
    records = []
    handler = logging.Handler()
    handler.setLevel(logging.INFO)
    handler.emit = lambda record: records.append(record)
    logger = logging.getLogger("watch")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


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


class TestGameInfoFailure(unittest.IsolatedAsyncioTestCase):
    """gameInfo failure plumbing (18 Nov 2026, VM journal):
    the endpoint answers HTTP 400 for every game id from the
    bot's server, while the IDENTICAL request (same params,
    headers, UA, both mirrors) succeeds from Vercel — upstream
    IP-gating, not a bad request shape. onex_game_info must
    report the failure (None) so the detail can say so, and a
    failed gameInfo must never fail the detail or empty the
    stats table (stats come from the live list, not gameInfo)."""

    async def test_http_failure_returns_none(self):
        # hc.get returns None on an HTTP error (400):
        # raise_for_status -> break -> None.
        with patch.object(prov.hc, "get",
                          new=AsyncMock(return_value=None)):
            info = await prov.onex_game_info("771747666")
        self.assertIsNone(info)

    async def test_exception_returns_none(self):
        with patch.object(prov.hc, "get",
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            info = await prov.onex_game_info("771747666")
        self.assertIsNone(info)

    async def test_empty_answer_returns_empty_dict(self):
        # Answered but no usable record for this game: {} —
        # NOT a feed failure, so info_failed stays False.
        with patch.object(prov.hc, "get",
                          new=AsyncMock(return_value={})):
            info = await prov.onex_game_info("771747666")
        self.assertEqual(info, {})

    async def test_game_detail_marks_info_failed(self):
        raw = _load("games1x2.json")[0]
        with patch.object(prov.hc, "get",
                          new=AsyncMock(return_value=None)):
            detail = await prov.onex_game_detail(raw)
        self.assertTrue(detail["info_failed"])
        # Scores still come from the live list — the detail
        # (and its stats) survive a dead gameInfo.
        self.assertEqual(detail["home_score"], 0)
        self.assertIsNotNone(detail["stats"])

    async def test_game_detail_info_ok_when_game_info_answers(self):
        raw = _load("games1x2.json")[0]
        info = _load("gameinfo_771747666.json")
        with patch.object(prov.hc, "get",
                          new=AsyncMock(return_value=info)):
            detail = await prov.onex_game_detail(raw)
        self.assertFalse(detail["info_failed"])
        self.assertEqual(detail.get("venue"), "Stade de Marrakech")


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
        await _cancel_panel_workers()
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
    def __init__(self, mid, content, view=None, bucket=None):
        self.id = mid
        self.content = content
        self.view = view
        self.edits = []
        self.edit_error = None
        self.deleted = False
        self.bucket = bucket

    async def edit(self, content=None, allowed_mentions=None, view=None):
        if self.edit_error is not None:
            raise self.edit_error
        if self.bucket is not None and not self.bucket.try_acquire():
            # Faithful to a Discord 429: _refresh_panel reads
            # err.retry_after and pauses only this panel.
            # retry_after matches the bucket: one token refills
            # in window/limit seconds, so a retry after that
            # lands instead of denying again.
            retry_after = (self.bucket.window / self.bucket.limit
                           if self.bucket is not None else 1.0)
            resp = SimpleNamespace(status=429, reason="Too Many Requests",
                                   headers={"Retry-After": str(retry_after)})
            err = discord.HTTPException(resp, "rate limited")
            err.retry_after = retry_after
            raise err
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


async def _drain_panel_workers(timeout=5.0):
    """Wait for the tick's background panel workers to finish
    their edits. The tick only schedules edits; the workers do
    them — so a test that asserts on a panel must drain first."""
    tasks = [t for t in list(wmod._panel_workers.values()) if not t.done()]
    if tasks:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout)


async def _cancel_panel_workers():
    """Cancel any leftover panel workers (test hygiene: a worker
    from one test must never edit another test's panel). Also
    resets the shared edit scheduler — its per-channel worker
    would otherwise outlive the test's event loop."""
    for task in list(wmod._panel_workers.values()):
        task.cancel()
    if wmod._panel_workers:
        await asyncio.gather(*wmod._panel_workers.values(),
                             return_exceptions=True)
    wmod._panel_workers.clear()
    await cemod.reset_for_tests()


class FakeBucket:
    """Discord's per-channel edit bucket, as discord.py models it
    (see discord/http.py Ratelimit): `limit` tokens, refilling
    one per window/limit seconds, shared by every message edit
    in the channel. The test drives its clock by hand —
    advance(1.0) per simulated second — so a 20s measurement
    runs in milliseconds. Default 5 tokens per 5s: Discord's
    standard per-route bucket, which the bot's journal (1
    edit/sec/channel, never a 429) is consistent with.
    `now_fn` (a virtual clock) refills the bucket from that
    clock on every acquire instead — used by the shared-scheduler
    measurements, where the scheduler's virtual sleeps advance
    the same clock."""
    def __init__(self, limit=5, window=5.0, now_fn=None):
        self.limit = limit
        self.window = window
        self.tokens = float(limit)
        self.now = 0.0
        self.now_fn = now_fn

    def advance(self, seconds):
        self.now += seconds
        self.tokens = min(float(self.limit),
                          self.tokens + seconds * self.limit / self.window)

    def try_acquire(self):
        if self.now_fn is not None:
            now = self.now_fn()
            if now > self.now:
                self.advance(now - self.now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


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
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0
        self.games1x2 = _load("games1x2.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
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
        # no heartbeat yet (the seed's edit is moments old, under
        # the 1s heartbeat).
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
                                  "🔔 Follow", "🔕 Unfollow",
                                  "👀 Watching: 1 · 1s"])
        self.assertEqual([b.custom_id for b in msg.view.children],
                         ["watch:detail", "watch:live", "panel:refresh",
                          "panel:timeline", "panel:simple", "panel:advanced",
                          "panel:follow", "panel:unfollow",
                          "panel:watching"])
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

    async def test_missing_game_survives_thirty_absent_ticks(self):
        # Feed truncation (games1x2 caps at 50): a game can sit off
        # the live list for minutes while still live. 30 absent
        # ticks must not end the watch or send anything.
        self._seed_watch()
        for _ in range(30):
            await wmod.on_tick(self.bot, [])
            self.assertIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(self.channel.sent), 0)

    async def test_missing_game_ends_after_long_absence(self):
        # ...but a game gone for over MISSING_END_AFTER_S is over:
        # the "no longer live" notice goes out once and the watch
        # ends, exactly as the old 10-tick rule did — just later.
        self._seed_watch()
        wmod._watches[(777, "771747666")]["missing_since"] = (
            time.monotonic() - wmod.MISSING_END_AFTER_S - 1)
        await wmod.on_tick(self.bot, [])
        self.assertNotIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("no longer live", self.channel.sent[0].content)

    async def test_failed_fetch_never_counts_as_missing(self):
        # A tick whose fetch failed arrives as None: the game is
        # absent from nothing, so the watch must not age at all —
        # not even its missing counter moves.
        self._seed_watch()
        for _ in range(30):
            await wmod.on_tick(self.bot, None)
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch.get("missing", 0), 0)
        self.assertFalse(watch.get("missing_since"))
        # ...and genuinely-absent ticks after that still leave it
        # alive while the absence is short.
        for _ in range(5):
            await wmod.on_tick(self.bot, [])
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(self.channel.sent), 0)


class FakeResponse:
    def __init__(self):
        self.sent: list[str] = []
        self.defer_calls: list[tuple] = []

    async def defer(self, ephemeral=False, thinking=False, **kwargs):
        self.defer_calls.append((ephemeral, thinking))
        return None

    async def send_message(self, content=None, ephemeral=False,
                           view=None, **kwargs):
        self.sent.append(content)


class FakeFollowup:
    def __init__(self):
        self.sent: list[str] = []
        self.sent_views: list = []
        # Whether each send was ephemeral — the new-message
        # button behaviour sends VISIBLE messages, so tests
        # must be able to tell the two apart.
        self.ephemeral_flags: list[bool] = []
        self._next_id = 4000

    async def send(self, content=None, ephemeral=False, view=None, **kwargs):
        self.sent.append(content)
        self.sent_views.append(view)
        self.ephemeral_flags.append(bool(ephemeral))
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


class TestRenderAnalytics(unittest.TestCase):
    """The Advanced analytics view: the old markdown pipe table
    rendered as raw text in Discord, and the first bar layout
    (one long line per stat) wrapped mid-row on narrow screens.
    The stats now render as TWO short lines per stat in a
    monospace block — label, then home value + bar + away value
    — with the teams named in a header row INSIDE the block
    (home left, away right) as well as in the Home/Away line
    above it. Each sport gets its own stat order from the stats
    its feed actually carries; cricket has no stats table in the
    feed, so it gets Score / Wickets / Overs / Run rate from the
    cricket score data."""

    def setUp(self):
        g = _load("games1x2.json")[0]
        self.detail = prov._onex_detail_from_raw(g)

    def _detail_for(self, sport, stats):
        """A bare detail dict for one sport, stats in the order
        given — no fixture needed, the order is the test."""
        return {
            "home": "Home Team", "away": "Away Team",
            "home_score": "0", "away_score": "0",
            "league": "Test League", "clock": "12:00",
            "sport": sport, "stats": stats, "cricket": {},
            "onex_id": "1",
        }

    def _block_lines(self, text):
        block = text.split("```")[1]
        return [r for r in block.strip().split("\n") if r.strip()]

    def _stat_labels(self, text):
        """The label lines of the block, in order (every other
        line after the team header row)."""
        lines = self._block_lines(text)
        return lines[1::2]

    def test_keeps_every_stat_row_and_momentum(self):
        momentum = ("Momentum (estimate, last ~3 min): away side +7 "
                    "attacks/shots vs the other side")
        text = wmod.render_analytics(self.detail, momentum,
                                     updated="16:17:34")
        for name in ("Possession", "Shots on target",
                     "Corners", "Yellow cards", "Red card", "Attacks",
                     "xG", "Saves", "Substitutions", "Penalty"):
            self.assertIn(name, text)
        # Possession % values carry the % the stat name declares.
        self.assertIn("40%", text)
        self.assertIn("60%", text)
        # xG decimals are kept exactly as the feed sent them.
        self.assertIn("0.15", text)
        self.assertIn("0.71", text)
        self.assertIn("Momentum (estimate", text)
        self.assertIn("Game ID: 771747666", text)
        self.assertIn("-# Updated 16:17:34 NPT", text)

    def test_no_markdown_pipe_table(self):
        text = wmod.render_analytics(self.detail)
        self.assertNotIn("| Statistic |", text)
        self.assertNotIn("|---|", text)
        self.assertNotIn("| Possession", text)

    def test_stat_block_is_two_lines_per_stat(self):
        text = wmod.render_analytics(self.detail)
        self.assertIn("```", text)
        self.assertIn("█", text)
        self.assertIn("░", text)
        # Possession 40/60: 4 of 10 blocks filled.
        self.assertIn("████░░░░░░", text)
        # A team header row inside the block, then two lines per
        # stat: a label line, then a value line carrying home
        # value, the 10-block bar and away value together — the
        # one-long-line layout wrapped mid-row on narrow screens
        # and split the numbers from their bar.
        lines = self._block_lines(text)
        self.assertEqual(len(lines), 21)  # header + 10 stats x 2 lines
        header = lines[0]
        self.assertNotIn("█", header)
        self.assertNotIn("░", header)
        for i in range(1, len(lines), 2):
            label, values = lines[i], lines[i + 1]
            self.assertNotIn("█", label)
            self.assertNotIn("░", label)
            self.assertRegex(values, r"[█░]{10}")
        # The Possession value line holds home, bar and away
        # on the SAME line.
        idx = lines.index("Possession")
        self.assertIn("40%", lines[idx + 1])
        self.assertIn("████░░░░░░", lines[idx + 1])
        self.assertIn("60%", lines[idx + 1])

    def test_team_names_inside_the_block(self):
        # The columns are labelled with the actual teams right
        # where the numbers are — the reader never has to look
        # outside the box. Home on the left, away on the right.
        text = wmod.render_analytics(self.detail)
        lines = self._block_lines(text)
        header = lines[0]
        self.assertLessEqual(len(header), 32, header)
        self.assertTrue(header.startswith("Republic of th…"),
                        header)
        self.assertTrue(header.rstrip().endswith("Ivory Coast"),
                        header)
        # Long names are shortened with … so the row stays short.
        self.assertIn("…", header)

    def test_basketball_order_and_percent(self):
        # Basketball gets its own stats, in its own order —
        # shooting first — even when the feed sends them scrambled.
        detail = self._detail_for("Basketball", [
            {"name": "Fouls", "home": "12", "away": "11"},
            {"name": "Free throws scored %", "home": "100", "away": "69"},
            {"name": "3-pointers", "home": "5", "away": "4"},
            {"name": "2-pointers", "home": "12", "away": "9"},
            {"name": "Timeouts remaining", "home": "3", "away": "3"},
            {"name": "Free throws", "home": "8", "away": "9"},
        ])
        text = wmod.render_analytics(detail)
        self.assertEqual(
            self._stat_labels(text),
            ["2-pointers", "3-pointers", "Free throws",
             "Free throws scored", "Fouls", "Timeouts remaining"])
        self.assertIn("100%", text)
        self.assertIn("69%", text)
        self.assertNotIn("Possession", text)

    def test_tennis_order_and_mid_name_percent(self):
        # Tennis serve/break stats in tennis order; a % in the
        # middle of a stat name ("Win % 1st Serve") still shows
        # its % on the values.
        detail = self._detail_for("Tennis", [
            {"name": "Breaks", "home": "0", "away": "0"},
            {"name": "Win % 2nd Serve", "home": "50", "away": "67"},
            {"name": "Aces", "home": "1", "away": "3"},
            {"name": "1st Serve %", "home": "64", "away": "62"},
            {"name": "Win % 1st Serve", "home": "86", "away": "80"},
            {"name": "Double faults", "home": "1", "away": "0"},
            {"name": "Break point %", "home": "0", "away": "0"},
            {"name": "Points on own serve", "home": "8", "away": "6"},
        ])
        text = wmod.render_analytics(detail)
        self.assertEqual(
            self._stat_labels(text),
            ["Aces", "Double faults", "1st Serve",
             "Win % 1st Serve", "Win % 2nd Serve", "Breaks",
             "Break point", "Points on own serve"])
        self.assertIn("86%", text)
        self.assertIn("80%", text)
        self.assertNotIn("Possession", text)

    def test_volleyball_and_ice_hockey_orders(self):
        detail = self._detail_for("Volleyball", [
            {"name": "Timeouts", "home": "2", "away": "2"},
            {"name": "Points on opponent's serve", "home": "23",
             "away": "23"},
            {"name": "Max pts in a row", "home": "3", "away": "3"},
            {"name": "Points on own serve", "home": "13", "away": "11"},
        ])
        self.assertEqual(
            self._stat_labels(wmod.render_analytics(detail)),
            ["Points on own serve", "Points on opponent's serve",
             "Max pts in a row", "Timeouts"])
        detail = self._detail_for("Ice Hockey", [
            {"name": "Penalties", "home": "3", "away": "1"},
            {"name": "Possession %", "home": "47", "away": "53"},
            {"name": "Attacks", "home": "80", "away": "81"},
            {"name": "2-min penalties", "home": "3", "away": "1"},
            {"name": "Goals during powerplay", "home": "1",
             "away": "0"},
        ])
        self.assertEqual(
            self._stat_labels(wmod.render_analytics(detail)),
            ["Possession", "Attacks", "Goals during powerplay",
             "Penalties", "2-min penalties"])

    def test_unknown_sport_keeps_feed_order(self):
        # A sport with no preferred order renders its stats in
        # the order the feed sent them — never invented, never
        # re-sorted into a football shape.
        detail = self._detail_for("Handball", [
            {"name": "7m throws", "home": "4", "away": "2"},
            {"name": "Fast breaks", "home": "6", "away": "5"},
        ])
        self.assertEqual(
            self._stat_labels(wmod.render_analytics(detail)),
            ["7m throws", "Fast breaks"])

    def test_cricket_shows_cricket_rows_not_football(self):
        # Real live cricket (Australia vs England): no tabloStats
        # in the feed, so the block shows Score, Wickets and
        # Overs from the cricket score data — and no football
        # rows at all.
        g = _load("games1x2_cricket.json")[0]
        detail = prov._onex_detail_from_raw(g)
        text = wmod.render_analytics(detail, updated="16:38:00")
        self.assertEqual(self._stat_labels(text),
                         ["Score", "Wickets", "Overs"])
        self.assertIn("222/9", text)
        self.assertIn("328/10", text)
        self.assertIn("45.0 ov", text)
        for name in ("Possession", "Shots on target", "Corners",
                     "Attacks", "Yellow cards"):
            self.assertNotIn(name, text)
        # The teams are named inside the block too.
        header = self._block_lines(text)[0]
        self.assertIn("Australia", header)
        self.assertIn("England", header)
        self.assertIn("Game ID: 771890070", text)
        self.assertIn("-# Updated 16:38:00 NPT", text)

    def test_cricket_momentum_never_shown(self):
        # The momentum estimate comes from football stat
        # snapshots — it never belongs on a cricket game.
        g = _load("games1x2_cricket.json")[0]
        detail = prov._onex_detail_from_raw(g)
        text = wmod.render_analytics(
            detail, "Momentum (estimate, last ~3 min): home side +5")
        self.assertNotIn("Momentum", text)

    def test_cricket_no_data_honest_line(self):
        # A cricket game with no score data yet gets the honest
        # line, not an empty box.
        detail = self._detail_for("Cricket", [])
        detail["cricket"] = {}
        detail["home_score"] = None
        detail["away_score"] = None
        text = wmod.render_analytics(detail)
        self.assertIn("No match statistics available yet.", text)
        self.assertNotIn("```", text)

    def test_no_line_in_the_block_can_wrap(self):
        # Narrow phone screens wrap long lines mid-row; every
        # line in the stat block stays short enough to fit.
        text = wmod.render_analytics(self.detail)
        block = text.split("```")[1]
        for line in block.strip().split("\n"):
            self.assertLessEqual(len(line), 32, line)

    def test_header_names_home_and_away_teams(self):
        # The two numbers on a value line are home then away —
        # the header says which team is which, once, above the
        # block.
        text = wmod.render_analytics(self.detail)
        home, away = self.detail["home"], self.detail["away"]
        self.assertIn(f"Home: {home}", text)
        self.assertIn(f"Away: {away}", text)

    def test_non_numeric_stat_renders_without_bar(self):
        detail = dict(self.detail)
        detail["stats"] = [
            {"name": "Possession %", "home": "53", "away": "47"},
            {"name": "Man of the match", "home": "—", "away": "—"}]
        text = wmod.render_analytics(detail)
        self.assertIn("Man of the match", text)
        block = text.split("```")[1]
        lines = [r for r in block.strip().split("\n") if r.strip()]
        idx = lines.index("Man of the match")
        self.assertNotIn("█", lines[idx + 1])
        self.assertNotIn("░", lines[idx + 1])
        # The numeric row still has its bar.
        self.assertIn("█████░░░░░", text)

    def test_no_stats_message_unchanged(self):
        detail = dict(self.detail)
        detail["stats"] = []
        text = wmod.render_analytics(detail)
        self.assertIn("No match statistics available yet.", text)
        self.assertIn("Game ID: 771747666", text)


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
                          "panel:follow", "panel:unfollow",
                          "panel:watching"])

    async def test_detail_button_resolves_game_from_message(self):
        # Static custom_id: the game comes from the message's
        # "Game ID:" line, and the fetch is for that id. The tap
        # sends a NEW visible message — it never edits the card
        # in place and never adopts it as a panel.
        view = self._view()
        interaction = self._interaction("watch:detail")
        mock_search = AsyncMock(
            return_value={"games": [], "detail": self.detail})
        with patch.object(prov, "onex_search", mock_search):
            await view.children[0].callback(interaction)
        mock_search.assert_awaited_once_with("771747666")
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("Match stats", text)
        self.assertIn("Possession", text)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", text)
        self.assertEqual(interaction.edited, [])
        self.assertNotIn((777, "771747666"), wmod._watches)

    async def test_detail_button_sends_new_message(self):
        view = self._view()
        interaction = self._interaction("watch:detail")
        with self._mock_search(self.detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("Match stats", text)
        self.assertIn("Possession", text)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", text)
        self.assertEqual(interaction.edited, [])
        self.assertNotIn((777, "771747666"), wmod._watches)

    async def test_live_button_sends_new_live_line(self):
        view = self._view()
        interaction = self._interaction("watch:live")
        with self._mock_search(self.detail):
            await view.children[1].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("0 – 1", text)
        self.assertIn("67:00", text)
        self.assertNotIn("Match stats", text)
        self.assertEqual(interaction.edited, [])

    async def test_live_button_cricket_sends_new_live_line(self):
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
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("16.4 ov", text)
        self.assertIn("108/4", text)
        self.assertEqual(interaction.edited, [])

    async def test_gone_game_says_no_longer_live(self):
        view = self._view()
        for child in view.children:
            # Unfollow never fetches the game — it only flips the
            # notification flag, and with no watch here it says so.
            # Watching never fetches a game either — it shows the
            # channel's watching list, not this game's detail.
            if child.custom_id in ("panel:unfollow", "panel:watching"):
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
        await _cancel_panel_workers()
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
                          "🔕 Unfollow", "👀 Watching: 1 · 1s"])
        self.assertEqual(self._ids(view),
                         ["watch:detail", "watch:live", "panel:refresh",
                          "panel:timeline", "panel:simple", "panel:advanced",
                          "panel:follow", "panel:unfollow",
                          "panel:watching"])

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
                          "🔕 Unfollow", "👀 Watching: 0"])
        self.assertEqual(self._ids(view),
                         ["watch:detail", "watch:live", "panel:refresh",
                          "panel:timeline", "panel:simple", "panel:advanced",
                          "panel:follow", "panel:unfollow",
                          "panel:watching"])


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
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
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
        await _drain_panel_workers()
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
        await _drain_panel_workers()
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
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0
        self.games1x2 = _load("games1x2.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
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
        # Gone for over MISSING_END_AFTER_S, not merely a few ticks:
        # a short absence (feed truncation) must not end the watch.
        wmod._watches[(777, "771747666")]["missing_since"] = (
            time.monotonic() - wmod.MISSING_END_AFTER_S - 1)
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
        await _cancel_panel_workers()
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

    async def test_refresh_sends_new_message(self):
        view = self._view()
        interaction = self._interaction("panel:refresh")
        new_detail = dict(self.detail)
        new_detail["home_score"] = 1
        with self._mock_search(new_detail):
            await view.children[2].callback(interaction)
        # Refresh sends a NEW visible message — it never edits
        # the card in place.
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        content = interaction.followup.sent[0]
        self.assertIn("1 – 1", content)
        self.assertIn("Game ID: 771747666", content)
        self.assertIsInstance(interaction.followup.sent_views[0],
                              wmod.WatchAlertView)
        self.assertEqual(interaction.edited, [])

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

    async def test_detail_button_on_alert_sends_new_message(self):
        # The old in-place design switched the real panel and
        # sent NOTHING — the panel changing was the feedback.
        # That is the design the user said does not work
        # properly (a queued tick edit could overwrite the
        # tap's switch): a tap on an alert now sends a NEW
        # visible message with the detail view. The live panel
        # is never EDITED by the tap — same message, same body
        # — but its stored view DOES switch to detail (18 Nov
        # 2026: "Advanced stops the refresh" — the panel kept
        # its old view while the snapshot froze, so the tap
        # must switch the panel's view and re-assert its
        # worker, or the user stares at a frozen card).
        channel = FakeChannel()
        bot = FakeBot(channel)
        detail = self.detail
        body = wmod.render_panel(detail, "67:00")
        msg = FakeMessage(2001, body)
        channel.messages[2001] = msg
        self._seed_watch()
        watch = wmod._watches[(777, "771747666")]
        watch["panel_message_id"] = 2001
        watch["panel_body"] = body
        watch["panel_base_body"] = body
        watch["panel_kind"] = "compact"
        alert = ("⚽ **GOAL** — Republic of the Gambia 1–1 Ivory Coast (67')\n"
                 "Game ID: 771747666")
        view = self._view()
        interaction = self._interaction("watch:detail", content=alert)
        interaction.client = bot
        with self._mock_search(detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Match stats", interaction.followup.sent[0])
        self.assertEqual(interaction.edited, [])
        self.assertEqual(len(msg.edits), 0)
        self.assertEqual(msg.content, body)
        # The tap never edits the old panel message — but the
        # panel MOVES to the new message (18 Nov 2026: the
        # tap's own message was a dead copy while the tick kept
        # editing the old panel elsewhere) and switches view.
        self.assertEqual(watch["panel_kind"], "detail")
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertEqual(watch["panel_body"],
                         interaction.followup.sent[0])

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
        # Timeline is a NEW visible channel message now, not
        # an ephemeral one-off.
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
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
        self.assertEqual(interaction.followup.ephemeral_flags, [True])

    async def test_simple_button_sends_new_simple_message(self):
        view = self._view()
        interaction = self._interaction("panel:simple")
        with self._mock_search(self.detail):
            await view.children[4].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertNotIn("Match stats", interaction.followup.sent[0])
        self.assertEqual(interaction.edited, [])

    async def test_advanced_button_sends_new_analytics_message(self):
        view = self._view()
        interaction = self._interaction("panel:advanced")
        with self._mock_search(self.detail):
            await view.children[5].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("Match statistics", text)
        self.assertIn("Possession", text)
        self.assertEqual(interaction.edited, [])

    async def test_follow_starts_watch(self):
        view = self._view()
        interaction = self._interaction("panel:follow")
        with self._mock_search(self.detail):
            await view.children[6].callback(interaction)
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Watching", interaction.followup.sent[0])

    async def test_follow_already_watched(self):
        self._seed_watch()
        view = self._view()
        interaction = self._interaction("panel:follow")
        with self._mock_search(self.detail):
            await view.children[6].callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["That game is already watched in this channel."])
        self.assertEqual(interaction.followup.ephemeral_flags, [False])

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
        self.assertEqual(interaction.followup.ephemeral_flags, [False])

    async def test_unfollow_not_watched(self):
        view = self._view()
        interaction = self._interaction("panel:unfollow")
        with self._mock_search(self.detail):
            await view.children[7].callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["That game is not watched in this channel."])
        self.assertEqual(interaction.followup.ephemeral_flags, [False])


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
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0
        self.games1x2 = _load("games1x2.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
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
        await _drain_panel_workers()
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
                                  "🔔 Follow", "🔕 Unfollow",
                                  "👀 Watching: 1 · 1s"])
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_message_id"], panel.id)
        self.assertEqual(watch["panel_body"], panel.content)

    async def test_clock_only_change_edits_panel_no_new_message(self):
        self._seed_watch(with_panel=False)
        await wmod.on_tick(self.bot, self.games1x2)
        await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 1)
        panel = self.channel.sent[0]
        self.assertIn("67:00", panel.content)
        watch = wmod._watches[(777, "771747666")]
        # Five seconds pass with no score change: only the clock moves.
        watch["panel_timer_ts"] = time.monotonic() - 5
        watch["last_panel_edit"] = time.monotonic() - 2
        await wmod.on_tick(self.bot, self.games1x2)
        await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(len(panel.edits), 1)
        # The clock advanced ~5s — plus up to ~1s of channel-slot
        # wait before the render — so it reads 67:05 or 67:06.
        # (With the old 0.35s slot it always landed on 67:05.)
        self.assertTrue(any(c in panel.edits[0]
                            for c in ("67:05", "67:06")),
                        panel.edits[0])

    async def test_goal_sends_alert_and_edits_panel(self):
        self._seed_watch(with_panel=True)
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        await _drain_panel_workers()
        # One new message: the GOAL alert. The panel edits in place.
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)
        panel = self.channel.messages[2001]
        self.assertEqual(len(panel.edits), 1)
        self.assertIn("1 – 1", panel.edits[0])
        self.assertIn("67:00", panel.edits[0])

    async def test_watch_survives_truncated_feed_and_panel_resumes(self):
        # The Eliminator case (18 Nov): the game fell off the
        # 50-entry live list for a stretch while still live. 30
        # absent ticks hold the SAME panel — nothing is sent, the
        # panel is not touched — and when the game reappears the
        # panel ticks on in place, no /game needed.
        self._seed_watch(with_panel=True)
        panel = self.channel.messages[2001]
        for _ in range(30):
            await wmod.on_tick(self.bot, [])
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(panel.edits), 0)
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        await _drain_panel_workers()
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(panel.edits), 1)
        self.assertIn("1 – 1", panel.edits[0])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)

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
        await _drain_panel_workers()
        # The GOAL alert still goes out (a new message); the panel
        # edit fails with 429 and the WHOLE channel pauses — the
        # shared scheduler records the pause, not the panel.
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)
        self.assertEqual(len(panel.edits), 0)
        self.assertGreater(cemod.channel_blocked_until(777),
                           time.monotonic())
        # While paused, another tick does not try to edit again —
        # the scheduler refuses at once instead of queueing a job
        # that would land stale.
        panel.edit_error = None
        watch = wmod._watches[(777, "771747666")]
        watch["last_panel_edit"] = time.monotonic() - 2
        await wmod.on_tick(self.bot, [changed])
        await _drain_panel_workers()
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
        await _drain_panel_workers()
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
        await _drain_panel_workers()
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

    async def test_refresh_on_panel_sends_new_message(self):
        # Refresh on a panel used to edit the panel in place —
        # under the new-message design it sends a NEW visible
        # message with the fresh score and leaves the panel
        # alone (no watch is registered for this message here,
        # so Refresh renders a simple detail snapshot).
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
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        content = interaction.followup.sent[0]
        self.assertIn("1 – 1", content)
        self.assertIn("Game ID: 771747666", content)
        self.assertIsInstance(interaction.followup.sent_views[0],
                              wmod.WatchAlertView)
        self.assertEqual(interaction.edited, [])

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

    async def test_analytics_panel_ticks_in_place_with_new_layout(self):
        # An analytics panel edits in place on a stat change and
        # keeps the bar layout, the Game ID line and the footer.
        detail = prov._onex_detail_from_raw(self.games1x2[0])
        body = wmod.render_analytics(detail, updated="16:17:34")
        self.channel.messages[2001] = FakeMessage(2001, body)
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = detail["home"], detail["away"]
        wmod._watches[(777, "771747666")] = {
            "home": detail["home"], "away": detail["away"],
            "league": detail.get("league", ""), "detail": "advanced",
            "fmt": "", "snapshot": snap, "missing": 0, "notify": False,
            "panel_kind": "analytics", "panel_message_id": 2001,
            "panel_body": body,
            "panel_base_body": wmod._strip_updated_footer(body),
            "panel_static": wmod._panel_static_from_detail(detail),
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            "last_panel_edit": time.monotonic() - 2,
            "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        changed = json.loads(json.dumps(self.games1x2[0]))
        # Possession flips 40/60 -> 55/45.
        changed["scores"]["tabloStats"]["0"][4]["s1"] = "55"
        changed["scores"]["tabloStats"]["0"][4]["s2"] = "45"
        await wmod.on_tick(self.bot, [changed])
        await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        panel = self.channel.messages[2001]
        self.assertEqual(len(panel.edits), 1)
        self.assertIn("```", panel.edits[0])
        self.assertIn("55%", panel.edits[0])
        self.assertIn("Game ID: 771747666", panel.edits[0])
        self.assertIn("-# Updated", panel.edits[0])

    async def test_post_panel_now_posts_panel_in_other_channel(self):
        # /watch into a different channel: post_panel_now posts
        # the live panel there. (It used to call _refresh_panel
        # with the arguments in the wrong order, so the panel was
        # never posted and the call silently returned False.)
        self._seed_watch(with_panel=False)
        detail = prov._onex_detail_from_raw(self.games1x2[0])
        posted = await wmod.post_panel_now(self.bot, 777, "771747666",
                                           detail)
        self.assertTrue(posted)
        self.assertEqual(len(self.channel.sent), 1)
        panel = self.channel.sent[0]
        self.assertIn("🔴 LIVE", panel.content)
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast",
                      panel.content)
        self.assertIn("Game ID: 771747666", panel.content)
        self.assertEqual(
            wmod._watches[(777, "771747666")]["panel_message_id"],
            panel.id)

    async def test_post_panel_now_false_without_watch(self):
        detail = prov._onex_detail_from_raw(self.games1x2[0])
        self.assertFalse(await wmod.post_panel_now(
            self.bot, 777, "771747666", detail))
        self.assertEqual(len(self.channel.sent), 0)


class TestGamePanel(unittest.IsolatedAsyncioTestCase):
    """The /game reply is a live panel: one message, edited in
    place every second (score + MM:SS clock), no new messages for
    routine changes. Follow controls notifications only; Unfollow
    stops the alerts and the panel keeps ticking until full-time.
    Several panels in one channel share Discord's ~1 edit/sec
    budget evenly: 1 game every 1s, 2 every 2s each, and so on."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp6"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0
        self.games1x2 = _load("games1x2.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _detail(self, idx=0):
        return prov._onex_detail_from_raw(self.games1x2[idx])

    def _reset_gaps(self):
        # The 1s per-panel gap and the channel stagger are real
        # time — reset them so a second tick in the test can edit.
        # Two seconds ago, not 0.0: that defeats the 1s gap. Note
        # the 1s heartbeat now fires on a no-change tick, so this
        # helper is only used before ticks that change something
        # (or the stagger test, where the two-phase tick keeps a
        # heartbeat from taking the slot ahead of a score change).
        for watch in wmod._watches.values():
            watch["last_panel_edit"] = time.monotonic() - 2.0
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0

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
        await _drain_panel_workers()
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
        await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        # A goal: one notification AND the panel ticks.
        self._reset_gaps()
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        changed["scores"]["timer"]["timeSec"] = 4080
        await wmod.on_tick(self.bot, [changed])
        await _drain_panel_workers()
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
        await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("1 – 1", msg.content)
        # And a clock tick still edits the panel in place.
        self._reset_gaps()
        later = json.loads(json.dumps(changed))
        later["scores"]["timer"]["timeSec"] = 4140
        await wmod.on_tick(self.bot, [later])
        await _drain_panel_workers()
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
        await _drain_panel_workers()
        # No re-post, no new message — the panel just stops.
        self.assertEqual(wmod._watches, {})
        self.assertEqual(len(self.channel.sent), 0)

    async def test_channel_edits_are_staggered(self):
        # Two panels ticking in one channel: one tick edits BOTH
        # of them — but spaced 1s apart (one channel edit slot
        # per second), never in the same instant. (The old
        # take-turns design edited only one per tick, so each
        # panel refreshed every ~2s with two panels and ~4s with
        # four.)
        a = self._seed_panel_for_raw(0, 2001, "771747666")
        b = self._seed_panel_for_raw(2, 2002, "771750037")
        stamps = []
        orig_a, orig_b = a.edit, b.edit

        async def _edit_a(*args, **kwargs):
            stamps.append((2001, time.monotonic()))
            return await orig_a(*args, **kwargs)

        async def _edit_b(*args, **kwargs):
            stamps.append((2002, time.monotonic()))
            return await orig_b(*args, **kwargs)

        a.edit = _edit_a
        b.edit = _edit_b
        await wmod.on_tick(self.bot, [self.games1x2[0], self.games1x2[2]])
        await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(a.edits), 1)
        self.assertEqual(len(b.edits), 1)
        self.assertEqual(len(stamps), 2)
        # Spaced, not simultaneous: the second edit lands about
        # 1s after the first (one channel edit slot per second).
        self.assertGreaterEqual(stamps[1][1] - stamps[0][1], 0.30)
        self.assertLess(stamps[1][1] - stamps[0][1], 2.0)

    async def test_live_panel_edits_every_second_via_footer(self):
        # The 1s heartbeat: a live football panel whose score and
        # clock have not moved still edits once a second, on its
        # Updated footer alone — no new message.
        msg = self._seed_game_panel()
        clock = {"n": 0}

        def _ticking_time():
            clock["n"] += 1
            return f"12:00:0{clock['n']}"

        with patch.object(wmod, "now_npt_str",
                          side_effect=_ticking_time):
            await wmod.on_tick(self.bot, self.games1x2)
            await _drain_panel_workers()
            self.assertEqual(len(self.channel.sent), 0)
            self.assertEqual(len(msg.edits), 1)
            self.assertIn("-# Updated 12:00:01 NPT", msg.edits[0])
            # A second later: the footer advances again.
            self._reset_gaps()
            await wmod.on_tick(self.bot, self.games1x2)
            await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 2)
        self.assertIn("-# Updated 12:00:02 NPT", msg.edits[1])

    async def test_content_change_is_not_starved_by_heartbeats(self):
        # Two panels in one channel: one idle (heartbeat only),
        # one whose clock has genuinely moved (a real content
        # change, but no goal — so no alert is sent). The content
        # change lands FIRST, and the idle panel's heartbeat lands
        # on the SAME tick, right after it (one channel edit slot
        # per second) instead of waiting for the next tick.
        idle = self._seed_game_panel()
        stale = self._seed_panel_for_raw(4, 2002, "771747706")
        stamps = []
        orig_idle, orig_stale = idle.edit, stale.edit

        async def _edit_idle(*args, **kwargs):
            stamps.append((2001, time.monotonic()))
            return await orig_idle(*args, **kwargs)

        async def _edit_stale(*args, **kwargs):
            stamps.append((2002, time.monotonic()))
            return await orig_stale(*args, **kwargs)

        idle.edit = _edit_idle
        stale.edit = _edit_stale
        moved = json.loads(json.dumps(self.games1x2[4]))
        moved["scores"]["timer"]["timeSec"] += 60
        await wmod.on_tick(self.bot, [self.games1x2[0], moved])
        await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(stale.edits), 1)
        self.assertEqual(len(idle.edits), 1)
        self.assertIn("-# Updated", idle.edits[0])
        # The content change edited before the heartbeat.
        self.assertEqual(stamps[0][0], 2002)
        self.assertEqual(stamps[1][0], 2001)
        self.assertGreaterEqual(stamps[1][1] - stamps[0][1], 0.30)
        self.assertIn("-# Updated", idle.edits[0])
        self.assertEqual(len(stale.edits), 1)

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
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0
        self.npl = _load_scoreboard("games1x2_npl.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
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
            # Edited 2s ago: the 1s gap passes, so a no-change
            # tick heartbeat-edits the footer (1s heartbeat).
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
        await _drain_panel_workers()
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
        await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("77/2", msg.edits[0])

    async def test_cricket_no_change_heartbeat_edits_every_second(self):
        # The 1s heartbeat: a cricket panel whose score has not
        # moved still edits once a second, on its Updated footer
        # alone — no new message, score unchanged.
        msg = self._seed_cricket_panel()
        clock = {"n": 0}

        def _ticking_time():
            clock["n"] += 1
            return f"12:00:0{clock['n']}"

        with patch.object(wmod, "now_npt_str",
                          side_effect=_ticking_time):
            await wmod.on_tick(self.bot, self.npl)
            await _drain_panel_workers()
            self.assertEqual(len(self.channel.sent), 0)
            self.assertEqual(len(msg.edits), 1)
            self.assertIn("10.5 ov", msg.edits[0])
            self.assertIn("-# Updated 12:00:01 NPT", msg.edits[0])
            # A second later: the footer advances again.
            wmod._watches[(777, "771678099")]["last_panel_edit"] = \
                time.monotonic() - 2
            wmod._panel_log_window_start = 0.0
            wmod._panel_log_edits = 0
            await wmod.on_tick(self.bot, self.npl)
            await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 2)
        self.assertIn("10.5 ov", msg.edits[1])
        self.assertIn("-# Updated 12:00:02 NPT", msg.edits[1])

    async def test_cricket_heartbeat_edits_after_1s_idle(self):
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
            # Nothing changed for over 1s: the heartbeat fires.
            "last_panel_edit": time.monotonic() - 2,
            "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        with patch.object(wmod, "now_npt_str",
                          return_value="12:00:05"):
            await wmod.on_tick(self.bot, self.npl)
            await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(len(msg.edits), 1)
        # The score is unchanged; only the Updated time moved.
        self.assertIn("10.5 ov", msg.edits[0])
        self.assertIn("-# Updated 12:00:05 NPT", msg.edits[0])
        self.assertNotIn("00:00:01", msg.edits[0])

    async def test_detail_button_never_adopts_static_card(self):
        # The user's exact case, under the new-message design:
        # a /game reply posted before the panel code was never
        # registered, so it could never tick. Tapping Detail
        # used to adopt it as the panel (and then it ticked) —
        # that in-place edit is exactly what raced the tick and
        # never stuck. Now the tap sends a NEW visible message
        # with the detail view and adopts nothing: the card
        # stays a static card, no watch is created.
        detail = self._detail()
        card = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        msg = FakeMessage(4001, card)
        self.channel.messages[4001] = msg
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("watch:detail", card)
        with self._mock_search(detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertEqual(interaction.edited, [])
        self.assertNotIn((777, "771678099"), wmod._watches)
        self.assertEqual(len(msg.edits), 0)
        self.assertEqual(msg.content, card)

    async def test_refresh_button_never_adopts_static_card(self):
        detail = self._detail()
        card = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        msg = FakeMessage(4001, card)
        self.channel.messages[4001] = msg
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("panel:refresh", card)
        with self._mock_search(detail):
            await view.children[2].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("10.5 ov", interaction.followup.sent[0])
        self.assertIn("-# Updated", interaction.followup.sent[0])
        self.assertEqual(interaction.edited, [])
        self.assertNotIn((777, "771678099"), wmod._watches)

    async def test_live_button_on_alert_sends_new_message(self):
        msg = self._seed_cricket_panel()
        alert = ("🏏 **WICKET** — Sudurpaschim Royals 76/2 (10.5 ov)\n"
                 "Game ID: 771678099")
        view = wmod.WatchAlertView()
        interaction = self._button_interaction("watch:live", alert,
                                               mid=4002)
        interaction.client = self.bot
        with self._mock_search(self._detail()):
            await view.children[1].callback(interaction)
        # The tap sends a NEW visible message with the compact
        # line — the real panel's message is never edited by the
        # tap, but its stored view switches to compact (18 Nov
        # 2026 repair: a tap must switch the panel's view, or
        # the user stares at a frozen card) and it keeps ticking.
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("🔴 LIVE", interaction.followup.sent[0])
        self.assertIn("10.5 ov", interaction.followup.sent[0])
        self.assertNotIn("Match stats", interaction.followup.sent[0])
        self.assertEqual(interaction.edited, [])
        self.assertEqual(len(msg.edits), 0)

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
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
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


class TestPanelEverySecond(unittest.IsolatedAsyncioTestCase):
    """Every live panel refreshes every second — the user's case
    is 4 panels in one channel, which used to take turns (one
    edit per tick), so each panel's Updated footer only advanced
    about every 4s. Now the tick schedules an edit for every
    panel on every tick, spaced 1s apart in the channel (one
    edit/sec/channel, Discord's sustained budget), so 1 game
    refreshes every 1s, 2 every 2s each, and so on."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp7"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0
        self.games1x2 = _load("games1x2.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _seed_four(self, bucket=None, count=4):
        """Four compact panels in one channel (the user's
        mix: one football, three cricket), all with stale
        bodies so the next tick wants to edit them."""
        msgs = []
        for idx in range(count):
            raw = self.games1x2[idx]
            detail = prov._onex_detail_from_raw(raw)
            game_id = str(raw["id"])
            msg = FakeMessage(2001 + idx, "stale", bucket=bucket)
            self.channel.messages[2001 + idx] = msg
            snap = wmod.snapshot_from_detail(detail)
            snap["_home"], snap["_away"] = (detail.get("home", ""),
                                            detail.get("away", ""))
            wmod._watches[(777, game_id)] = {
                "home": detail.get("home", ""),
                "away": detail.get("away", ""),
                "league": detail.get("league", ""), "detail": "simple",
                "fmt": str(detail.get("format") or ""), "snapshot": snap,
                "missing": 0, "notify": True, "panel_kind": "compact",
                "panel_message_id": 2001 + idx, "panel_body": "stale",
                "panel_base_body": "stale", "panel_static": {},
                "panel_momentum": None, "panel_momentum_ts": 0.0,
                "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
                "panel_timer_sec": None, "panel_timer_ts": 0.0,
            }
            msgs.append(msg)
        return msgs

    def _reset_tick_state(self):
        # The 1s heartbeat gate is real time — reset it so the
        # next tick in the test can edit. (Pacing lives in the
        # shared scheduler now, not in per-panel state, so there
        # is no slot timeline to reset between ticks.)
        for watch in wmod._watches.values():
            watch["last_panel_edit"] = time.monotonic() - 2

    def _ticking_time(self, clock):
        clock["n"] += 1
        return f"12:00:{clock['n']:02d}"

    async def test_four_panels_all_edit_every_tick(self):
        msgs = self._seed_four()
        clock = {"n": 0}
        with patch.object(wmod, "now_npt_str",
                          side_effect=lambda: self._ticking_time(clock)):
            for _ in range(3):
                self._reset_tick_state()
                await wmod.on_tick(self.bot, self.games1x2)
                await _drain_panel_workers()
        self.assertEqual(len(self.channel.sent), 0)
        for msg in msgs:
            self.assertEqual(len(msg.edits), 3)
            footers = [e.rsplit("Updated ", 1)[1][:8]
                       for e in msg.edits]
            # The footer advances on every tick — three edits,
            # three distinct, increasing times.
            self.assertEqual(footers, sorted(footers))
            self.assertEqual(len(set(footers)), 3)

    def test_plan_orders_changed_first_then_oldest(self):
        wmod._watches[(777, "a")] = {"last_panel_edit": 100.0}
        wmod._watches[(777, "b")] = {"last_panel_edit": 50.0}
        wmod._watches[(777, "c")] = {"last_panel_edit": 200.0}
        details = {(777, "a"): {}, (777, "b"): {}, (777, "c"): {}}
        plan = wmod._panel_edit_plan(details, {"b"})
        self.assertEqual(plan[777], [(777, "b"), (777, "a"), (777, "c")])

    async def test_429_pauses_the_whole_channel_not_just_one_panel(self):
        # A 429 is a per-CHANNEL bucket, not a per-panel one: when
        # one panel's edit is rate-limited, the scheduler pauses
        # the whole channel until retry_after. The other panels
        # wait out the same pause instead of firing into a drained
        # bucket — that firing is what used to cascade (the VM
        # journal showed ~14.6 429s/min and only ~30 landed
        # edits/min). Exactly one 429 happens, and every panel
        # still lands its edit — the limited one on the scheduler's
        # single retry after the pause (a 429's retry_after IS the
        # bucket refilling, so the retry lands; a fake that failed
        # forever would 429 the retry too and stall the queue past
        # the drain).
        msgs = self._seed_four()
        resp = SimpleNamespace(status=429, reason="Too Many Requests",
                               headers={"Retry-After": "5"})
        err = discord.HTTPException(resp, "rate limited")
        err.retry_after = 5.0
        msgs[1].edit_error = err
        raises = []
        orig_edit = msgs[1].edit

        async def _counting_edit(*args, **kwargs):
            try:
                return await orig_edit(*args, **kwargs)
            except discord.HTTPException as exc:
                if getattr(exc, "status", None) == 429:
                    raises.append(exc)
                    # Bucket refilled after retry_after: the
                    # scheduler's retry lands instead of 429ing
                    # again.
                    msgs[1].edit_error = None
                raise

        msgs[1].edit = _counting_edit
        times = {}
        for idx in (0, 2, 3):
            msg = msgs[idx]
            orig = msg.edit

            async def _timed(*args, _orig=orig, _idx=idx, **kwargs):
                times.setdefault(_idx, []).append(time.monotonic())
                return await _orig(*args, **kwargs)

            msg.edit = _timed
        self._reset_tick_state()
        await wmod.on_tick(self.bot, self.games1x2)
        # The drain waits out the 5s channel pause (msg2 lands
        # ~7s after msg0: 1s pacing, the pause, the retry, then
        # the 1.5s grown gap).
        await _drain_panel_workers(timeout=15.0)
        self.assertEqual(len(msgs[0].edits), 1)
        # The limited panel landed on the scheduler's retry after
        # the pause — not dropped, not hammered.
        self.assertEqual(len(msgs[1].edits), 1)
        self.assertEqual(len(msgs[2].edits), 1)
        self.assertEqual(len(msgs[3].edits), 1)
        self.assertEqual(len(self.channel.sent), 0)
        # Exactly one 429 — no cascade.
        self.assertEqual(len(raises), 1)
        # The pause delayed the other panels well past their
        # normal 1s slots: msg2 landed ~7s after msg0, not ~2s.
        self.assertGreaterEqual(times[2][0] - times[0][0], 4.0)
        self.assertAlmostEqual(times[3][0] - times[2][0], 1.0, delta=0.75)
        # Once the error clears, the limited panel edits again —
        # and no second 429 follows.
        msgs[1].edit_error = None
        self._reset_tick_state()
        await wmod.on_tick(self.bot, self.games1x2)
        await _drain_panel_workers(timeout=15.0)
        self.assertEqual(len(msgs[1].edits), 2)
        self.assertEqual(len(raises), 1)

    async def test_refresh_budget_divides_evenly(self):
        # The user's rule: 1 game refreshes every 1s, 2 games
        # every 2s each, 3 every 3s, 4 every 4s. The shared
        # scheduler paces ALL edits in the channel 1s apart —
        # exactly Discord's sustained per-channel edit budget —
        # so attempts never exceed the bucket: every scheduled
        # edit lands, no 429s, and the total stays ~60 edits/min
        # whatever N is. Measured in real time against the fake
        # bucket, which refills from the wall clock between ticks.
        # (The old 0.35s spacing tried to edit faster than the
        # budget: 80 attempts, only ~24 granted, the rest 429'd —
        # and the VM journal showed only 38 edits/min for 2
        # panels. The 1.0s slots that replaced it still only
        # reached ~30/min, because the scoreboard edited outside
        # the slot system and every 429 stalled ~4.2s — that is
        # what the shared scheduler fixes.)
        for n, ticks in ((1, 3), (2, 3), (3, 2), (4, 2)):
            await _cancel_panel_workers()
            wmod._watches = {}
            bucket = FakeBucket()
            msgs = self._seed_four(bucket=bucket, count=n)
            for msg in msgs:
                msg.edit_times = []
                orig_edit = msg.edit

                async def _timed_edit(*args, _orig=orig_edit,
                                      _msg=msg, **kwargs):
                    _msg.edit_times.append(time.monotonic())
                    return await _orig(*args, **kwargs)

                msg.edit = _timed_edit
            clock = {"n": 0}
            last = time.monotonic()
            with patch.object(
                    wmod, "now_npt_str",
                    side_effect=lambda: self._ticking_time(clock)):
                for i in range(ticks):
                    now = time.monotonic()
                    # Refill for the whole previous tick — on_tick
                    # AND the drain that waited for the 1s slots —
                    # not just the gap between ticks, or the bucket
                    # starves and 429s edits that are due.
                    bucket.advance(now - last)
                    last = now
                    await wmod.on_tick(self.bot, self.games1x2)
                    await _drain_panel_workers(timeout=10.0)
                    if i < ticks - 1:
                        # Production ticks once a second: the
                        # heartbeat gate (0.9s) only opens when a
                        # real second has passed since the last
                        # edit — back-to-back ticks would all be
                        # gated and nothing would land.
                        await asyncio.sleep(1.1)
            total = sum(len(m.edits) for m in msgs)
            # Every scheduled edit landed — no 429 denied any.
            self.assertEqual(total, n * ticks)
            for msg in msgs:
                self.assertEqual(len(msg.edits), ticks)
                gaps = [b - a for a, b in
                        zip(msg.edit_times, msg.edit_times[1:])]
                for gap in gaps:
                    # Each panel's edits are ~n seconds apart.
                    self.assertAlmostEqual(gap, n, delta=0.75)
            self.assertEqual(len(self.channel.sent), 0)

    async def test_single_panel_refreshes_every_second_for_60_ticks(self):
        # The hard requirement: with exactly ONE live panel in a
        # channel, /game MUST refresh at 1 second — ~60 edits
        # across 60 ticks, no skipped ticks. This runs in real
        # time (~60s): each tick's worker waits for its 1s
        # channel slot, so the drain after every tick lands about
        # a second after the previous one. The fake bucket refills
        # from the measured wall time between ticks, and the fake
        # clock advances one second per render, so no two
        # consecutive footers can ever be identical. At most 1-2
        # edits may be missed to scheduling slack; the exact
        # count is reported in the failure message.
        bucket = FakeBucket()
        msgs = self._seed_four(bucket=bucket, count=1)
        clock = {"n": 0}
        last = time.monotonic()
        with patch.object(
                wmod, "now_npt_str",
                side_effect=lambda: self._ticking_time(clock)):
            for i in range(60):
                now = time.monotonic()
                # Refill for the whole previous tick — on_tick AND
                # the drain that waited for the 1s slot — not just
                # the gap between ticks, or the bucket starves.
                bucket.advance(now - last)
                last = now
                await wmod.on_tick(self.bot, self.games1x2)
                await _drain_panel_workers(timeout=10.0)
                if i < 59:
                    # Production ticks once a second: the heartbeat
                    # gate (0.9s) only opens when a real second
                    # has passed since the last edit.
                    await asyncio.sleep(1.1)
        total = len(msgs[0].edits)
        print(f"[60-tick] single panel edits: {total}/60")
        self.assertGreaterEqual(
            total, 58,
            f"single panel edited only {total}/60 ticks")
        self.assertEqual(len(self.channel.sent), 0)

    async def test_per_minute_panel_log_line(self):
        self._seed_four()
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r.getMessage())
        old_level = wmod.log.level
        wmod.log.addHandler(handler)
        wmod.log.setLevel(logging.INFO)
        try:
            # The window is already 61s old: the next tick logs.
            wmod._panel_log_window_start = time.monotonic() - 61
            wmod._panel_log_edits = 0
            await wmod.on_tick(self.bot, self.games1x2)
            await _drain_panel_workers()
            self.assertEqual(len(records), 1)
            self.assertIn("panels: 4 registered, 0 edits", records[0])
            # The tick's 4 edits land after the log line; a
            # minute later the next tick reports them.
            wmod._panel_log_window_start = time.monotonic() - 61
            await wmod.on_tick(self.bot, self.games1x2)
            await _drain_panel_workers()
            self.assertEqual(len(records), 2)
            self.assertIn("panels: 4 registered, 4 edits", records[1])
            # A tick inside the fresh window logs nothing.
            await wmod.on_tick(self.bot, self.games1x2)
            await _drain_panel_workers()
            self.assertEqual(len(records), 2)
        finally:
            wmod.log.removeHandler(handler)
            wmod.log.setLevel(old_level)


class TestWatchingAndUnwatch(unittest.IsolatedAsyncioTestCase):
    """/watching shows how many games are refreshing and how
    fast each one is; /unwatch fully removes a game (panel
    deleted, watch gone, never ticks again). The 🔕 Unfollow
    button stays notifications-only — it is not /unwatch."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp9"
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
        await _cancel_panel_workers()
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _detail(self, idx=0):
        return prov._onex_detail_from_raw(self.games1x2[idx])

    def _snap(self, idx=0):
        detail = self._detail(idx)
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = detail["home"], detail["away"]
        return detail, snap

    async def _add_row(self, idx, game_id, notify=True, level="simple",
                       kind="compact", snapshot=None):
        detail, snap = self._snap(idx)
        await db.add_watch(777, game_id, detail["home"], detail["away"],
                           detail.get("league", ""), level, "",
                           snapshot if snapshot is not None else snap,
                           notify=notify, panel_kind=kind)
        return detail, snap

    def _seed_panel(self, idx, mid, game_id, notify=True, kind="compact"):
        detail, snap = self._snap(idx)
        if kind == "detail":
            body = wmod.render_detail(detail, wmod.DETAIL_SIMPLE)
        else:
            body = wmod.render_panel(detail, "67:00")
        msg = FakeMessage(mid, body)
        self.channel.messages[mid] = msg
        wmod._watches[(777, game_id)] = {
            "home": detail["home"], "away": detail["away"],
            "league": detail.get("league", ""), "detail": "simple",
            "fmt": str(detail.get("format") or ""), "snapshot": snap,
            "missing": 0, "notify": notify, "panel_kind": kind,
            "panel_message_id": mid, "panel_body": body,
            "panel_base_body": body, "panel_static": {},
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        return msg

    async def test_watching_shows_count_intervals_and_alerts(self):
        import bot as botmod
        _, snap0 = await self._add_row(0, "771747666", notify=True)
        detail1, snap1 = await self._add_row(
            1, "771739998", notify=False, level="advanced", kind="detail")
        up_snap = wmod.snapshot_from_detail({
            "home": "Lumbini Lions", "away": "Kathmandu Gurkhas",
            "home_score": None, "away_score": None, "status": "upcoming",
            "clock": "", "stats": None, "cricket": None})
        await db.add_watch(777, "771747118", "Lumbini Lions",
                           "Kathmandu Gurkhas", "Nepal Premier League",
                           "simple", "", up_snap, notify=True,
                           panel_kind="compact")
        interaction = FakeInteraction(channel_id=777)
        await botmod.cmd_watching.callback(interaction)
        # 18 Nov 2026: /watching defers first, so the text now
        # arrives via followup, not response.send_message.
        self.assertEqual(interaction.response.defer_calls, [(True, True)])
        text = interaction.followup.sent[0]
        # Header: 2 live games refreshing, each about every 2s.
        self.assertIn("2 games refreshing in this channel", text)
        self.assertIn("each about every 2s", text)
        # Football row: score, clock, interval, alerts on.
        self.assertIn("Republic of the Gambia", text)
        self.assertIn(f"{snap0['home_score']}–{snap0['away_score']}", text)
        self.assertIn(snap0["clock"], text)
        self.assertIn("every 2s", text)
        self.assertIn("🔔 alerts on", text)
        # Cricket row (a view-only /game panel): overs, alerts off.
        self.assertIn(detail1["home"], text)
        self.assertIn(f"{snap1['cricket']['over_str']} ov", text)
        self.assertIn("🔕 alerts off", text)
        # Upcoming row: clearly not refreshing yet.
        self.assertIn("Lumbini Lions", text)
        self.assertIn("not refreshing yet", text)

    async def test_watching_single_game_refreshes_every_second(self):
        import bot as botmod
        await self._add_row(0, "771747666", notify=True)
        interaction = FakeInteraction(channel_id=777)
        await botmod.cmd_watching.callback(interaction)
        self.assertEqual(interaction.response.defer_calls, [(True, True)])
        text = interaction.followup.sent[0]
        self.assertIn("1 game refreshing in this channel", text)
        self.assertIn("every second", text)

    async def test_unwatch_removes_watch_and_deletes_panel(self):
        import bot as botmod
        await self._add_row(0, "771747666", notify=True)
        msg = self._seed_panel(0, 2001, "771747666", notify=True)
        interaction = FakeInteraction(channel_id=777)
        interaction.client = self.bot
        await botmod.cmd_unwatch.callback(interaction, "771747666")
        self.assertEqual(
            interaction.followup.sent,
            ["🗑️ Removed **Republic of the Gambia vs Ivory Coast** — "
             "it is no longer refreshing in this channel."])
        self.assertNotIn((777, "771747666"), wmod._watches)
        self.assertEqual(await db.list_watches(777), [])
        self.assertTrue(msg.deleted)
        # And it never ticks again: a tick with the game live
        # posts nothing and edits nothing.
        await wmod.on_tick(self.bot, self.games1x2)
        await _drain_panel_workers()
        self.assertEqual(len(msg.edits), 0)
        self.assertEqual(len(self.channel.sent), 0)

    async def test_unwatch_removes_view_only_game_panel(self):
        import bot as botmod
        # A /game reply (notifications off, no /watch): /unwatch
        # by team name still fully removes it — deleting the
        # reply message is the remove.
        await self._add_row(0, "771747666", notify=False,
                            level="simple", kind="detail")
        msg = self._seed_panel(0, 2001, "771747666", notify=False,
                               kind="detail")
        interaction = FakeInteraction(channel_id=777)
        interaction.client = self.bot
        await botmod.cmd_unwatch.callback(interaction, "Gambia")
        self.assertIn("Removed", interaction.followup.sent[0])
        self.assertNotIn((777, "771747666"), wmod._watches)
        self.assertEqual(await db.list_watches(777), [])
        self.assertTrue(msg.deleted)

    async def test_unfollow_button_still_only_toggles(self):
        # The 🔕 button is NOT /unwatch: it stops alerts only,
        # the panel keeps ticking until full-time.
        detail, _ = await self._add_row(0, "771747666", notify=True)
        msg = self._seed_panel(0, 2001, "771747666", notify=True)
        view = wmod.WatchAlertView()
        interaction = FakeInteraction(
            message=SimpleNamespace(
                content=wmod.render_detail(detail, wmod.DETAIL_ADVANCED)),
            data={"custom_id": "panel:unfollow"})
        await view.children[7].callback(interaction)
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertFalse(wmod._watches[(777, "771747666")]["notify"])
        self.assertIn("Notifications off", interaction.followup.sent[0])
        self.assertFalse(msg.deleted)
        # ...and the panel still ticks on the next tick.
        await wmod.on_tick(self.bot, self.games1x2)
        await _drain_panel_workers()
        self.assertEqual(len(msg.edits), 1)


class _VirtualClock:
    """A hand-driven clock for the edit scheduler: only the
    test driver advances it, and the scheduler's virtual sleep
    waits on it — so a 60s measurement runs in milliseconds of
    real time. Starts at 1000.0 so the scheduler's "last edit"
    (initially 0.0) is long past, exactly as in production where
    monotonic time is already large when the bot starts."""

    def __init__(self):
        self.now = 1000.0

    def advance(self, seconds):
        self.now += seconds


class TestSharedEditBudget(unittest.IsolatedAsyncioTestCase):
    """One shared edit budget per channel: panels AND the
    scoreboard go through the same scheduler, so the bucket
    never sees more than 1 edit/s and 429s stop.

    Root cause (18 Nov 2026, VM journal): the panels paced at
    exactly 1 edit/s assuming they were the only editors, while
    the scoreboard edited the same channel outside the slot
    system — ~14.6 Discord 429s/min, each stalling ~4.2s, so 2
    panels landed only ~30 edits/min instead of ~60. These tests
    measure the fix in virtual time: the scheduler's sleeps wait
    on a hand-driven clock, and the fake bucket refills from the
    same clock, so 60 simulated seconds run in milliseconds."""

    async def asyncSetUp(self):
        self.clock = _VirtualClock()
        self._orig_now = cemod._now
        self._orig_sleep = cemod._sleep
        cemod._now = lambda: self.clock.now
        clock = self.clock

        async def _vsleep(delay):
            # Wait until the driver advances the clock past the
            # target — the driver is the only thing that moves
            # time, so this can never spin past it.
            end = clock.now + delay
            while clock.now < end:
                await asyncio.sleep(0)

        cemod._sleep = _vsleep
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)
        self._tasks = []

    async def asyncTearDown(self):
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await cemod.reset_for_tests()
        cemod._now = self._orig_now
        cemod._sleep = self._orig_sleep

    async def _tick_virtual(self, seconds=1.0, step=0.1):
        """Advance the virtual clock by `seconds`, letting the
        scheduler workers run as time passes."""
        target = self.clock.now + seconds
        while self.clock.now < target:
            self.clock.advance(min(step, target - self.clock.now))
            await asyncio.sleep(0)

    async def _run_scenario(self, n_panels, seconds=60,
                            scoreboard_every=10, bucket_window=5.0,
                            gap_samples=None):
        """Simulate `seconds` virtual seconds: every second one
        edit per panel (as the tick hands them fresh detail) plus
        a scoreboard edit every `scoreboard_every` seconds (the
        scoreboard only edits when the score changes — a busy
        minute). `bucket_window` calibrates the fake bucket:
        5.0s is Discord's standard 5-per-5s bucket; 10.0s
        refills 0.5/s, modelling the tighter budget production
        proved (429s at a steady 1 edit/s). `gap_samples`
        (a list) records the scheduler's adaptive gap each
        simulated second. Returns (per-panel landed counts,
        scoreboard landed count, 429 count)."""
        bucket = FakeBucket(limit=5, window=bucket_window,
                            now_fn=lambda: self.clock.now)
        panel_msgs = []
        for idx in range(n_panels):
            msg = FakeMessage(2001 + idx, "stale", bucket=bucket)
            self.channel.messages[msg.id] = msg
            panel_msgs.append(msg)
        sb_msg = FakeMessage(2999, "stale", bucket=bucket)
        self.channel.messages[sb_msg.id] = sb_msg
        denied = {"n": 0}
        for msg in panel_msgs + [sb_msg]:
            orig = msg.edit

            async def _counting(*args, _orig=orig, **kwargs):
                try:
                    return await _orig(*args, **kwargs)
                except discord.HTTPException as exc:
                    if getattr(exc, "status", None) == 429:
                        denied["n"] += 1
                    raise

            msg.edit = _counting
        for s in range(seconds):
            for idx, msg in enumerate(panel_msgs):
                self._tasks.append(asyncio.create_task(
                    cemod.edit_message(
                        self.bot, 777, msg.id,
                        content=f"panel {idx} t={s}")))
            if s % scoreboard_every == 0:
                self._tasks.append(asyncio.create_task(
                    cemod.edit_message(
                        self.bot, 777, sb_msg.id,
                        content=f"scoreboard t={s}")))
            if gap_samples is not None:
                gap_samples.append(cemod.channel_gap(777))
            await self._tick_virtual(1.0)
        # Let the trailing queue land (a few more virtual
        # seconds); those edits count toward the total too.
        await self._tick_virtual(5.0)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        return ([len(m.edits) for m in panel_msgs],
                len(sb_msg.edits), denied["n"])

    async def test_one_panel_plus_scoreboard_edits_every_second(self):
        counts, sb_edits, denied = await self._run_scenario(1)
        total = sum(counts) + sb_edits
        print(f"[shared-budget] 1 panel + scoreboard: "
              f"panel={counts}, scoreboard={sb_edits}, "
              f"total={total}, 429s={denied}")
        # One live game refreshes every 1s: ~60 edits/min total,
        # the panel taking almost all of them (the scoreboard
        # edits ~6 times a minute), zero 429s.
        self.assertEqual(denied, 0)
        self.assertGreaterEqual(total, 55)
        self.assertLessEqual(total, 65)
        self.assertGreaterEqual(counts[0], 50)
        self.assertEqual(sb_edits, 6)

    async def test_two_panels_plus_scoreboard_divide_evenly(self):
        counts, sb_edits, denied = await self._run_scenario(2)
        total = sum(counts) + sb_edits
        print(f"[shared-budget] 2 panels + scoreboard: "
              f"panels={counts}, scoreboard={sb_edits}, "
              f"total={total}, 429s={denied}")
        self.assertEqual(denied, 0)
        self.assertGreaterEqual(total, 55)
        self.assertLessEqual(total, 65)
        # 2 games: every ~2s each, even split.
        for count in counts:
            self.assertGreaterEqual(count, 24)
            self.assertLessEqual(count, 36)
        self.assertEqual(sb_edits, 6)

    async def test_three_panels_plus_scoreboard_divide_evenly(self):
        counts, sb_edits, denied = await self._run_scenario(3)
        total = sum(counts) + sb_edits
        print(f"[shared-budget] 3 panels + scoreboard: "
              f"panels={counts}, scoreboard={sb_edits}, "
              f"total={total}, 429s={denied}")
        self.assertEqual(denied, 0)
        self.assertGreaterEqual(total, 55)
        self.assertLessEqual(total, 65)
        # 3 games: every ~3s each, even split.
        for count in counts:
            self.assertGreaterEqual(count, 15)
            self.assertLessEqual(count, 25)
        self.assertEqual(sb_edits, 6)

    async def test_four_panels_plus_scoreboard_divide_evenly(self):
        counts, sb_edits, denied = await self._run_scenario(4)
        total = sum(counts) + sb_edits
        print(f"[shared-budget] 4 panels + scoreboard: "
              f"panels={counts}, scoreboard={sb_edits}, "
              f"total={total}, 429s={denied}")
        self.assertEqual(denied, 0)
        self.assertGreaterEqual(total, 55)
        self.assertLessEqual(total, 65)
        # 4 games: every ~4s each, even split — no panel starved.
        for count in counts:
            self.assertGreaterEqual(count, 11)
            self.assertLessEqual(count, 19)
        self.assertEqual(sb_edits, 6)

    async def test_429_does_not_cascade(self):
        # One injected 429 (retry_after 2s) on a panel edit: the
        # channel pauses, the failed job is retried on the next
        # submit and lands, and no second 429 ever follows —
        # the old design re-fired into the drained bucket and
        # produced ~14.6 429s/min.
        bucket = FakeBucket(now_fn=lambda: self.clock.now)
        msgs = []
        for idx in range(2):
            msg = FakeMessage(2001 + idx, "stale", bucket=bucket)
            self.channel.messages[msg.id] = msg
            msgs.append(msg)
        denied = {"n": 0}
        for msg in msgs:
            orig = msg.edit

            async def _counting(*args, _orig=orig, **kwargs):
                try:
                    return await _orig(*args, **kwargs)
                except discord.HTTPException as exc:
                    if getattr(exc, "status", None) == 429:
                        denied["n"] += 1
                    raise

            msg.edit = _counting
        resp = SimpleNamespace(status=429, reason="Too Many Requests",
                               headers={"Retry-After": "2"})
        err = discord.HTTPException(resp, "rate limited")
        err.retry_after = 2.0
        raised = {"n": 0}
        orig0 = msgs[0].edit

        async def _once_429(*args, **kwargs):
            if raised["n"] == 0:
                raised["n"] += 1
                raise err
            return await orig0(*args, **kwargs)

        msgs[0].edit = _once_429
        results = []
        for s in range(15):
            for idx, msg in enumerate(msgs):
                self._tasks.append(asyncio.create_task(
                    cemod.edit_message(
                        self.bot, 777, msg.id,
                        content=f"panel {idx} t={s}")))
            await self._tick_virtual(1.0)
        await self._tick_virtual(2.0)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for msg in msgs:
            results.append(len(msg.edits))
        # Exactly one 429 (the injected one, counted by `raised`);
        # the bucket itself never denied anything (`denied` is
        # 0) — the scheduler never exceeded the budget, so the
        # 429 did not cascade. The failed panel still lands edits
        # afterwards, and the other panel never stops.
        self.assertEqual(denied["n"], 0)
        self.assertEqual(raised["n"], 1)
        self.assertGreaterEqual(results[0], 5)
        self.assertGreaterEqual(results[1], 5)
        # Total landed is ~1/s over the ~17 virtual seconds,
        # minus the ~2s the channel paused for the 429 and the
        # elevated gap afterwards (×1.5, easing back only after
        # 10 clean edits — so ~10, not ~14). The point is the
        # 429 did not cascade: both panels keep landing edits.
        self.assertGreaterEqual(sum(results), 9)
        self.assertLessEqual(sum(results), 16)

    async def test_priority_tap_jumps_the_queue(self):
        # Three normal edits queued, then a priority tap: the tap
        # lands ahead of the normals that were queued before it
        # (third, not fifth) — it cannot preempt the edit already
        # popped for its pacing wait, but it never waits behind a
        # whole tick of heartbeats.
        bucket = FakeBucket(now_fn=lambda: self.clock.now)
        landed = []
        msgs = []
        for idx in range(4):
            msg = FakeMessage(2001 + idx, "stale", bucket=bucket)
            self.channel.messages[msg.id] = msg
            msgs.append(msg)
            orig = msg.edit

            async def _recording(*args, _orig=orig, _idx=idx, **kwargs):
                await _orig(*args, **kwargs)
                landed.append(_idx)

            msg.edit = _recording
        # Fill the channel with a slow first edit: block the
        # worker's pacing by submitting the first job, letting it
        # land, then queueing three normals before the tap.
        self._tasks.append(asyncio.create_task(
            cemod.edit_message(self.bot, 777, 2001, content="first")))
        await self._tick_virtual(0.5)
        for idx in (1, 2, 3):
            self._tasks.append(asyncio.create_task(
                cemod.edit_message(
                    self.bot, 777, 2001 + idx, content=f"normal {idx}")))
        await self._tick_virtual(0.2)
        self._tasks.append(asyncio.create_task(
            cemod.edit_message(
                self.bot, 777, 2001, content="tap", priority=True)))
        await self._tick_virtual(6.0)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.assertEqual(landed[0], 0)
        # The tap (message 2001 again) is the third edit to land —
        # ahead of normals 2 and 3, which were queued before it.
        self.assertEqual(landed[2], 0)
        self.assertEqual(landed, [0, 1, 0, 2, 3])

    async def test_first_429_seen_once_and_retried_after_pause(self):
        # Production falsified the old design (18 Nov 2026):
        # discord.py retries a 429 up to 5x inside the call, so
        # the scheduler never saw the FIRST 429 and the worker
        # stalled ~20s in hidden retries. The raw PATCH sees the
        # first 429 at once: exactly one rate-limit warning, the
        # channel pauses for retry_after, the job is retried once
        # after the pause and lands — ~4s total, never ~20s.
        bucket = FakeBucket(now_fn=lambda: self.clock.now)
        msg = FakeMessage(2001, "stale", bucket=bucket)
        self.channel.messages[msg.id] = msg
        calls = []
        orig_edit = msg.edit

        async def _once_429(*args, **kwargs):
            calls.append(self.clock.now)
            if len(calls) == 1:
                resp = SimpleNamespace(status=429,
                                       reason="Too Many Requests",
                                       headers={"Retry-After": "4"})
                err = discord.HTTPException(resp, "rate limited")
                err.retry_after = 4.0
                raise err
            return await orig_edit(*args, **kwargs)

        msg.edit = _once_429
        t0 = self.clock.now
        with self.assertLogs("edits", level="INFO") as logs:
            task = asyncio.create_task(
                cemod.edit_message(self.bot, 777, 2001, content="v1"))
            await self._tick_virtual(6.0)
            result = await task
        self.assertEqual(result, ("ok", "v1"))
        self.assertEqual(len(calls), 2)  # first attempt + one retry
        self.assertEqual(len(msg.edits), 1)
        # The retry landed ~4s after the submit (one retry_after),
        # not ~20s (five hidden discord.py retries).
        self.assertGreaterEqual(calls[1] - t0, 3.5)
        self.assertLessEqual(calls[1] - t0, 5.5)
        # Exactly one rate-limit warning for the one logical edit.
        limited = [line for line in logs.output if "rate-limited" in line]
        self.assertEqual(len(limited), 1)

    async def test_scoreboard_edit_lands_after_a_429_pause(self):
        # The old 2.0s scoreboard timeout was shorter than ONE
        # discord.py retry sleep, so every scoreboard edit timed
        # out and was discarded while resubmitting every tick.
        # With the raw PATCH (429 at once, one retry after the
        # pause) and the 5s timeout, the edit lands.
        bucket = FakeBucket(now_fn=lambda: self.clock.now)
        msg = FakeMessage(2001, "stale", bucket=bucket)
        self.channel.messages[msg.id] = msg
        calls = {"n": 0}
        orig_edit = msg.edit

        async def _once_429(*args, **kwargs):
            if calls["n"] == 0:
                calls["n"] += 1
                resp = SimpleNamespace(status=429,
                                       reason="Too Many Requests",
                                       headers={"Retry-After": "4"})
                err = discord.HTTPException(resp, "rate limited")
                err.retry_after = 4.0
                raise err
            return await orig_edit(*args, **kwargs)

        msg.edit = _once_429
        task = asyncio.create_task(
            cemod.edit_message(self.bot, 777, 2001, content="sb",
                               timeout=5.0))
        await self._tick_virtual(6.0)
        result = await task
        self.assertEqual(result, ("ok", "sb"))
        self.assertEqual(len(msg.edits), 1)

    async def test_gap_adapts_when_budget_is_tighter_than_model(self):
        # Production falsified the 1.0s premise: Discord 429'd at
        # a steady 1 edit/s from minute one. Calibrated to that
        # observation, the fake bucket here refills 0.5/s (5 per
        # 10s) instead of 1/s: the gap must grow so 429s stay
        # rare, and the run reports the sustainable rate honestly
        # instead of hammering the bucket.
        gaps = []
        counts, sb_edits, denied = await self._run_scenario(
            2, bucket_window=10.0, gap_samples=gaps)
        total = sum(counts) + sb_edits
        print(f"[shared-budget] tighter bucket (refill 0.5/s): "
              f"panels={counts}, scoreboard={sb_edits}, "
              f"total={total}, 429s={denied}, "
              f"max gap={max(gaps):.1f}s")
        # Rare 429s (a couple while the gap adapts), never the
        # ~15/min the old design produced.
        self.assertLessEqual(denied, 4)
        self.assertGreaterEqual(max(gaps), 1.5)
        # The sustainable rate at the adapted gap (~2s): well
        # below 60/min — that is the honest number for this
        # budget, not a failure to fix.
        self.assertGreaterEqual(total, 20)
        self.assertLessEqual(total, 45)

    async def test_worker_start_is_logged(self):
        # The total absence of "[edits]" lines in the VM journal
        # made the scheduler invisible — a dead or stalled worker
        # could not be told apart from a healthy one. The worker
        # now logs its start.
        bucket = FakeBucket(now_fn=lambda: self.clock.now)
        msg = FakeMessage(2001, "stale", bucket=bucket)
        self.channel.messages[msg.id] = msg
        with self.assertLogs("edits", level="INFO") as logs:
            task = asyncio.create_task(
                cemod.edit_message(self.bot, 777, 2001, content="v1"))
            await self._tick_virtual(2.0)
            await task
        self.assertTrue(
            any("edit worker started" in line for line in logs.output),
            logs.output)


class _FakeResp:
    """Minimal aiohttp response stand-in for _raw_patch tests."""

    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Minimal aiohttp session stand-in: patch() returns a
    canned response and records the URL + body."""

    def __init__(self, status, data):
        self.status = status
        self.data = data
        self.calls = []

    def patch(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return _FakeResp(self.status, self.data)


class TestRawPatchMapping(unittest.IsolatedAsyncioTestCase):
    """The raw PATCH itself (channel_edits._raw_patch): status
    mapping and retry_after parsing, with the session stubbed —
    no network, no token."""

    async def _run_raw(self, status, data):
        session = _FakeSession(status, data)
        orig = cemod._get_session

        async def _fake_session(bot):
            return session

        cemod._get_session = _fake_session
        try:
            bot = SimpleNamespace(http=SimpleNamespace(token="tok"))
            msg = SimpleNamespace(
                channel=SimpleNamespace(id=777), id=2001)
            return (await cemod._raw_patch(
                        bot, msg, {"content": "v1"}), session)
        finally:
            cemod._get_session = orig

    async def test_200_returns_ok(self):
        (status, data), session = await self._run_raw(
            200, {"id": "2001", "content": "v1"})
        self.assertEqual(status, 200)
        self.assertIn("/channels/777/messages/2001",
                      session.calls[0][0])
        self.assertEqual(session.calls[0][1]["content"], "v1")

    async def test_429_returns_retry_after(self):
        (status, data), _ = await self._run_raw(
            429, {"retry_after": 4.5, "global": False})
        self.assertEqual(status, 429)
        self.assertEqual(data["retry_after"], 4.5)

    async def test_404_returns_gone(self):
        (status, data), _ = await self._run_raw(
            404, {"message": "Unknown Message", "code": 10008})
        self.assertEqual(status, 404)

    async def test_no_token_falls_back_to_message_edit(self):
        # No token (tests / offline): the edit goes through
        # message.edit with the same (status, data) contract.
        channel = FakeChannel()
        msg = FakeMessage(2001, "stale")
        channel.messages[2001] = msg
        bot = FakeBot(channel)  # no .http, no token
        status, data = await cemod._raw_patch(
            bot, msg, {"content": "v2"})
        self.assertEqual((status, data), (200, {}))
        self.assertEqual(msg.content, "v2")

    async def test_no_token_fallback_maps_429(self):
        channel = FakeChannel()
        msg = FakeMessage(2001, "stale")
        channel.messages[2001] = msg
        err = discord.HTTPException(
            SimpleNamespace(status=429, reason="Too Many Requests"),
            "rate limited")
        err.retry_after = 3.0
        msg.edit_error = err
        bot = FakeBot(channel)
        status, data = await cemod._raw_patch(
            bot, msg, {"content": "v2"})
        self.assertEqual(status, 429)
        self.assertEqual(data, {"retry_after": 3.0})


class TestWatchingButton(unittest.IsolatedAsyncioTestCase):
    """The 9th button — 👀 Watching: how many games are live in
    this channel and each one's refresh speed (1 game -> every
    1s, 2 -> every 2s each, ...). The label updates as games go
    live and end, and tapping it shows the /watching list,
    ephemeral, to the tapper only."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp8"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True
        wmod._panel_log_window_start = 0.0
        wmod._panel_log_edits = 0
        self.games1x2 = _load("games1x2.json")
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _watching_button(self, view):
        return next(b for b in view.children
                    if b.custom_id.startswith("panel:watching"))

    def _seed_live_panel(self, idx, mid, game_id):
        detail = prov._onex_detail_from_raw(self.games1x2[idx])
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = detail["home"], detail["away"]
        snap["status"] = "2H"
        body = wmod.render_panel(detail, "67:00")
        msg = FakeMessage(mid, body)
        self.channel.messages[mid] = msg
        wmod._watches[(777, game_id)] = {
            "home": detail["home"], "away": detail["away"],
            "league": detail.get("league", ""), "detail": "simple",
            "fmt": str(detail.get("format") or ""), "snapshot": snap,
            "missing": 0, "missing_since": 0.0, "notify": True,
            "panel_kind": "compact", "panel_message_id": mid,
            "panel_body": body, "panel_base_body": body,
            "panel_static": {}, "panel_momentum": None,
            "panel_momentum_ts": 0.0, "last_panel_edit": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        return msg

    async def _add_row(self, idx, game_id, notify=True, level="simple",
                       kind="compact", snapshot=None):
        detail = prov._onex_detail_from_raw(self.games1x2[idx])
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = detail["home"], detail["away"]
        await db.add_watch(777, game_id, detail["home"], detail["away"],
                           detail.get("league", ""), level, "",
                           snapshot if snapshot is not None else snap,
                           notify=notify, panel_kind=kind)
        return detail, snap

    def test_label_and_interval_divide(self):
        # 1 game -> every 1s, 2 -> every 2s each, 3 -> 3s, 4 -> 4s.
        self.assertEqual(wmod.watching_label(None), "👀 Watching")
        self.assertEqual(wmod.watching_label(0), "👀 Watching: 0")
        self.assertEqual(wmod.watching_label(1), "👀 Watching: 1 · 1s")
        self.assertEqual(wmod.watching_label(2), "👀 Watching: 2 · 2s each")
        self.assertEqual(wmod.watching_label(3), "👀 Watching: 3 · 3s each")
        self.assertEqual(wmod.watching_label(4), "👀 Watching: 4 · 4s each")
        self.assertEqual(wmod.refresh_interval_s(1), 1)
        self.assertEqual(wmod.refresh_interval_s(2), 2)
        self.assertEqual(wmod.refresh_interval_s(3), 3)
        self.assertEqual(wmod.refresh_interval_s(4), 4)
        self.assertEqual(wmod.refresh_interval_s(0), 1)

    def test_live_count_counts_only_live_in_channel(self):
        self._seed_live_panel(0, 2001, "771747666")
        self._seed_live_panel(2, 2002, "771750037")
        # Upcoming in the same channel does not count.
        up = wmod.snapshot_from_detail({
            "home": "Lumbini Lions", "away": "Kathmandu Gurkhas",
            "home_score": None, "away_score": None, "status": "upcoming",
            "clock": "", "stats": None, "cricket": None})
        wmod._watches[(777, "771747118")] = {
            "home": "Lumbini Lions", "away": "Kathmandu Gurkhas",
            "league": "", "detail": "simple", "fmt": "", "snapshot": up,
            "missing": 0, "notify": True, "panel_kind": "compact",
            "panel_message_id": None, "panel_body": None,
            "panel_base_body": None, "panel_static": {},
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            "last_panel_edit": 0.0, "panel_timer_sec": None,
            "panel_timer_ts": 0.0}
        # A live game in another channel does not count either.
        wmod._watches[(888, "771747666")] = {
            "snapshot": {"status": "2H"}}
        self.assertEqual(wmod.live_watch_count(777), 2)
        self.assertEqual(wmod.live_watch_count(888), 1)

    async def test_view_label_from_count_and_static_default(self):
        view = wmod.WatchAlertView("771747666", live_count=1)
        self.assertEqual(self._watching_button(view).label,
                         "👀 Watching: 1 · 1s")
        self.assertEqual(self._watching_button(view).custom_id,
                         "panel:watching:771747666")
        view2 = wmod.WatchAlertView("771747666", live_count=2)
        self.assertEqual(self._watching_button(view2).label,
                         "👀 Watching: 2 · 2s each")
        view0 = wmod.WatchAlertView()
        self.assertEqual(self._watching_button(view0).label, "👀 Watching")
        self.assertEqual(self._watching_button(view0).custom_id,
                         "panel:watching")
        # Nine buttons now, the old eight untouched and in order.
        self.assertEqual(
            [b.custom_id for b in view0.children],
            ["watch:detail", "watch:live", "panel:refresh",
             "panel:timeline", "panel:simple", "panel:advanced",
             "panel:follow", "panel:unfollow", "panel:watching"])

    async def test_view_label_from_channel(self):
        self._seed_live_panel(0, 2001, "771747666")
        view = wmod.WatchAlertView("771747666", channel_id=777)
        self.assertEqual(self._watching_button(view).label,
                         "👀 Watching: 1 · 1s")

    async def test_refresh_panel_label_updates_with_count(self):
        # 1 live -> the panel's button says 1 · 1s.
        msg = self._seed_live_panel(0, 2001, "771747666")
        watch = wmod._watches[(777, "771747666")]
        detail = prov._onex_detail_from_raw(self.games1x2[0])
        detail = dict(detail)
        detail["timer_sec"] = 4020
        self.assertTrue(await wmod._refresh_panel(
            self.bot, 777, "771747666", watch, detail))
        self.assertEqual(
            self._watching_button(msg.view).label, "👀 Watching: 1 · 1s")
        # A second game goes live in the same channel -> 2 · 2s.
        self._seed_live_panel(2, 2002, "771750037")
        detail["timer_sec"] = 4080
        self.assertTrue(await wmod._refresh_panel(
            self.bot, 777, "771747666", watch, detail))
        self.assertEqual(
            self._watching_button(msg.view).label,
            "👀 Watching: 2 · 2s each")
        # The second game ends -> back to 1 · 1s.
        del wmod._watches[(777, "771750037")]
        detail["timer_sec"] = 4140
        self.assertTrue(await wmod._refresh_panel(
            self.bot, 777, "771747666", watch, detail))
        self.assertEqual(
            self._watching_button(msg.view).label, "👀 Watching: 1 · 1s")

    async def test_watching_button_tap_sends_new_message(self):
        import bot as botmod
        _, snap0 = await self._add_row(0, "771747666", notify=True)
        detail1, snap1 = await self._add_row(
            1, "771739998", notify=False, level="advanced", kind="detail")
        up_snap = wmod.snapshot_from_detail({
            "home": "Lumbini Lions", "away": "Kathmandu Gurkhas",
            "home_score": None, "away_score": None, "status": "upcoming",
            "clock": "", "stats": None, "cricket": None})
        await db.add_watch(777, "771747118", "Lumbini Lions",
                           "Kathmandu Gurkhas", "Nepal Premier League",
                           "simple", "", up_snap, notify=True,
                           panel_kind="compact")
        view = wmod.WatchAlertView("771747666", live_count=2)
        interaction = FakeInteraction(
            channel_id=777,
            message=SimpleNamespace(
                content=wmod.render_detail(
                    prov._onex_detail_from_raw(self.games1x2[0]),
                    wmod.DETAIL_ADVANCED)),
            data={"custom_id": "panel:watching:771747666"})
        await self._watching_button(view).callback(interaction)
        # NEW visible channel message (was ephemeral): the list
        # itself is the answer — no response.send_message, no
        # "panel updated" follow-up — and it carries one ❌
        # Remove button per watched game.
        self.assertEqual(len(self.channel.sent), 0)
        self.assertEqual(interaction.response.sent, [])
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("2 games refreshing in this channel", text)
        self.assertIn("each about every 2s", text)
        self.assertIn("Republic of the Gambia", text)
        self.assertIn(f"{snap0['home_score']}–{snap0['away_score']}", text)
        self.assertIn("every 2s", text)
        self.assertIn("🔔 alerts on", text)
        self.assertIn(detail1["home"], text)
        self.assertIn(f"{snap1['cricket']['over_str']} ov", text)
        self.assertIn("🔕 alerts off", text)
        self.assertIn("not refreshing yet", text)
        list_view = interaction.followup.sent_views[0]
        self.assertIsInstance(list_view, wmod.WatchingListView)
        self.assertEqual(
            sorted(b.custom_id for b in list_view.children),
            ["panel:remove:771739998", "panel:remove:771747118",
             "panel:remove:771747666"])
        self.assertTrue(all(b.item.label.startswith("❌ Remove ")
                            for b in list_view.children))

    async def test_watching_button_matches_command(self):
        import bot as botmod
        await self._add_row(0, "771747666", notify=True)
        view = wmod.WatchAlertView("771747666", live_count=1)
        interaction = FakeInteraction(
            channel_id=777,
            message=SimpleNamespace(
                content=wmod.render_detail(
                    prov._onex_detail_from_raw(self.games1x2[0]),
                    wmod.DETAIL_SIMPLE)),
            data={"custom_id": "panel:watching:771747666"})
        await self._watching_button(view).callback(interaction)
        cmd = FakeInteraction(channel_id=777)
        await botmod.cmd_watching.callback(cmd)
        # Same helper, same text — the button and the command
        # can never drift apart (the button's message is
        # visible, the command's reply stays ephemeral). Both
        # now arrive via followup: the button's, and the
        # command's after its defer.
        self.assertEqual(interaction.followup.sent, cmd.followup.sent)

    async def test_watching_button_with_nothing_watched(self):
        view = wmod.WatchAlertView(live_count=0)
        interaction = FakeInteraction(
            channel_id=777,
            message=SimpleNamespace(content=""),
            data={"custom_id": "panel:watching"})
        await self._watching_button(view).callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["No games watched in this channel. "
                          "Use `/watch <game>` to follow one."])
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertEqual(len(self.channel.sent), 0)
        # No games, so no Remove buttons either.
        list_view = interaction.followup.sent_views[0]
        self.assertIsInstance(list_view, wmod.WatchingListView)
        self.assertEqual(list_view.children, [])


class TestNewMessageButtons(unittest.IsolatedAsyncioTestCase):
    """Button taps send a NEW channel message (18 Nov 2026, the
    user's call: editing in the same panel "is not working
    properly"). The live panel is never edited by a tap — its
    kind, body and message stay exactly as they are, and the
    tick keeps editing it."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp9"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        self._orig_cooldown = dict(wmod._refresh_cooldown)
        wmod._watches = {}
        wmod._loaded = True
        wmod._refresh_cooldown = {}
        self.games1x2 = _load("games1x2.json")
        self.detail = prov._onex_detail_from_raw(self.games1x2[0])
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        wmod._refresh_cooldown = self._orig_cooldown
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _view(self, game_id=None):
        return wmod.WatchAlertView(game_id)

    def _interaction(self, custom_id, content=None):
        if content is None:
            content = wmod.render_detail(self.detail,
                                         wmod.DETAIL_ADVANCED)
        return FakeInteraction(
            message=SimpleNamespace(content=content),
            data={"custom_id": custom_id})

    def _mock_search(self, detail):
        return patch.object(
            prov, "onex_search",
            new=AsyncMock(return_value={"games": [], "detail": detail}))

    def _seed_panel(self, kind="compact", detail_level="simple"):
        snap = wmod.snapshot_from_detail(
            {"home": "Republic of the Gambia", "away": "Ivory Coast",
             "home_score": 0, "away_score": 1, "status": "2H", "minute": 67,
             "clock": "67'", "stats": [], "cricket": None})
        snap["_home"], snap["_away"] = ("Republic of the Gambia",
                                        "Ivory Coast")
        body = wmod.render_panel(self.detail, "67:00")
        msg = FakeMessage(2001, body)
        self.channel.messages[2001] = msg
        wmod._watches[(777, "771747666")] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": detail_level,
            "fmt": "", "snapshot": snap, "missing": 0,
            "missing_since": 0.0, "notify": True, "panel_kind": kind,
            "panel_message_id": 2001, "panel_body": body,
            "panel_base_body": body, "panel_static": {},
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            "last_panel_edit": 0.0, "panel_timer_sec": None,
            "panel_timer_ts": 0.0,
        }
        return msg, body

    async def test_detail_tap_sends_new_message_panel_moves_to_it(self):
        msg, body = self._seed_panel()
        view = self._view()
        interaction = self._interaction("watch:detail")
        with self._mock_search(self.detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Match stats", interaction.followup.sent[0])
        self.assertEqual(interaction.edited, [])
        self.assertEqual(len(msg.edits), 0)
        self.assertEqual(msg.content, body)
        watch = wmod._watches[(777, "771747666")]
        # The tap never edits the OLD panel message — but the
        # panel MOVES to the new message (18 Nov 2026, proven
        # live: the tap's own message was a dead copy while the
        # tick kept editing the old panel elsewhere), in detail
        # view, so the next tick refreshes the message in front
        # of the user.
        self.assertEqual(watch["panel_kind"], "detail")
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertEqual(watch["panel_body"],
                         interaction.followup.sent[0])

    async def test_live_tap_sends_new_compact_message(self):
        msg, body = self._seed_panel(kind="detail")
        view = self._view()
        interaction = self._interaction("watch:live")
        with self._mock_search(self.detail):
            await view.children[1].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("🔴 LIVE", text)
        self.assertIn("67:00", text)
        self.assertNotIn("Match stats", text)
        self.assertEqual(interaction.edited, [])
        self.assertEqual(len(msg.edits), 0)
        # Live switches the panel's stored view back to compact.
        self.assertEqual(
            wmod._watches[(777, "771747666")]["panel_kind"], "compact")

    async def test_advanced_tap_with_no_stats_says_so_plainly(self):
        # The user's screenshot case (Cheetah Kasoa vs Gaz
        # Nobles): Advanced shows "No match statistics
        # available yet." — that is CORRECT, not a bug: the
        # feed sends no tabloStats for that game, so there is
        # nothing to show. The tap still sends its new message.
        msg, body = self._seed_panel()
        no_stats = dict(self.detail)
        no_stats["stats"] = []
        view = self._view()
        interaction = self._interaction("panel:advanced")
        with self._mock_search(no_stats):
            await view.children[5].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("No match statistics available yet.", text)
        self.assertIn("Game ID: 771747666", text)
        self.assertEqual(interaction.edited, [])
        self.assertEqual(len(msg.edits), 0)

    async def test_advanced_tap_with_failed_details_feed_says_so(self):
        # The gameInfo 400s (18 Nov 2026, VM journal): the
        # details feed is not answering from the bot's server.
        # Advanced with no stats AND a failed details feed
        # says so plainly — it does NOT pretend the stats feed
        # itself failed (stats come from the live list, which
        # is fine; this game just carries no stats table).
        msg, body = self._seed_panel()
        no_stats = dict(self.detail)
        no_stats["stats"] = []
        no_stats["info_failed"] = True
        view = self._view()
        interaction = self._interaction("panel:advanced")
        with self._mock_search(no_stats):
            await view.children[5].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        text = interaction.followup.sent[0]
        self.assertIn("No match statistics available for this game.",
                      text)
        self.assertIn("details feed is not answering", text)
        self.assertIn("Game ID: 771747666", text)
        self.assertEqual(interaction.edited, [])
        self.assertEqual(len(msg.edits), 0)

    async def test_detail_tap_logs_one_line(self):
        # Finding 4 (18 Nov 2026): taps logged NOTHING, so a
        # "buttons not working" report was invisible in the
        # journal. Every tap now logs exactly one line with
        # button, game, channel, user and outcome.
        view = self._view()
        interaction = self._interaction("watch:detail")
        with self._mock_search(self.detail), _capture_watch_log() as recs:
            await view.children[0].callback(interaction)
        tap_lines = [r.getMessage() for r in recs
                     if r.name == "watch" and "tap Detail" in r.getMessage()]
        self.assertEqual(len(tap_lines), 1)
        line = tap_lines[0]
        self.assertIn("game=771747666", line)
        self.assertIn("channel=777", line)
        self.assertIn("user=42", line)
        self.assertIn("sent new message", line)

    async def test_maintenance_blocked_tap_logs_blocked(self):
        # A maintenance-blocked tap never reaches a handler,
        # so interaction_check logs its only journal line.
        view = self._view()
        interaction = self._interaction("watch:detail")
        with patch.object(wmod.maintenance, "view_blocked",
                          new=AsyncMock(return_value=True)), \
                _capture_watch_log() as recs:
            blocked = await view.interaction_check(interaction)
        self.assertFalse(blocked)
        tap_lines = [r.getMessage() for r in recs
                     if r.name == "watch"
                     and "blocked: maintenance" in r.getMessage()]
        self.assertEqual(len(tap_lines), 1)
        self.assertIn("771747666", tap_lines[0])
        self.assertIn("channel=777", tap_lines[0])

    async def test_tap_on_stale_message_still_sends_new_message(self):
        # Findings 3 + 5 (18 Nov 2026): tapping a STALE
        # message — an old panel, a goal alert — used to
        # silently switch the REAL panel elsewhere, with no
        # feedback on the message tapped, and could adopt the
        # stale message as the panel. Now the tap answers
        # visibly on the spot and never touches any panel.
        msg, body = self._seed_panel(kind="detail")
        stale = wmod.render_panel(self.detail, "12:00")
        view = self._view("771747666")
        interaction = self._interaction("watch:detail:771747666",
                                        content=stale)
        with self._mock_search(self.detail):
            await view.children[0].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertEqual(interaction.edited, [])
        self.assertEqual(len(msg.edits), 0)
        self.assertEqual(
            wmod._watches[(777, "771747666")]["panel_kind"], "detail")

    async def test_refresh_tap_sends_current_view_as_new_message(self):
        msg, body = self._seed_panel(kind="detail")
        view = self._view()
        interaction = self._interaction("panel:refresh")
        with self._mock_search(self.detail):
            await view.children[2].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Game ID: 771747666",
                      interaction.followup.sent[0])
        self.assertEqual(interaction.edited, [])
        self.assertEqual(len(msg.edits), 0)

    async def test_timeline_tap_sends_new_message(self):
        view = self._view()
        interaction = self._interaction("panel:timeline")
        events = [{"minute_int": 23, "minute": "23", "icon": "⚽",
                   "detail": "Goal", "player": "S. Mane",
                   "team": "Ivory Coast"}]
        with self._mock_search(self.detail), \
                patch.object(prov, "onex_events",
                             new=AsyncMock(return_value=events)):
            await view.children[3].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Goal", interaction.followup.sent[0])

    async def test_follow_tap_sends_new_message_and_starts_watch(self):
        view = self._view()
        interaction = self._interaction("panel:follow")
        with self._mock_search(self.detail):
            await view.children[6].callback(interaction)
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Watching", interaction.followup.sent[0])

    async def test_unfollow_tap_sends_new_message_panel_untouched(self):
        msg, body = self._seed_panel()
        view = self._view()
        interaction = self._interaction("panel:unfollow")
        await view.children[7].callback(interaction)
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertFalse(wmod._watches[(777, "771747666")]["notify"])
        self.assertFalse(msg.deleted)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Notifications off", interaction.followup.sent[0])


class TestTapRepairsPanel(unittest.IsolatedAsyncioTestCase):
    """18 Nov 2026 — "Advanced stops the refresh": tapping
    Advanced sent a one-off analytics snapshot that never
    ticks, while the live panel kept its OLD view, so the user
    stared at a frozen card. The aadf03dd repair (tap switches
    the panel's stored view) was not enough — proven live at
    22:57 NPT, the panel DID switch view and kept ticking at
    45–52 edits/min, but on its OLD message, while the user
    kept looking at the tap's new message. So the panel MOVES:
    the message a view tap sends BECOMES the live panel — the
    tick edits THAT message from then on, in the tapped view,
    and the old panel message is left frozen, never edited
    again. A second tap moves the panel again. The tap itself
    never edits any message in place."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp12"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        self._orig_cooldown = dict(wmod._refresh_cooldown)
        wmod._watches = {}
        wmod._loaded = True
        wmod._refresh_cooldown = {}
        self.games1x2 = _load("games1x2.json")
        self.detail = prov._onex_detail_from_raw(self.games1x2[0])
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        wmod._refresh_cooldown = self._orig_cooldown
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _mock_search(self, detail):
        return patch.object(
            prov, "onex_search",
            new=AsyncMock(return_value={"games": [], "detail": detail}))

    def _seed_panel(self, kind="compact"):
        snap = wmod.snapshot_from_detail(
            {"home": "Republic of the Gambia", "away": "Ivory Coast",
             "home_score": 0, "away_score": 1, "status": "2H", "minute": 67,
             "clock": "67'", "stats": [], "cricket": None})
        snap["_home"], snap["_away"] = ("Republic of the Gambia",
                                        "Ivory Coast")
        body = wmod.render_panel(self.detail, "67:00")
        msg = FakeMessage(2001, body)
        self.channel.messages[2001] = msg
        wmod._watches[(777, "771747666")] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": "simple",
            "fmt": "", "snapshot": snap, "missing": 0,
            "missing_since": 0.0, "notify": True, "panel_kind": kind,
            "panel_message_id": 2001, "panel_body": body,
            "panel_base_body": body, "panel_static": {},
            "panel_momentum": None, "panel_momentum_ts": 0.0,
            "last_panel_edit": 0.0, "panel_timer_sec": None,
            "panel_timer_ts": 0.0,
        }
        return msg, body

    def _interaction(self, custom_id, content=None):
        if content is None:
            content = wmod.render_detail(self.detail,
                                         wmod.DETAIL_ADVANCED)
        interaction = FakeInteraction(
            message=SimpleNamespace(content=content),
            data={"custom_id": custom_id})
        interaction.client = self.bot
        return interaction

    def _register_followup_panel(self, interaction):
        """The followup message the tap just sent, registered
        in the fake channel so the tick's worker can fetch and
        edit it — in production the message exists in the
        channel by construction. FakeFollowup ids start at
        4001 and increment per send on that interaction."""
        new_id = 4000 + len(interaction.followup.sent)
        msg = FakeMessage(new_id, interaction.followup.sent[-1])
        self.channel.messages[new_id] = msg
        return msg

    async def _goal_tick(self):
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        await _drain_panel_workers()

    async def test_advanced_tap_switches_panel_and_it_keeps_ticking(self):
        msg, body = self._seed_panel(kind="compact")
        view = wmod.WatchAlertView()
        interaction = self._interaction("panel:advanced")
        with self._mock_search(self.detail):
            await view.children[5].callback(interaction)
        # The snapshot arrives as a new visible message...
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Match statistics", interaction.followup.sent[0])
        # ...the tap itself never edits the OLD panel message...
        self.assertEqual(msg.content, body)
        self.assertEqual(len(msg.edits), 0)
        # ...but the panel MOVES to the new message, in the
        # analytics view, and its worker is re-asserted.
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_kind"], "analytics")
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertIn((777, "771747666"), wmod._panel_workers)
        # ...and the NEXT tick refreshes THAT message, not the
        # old one: a goal lands on the new panel, in the
        # analytics view, instead of the panel going quiet.
        new_msg = self._register_followup_panel(interaction)
        await self._goal_tick()
        self.assertGreaterEqual(len(new_msg.edits), 1)
        self.assertIn("1 – 1", new_msg.content)
        self.assertIn("Match statistics", new_msg.content)
        self.assertEqual(len(msg.edits), 0)
        self.assertEqual(msg.content, body)
        self.assertIn((777, "771747666"), wmod._watches)

    async def test_second_tap_moves_panel_again(self):
        # Tap Advanced, then tap Live on the new panel: the
        # panel moves a second time, to the newest message,
        # and the tick follows it there.
        msg, body = self._seed_panel(kind="compact")
        view = wmod.WatchAlertView()
        interaction = self._interaction("panel:advanced")
        with self._mock_search(self.detail):
            await view.children[5].callback(interaction)
        first_msg = self._register_followup_panel(interaction)
        self.assertEqual(
            wmod._watches[(777, "771747666")]["panel_message_id"], 4001)
        with self._mock_search(self.detail):
            await view.children[1].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 2)
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_kind"], "compact")
        self.assertEqual(watch["panel_message_id"], 4002)
        second_msg = self._register_followup_panel(interaction)
        await self._goal_tick()
        self.assertGreaterEqual(len(second_msg.edits), 1)
        self.assertIn("1 – 1", second_msg.content)
        self.assertEqual(len(first_msg.edits), 0)
        self.assertEqual(len(msg.edits), 0)

    async def test_advanced_tap_on_stale_message_repairs_real_panel(self):
        # The tap lands on an old alert, not the live panel:
        # the panel still moves to the tap's new message and
        # switches to analytics — the stale message is never
        # adopted, and the old panel is never edited again.
        msg, body = self._seed_panel(kind="compact")
        alert = ("⚽ **GOAL** — Republic of the Gambia 1–1 Ivory Coast "
                 "(67')\nGame ID: 771747666")
        view = wmod.WatchAlertView()
        interaction = self._interaction("panel:advanced", content=alert)
        with self._mock_search(self.detail):
            await view.children[5].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_kind"], "analytics")
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertEqual(len(msg.edits), 0)
        new_msg = self._register_followup_panel(interaction)
        await self._goal_tick()
        self.assertGreaterEqual(len(new_msg.edits), 1)
        self.assertIn("Match statistics", new_msg.content)
        self.assertIn("1 – 1", new_msg.content)
        self.assertEqual(len(msg.edits), 0)

    async def test_detail_tap_switches_panel_view_simple_too(self):
        msg, body = self._seed_panel(kind="compact")
        view = wmod.WatchAlertView()
        interaction = self._interaction("watch:detail")
        with self._mock_search(self.detail):
            await view.children[0].callback(interaction)
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_kind"], "detail")
        self.assertEqual(watch["detail"], wmod.DETAIL_ADVANCED)
        # The panel moved to the detail card the tap sent.
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertIn("Match stats", interaction.followup.sent[0])
        new_msg = self._register_followup_panel(interaction)
        await self._goal_tick()
        self.assertGreaterEqual(len(new_msg.edits), 1)
        self.assertIn("1 – 1", new_msg.content)
        self.assertEqual(len(msg.edits), 0)

    async def test_refresh_tap_reasserts_worker_without_switching_view(self):
        msg, body = self._seed_panel(kind="detail")
        view = wmod.WatchAlertView()
        interaction = self._interaction("panel:refresh")
        with self._mock_search(self.detail):
            await view.children[2].callback(interaction)
        # Refresh switches nothing — the panel keeps its view —
        # but it still MOVES to the fresh message, and the
        # worker is re-asserted, so a stalled panel gets its
        # next edit on the message in front of the user.
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_kind"], "detail")
        self.assertEqual(watch["panel_message_id"], 4001)
        self.assertIn((777, "771747666"), wmod._panel_workers)
        new_msg = self._register_followup_panel(interaction)
        await self._goal_tick()
        self.assertGreaterEqual(len(new_msg.edits), 1)
        self.assertIn("1 – 1", new_msg.content)
        self.assertEqual(len(msg.edits), 0)

    async def test_tap_with_no_live_panel_just_sends_snapshot(self):
        # An /ask detail reply with no watch behind it: the tap
        # sends its snapshot and nothing else — no watch is
        # invented, no crash.
        view = wmod.WatchAlertView()
        interaction = self._interaction("panel:advanced")
        with self._mock_search(self.detail):
            await view.children[5].callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertNotIn((777, "771747666"), wmod._watches)

    async def test_follow_tap_does_not_freeze_panel(self):
        # The other taps never touch the panel at all: after a
        # Follow tap the panel is exactly as it was, and the
        # next tick still refreshes it.
        msg, body = self._seed_panel(kind="compact")
        view = wmod.WatchAlertView()
        interaction = self._interaction("panel:follow")
        with self._mock_search(self.detail):
            await view.children[6].callback(interaction)
        watch = wmod._watches[(777, "771747666")]
        self.assertEqual(watch["panel_kind"], "compact")
        self.assertEqual(watch["panel_message_id"], 2001)
        self.assertEqual(len(msg.edits), 0)
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        await _drain_panel_workers()
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("1 – 1", msg.content)


class TestRemoveWatchButton(unittest.IsolatedAsyncioTestCase):
    """The ❌ Remove buttons on a watching-list message: full
    remove like /unwatch, persistent after a restart via the
    DynamicItem template."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp10"
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
        await _cancel_panel_workers()
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    async def _seed_full(self, idx, game_id, mid):
        detail = prov._onex_detail_from_raw(self.games1x2[idx])
        snap = wmod.snapshot_from_detail(detail)
        snap["_home"], snap["_away"] = detail["home"], detail["away"]
        snap["status"] = "2H"
        await db.add_watch(777, game_id, detail["home"], detail["away"],
                           detail.get("league", ""), "simple", "", snap,
                           notify=True, panel_kind="compact")
        body = wmod.render_panel(detail, "67:00")
        msg = FakeMessage(mid, body)
        self.channel.messages[mid] = msg
        wmod._watches[(777, game_id)] = {
            "home": detail["home"], "away": detail["away"],
            "league": detail.get("league", ""), "detail": "simple",
            "fmt": str(detail.get("format") or ""), "snapshot": snap,
            "missing": 0, "missing_since": 0.0, "notify": True,
            "panel_kind": "compact", "panel_message_id": mid,
            "panel_body": body, "panel_base_body": body,
            "panel_static": {}, "panel_momentum": None,
            "panel_momentum_ts": 0.0, "last_panel_edit": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }
        return detail, msg

    def _remove_button(self, view, game_id):
        return next(b for b in view.children
                    if b.custom_id == f"panel:remove:{game_id}")

    async def _list_view_and_message(self):
        rows = await wmod.list_channel_watches(777, include_view_only=True)
        view = wmod.WatchingListView(rows)
        text = wmod._watching_text(rows)
        msg = FakeMessage(4001, text, view=view)
        self.channel.messages[4001] = msg
        return view, msg, text

    async def test_remove_tap_fully_removes_watch(self):
        await self._seed_full(0, "771747666", 2001)
        await self._seed_full(2, "771750037", 2002)
        view, msg, _text = await self._list_view_and_message()
        btn = self._remove_button(view, "771747666")
        interaction = FakeInteraction(
            channel_id=777, message=msg,
            data={"custom_id": "panel:remove:771747666"})
        interaction.client = self.bot
        await btn.callback(interaction)
        # Panel deleted, watch gone from memory and db.
        self.assertTrue(self.channel.messages[2001].deleted)
        self.assertNotIn((777, "771747666"), wmod._watches)
        rows = await wmod.list_channel_watches(777,
                                               include_view_only=True)
        self.assertEqual([r["game_id"] for r in rows], ["771750037"])
        # The list message was edited to the fresh list...
        self.assertEqual(len(interaction.edited), 1)
        new_text, new_view = interaction.edited[0]
        self.assertNotIn("Republic of the Gambia", new_text)
        self.assertIsInstance(new_view, wmod.WatchingListView)
        self.assertEqual([b.custom_id for b in new_view.children],
                         ["panel:remove:771750037"])
        # ...and a short new channel message confirms the removal.
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("❌ Removed", interaction.followup.sent[0])
        self.assertIn("Republic of the Gambia",
                      interaction.followup.sent[0])
        # The other game is untouched: still watched, panel alive.
        self.assertIn((777, "771750037"), wmod._watches)
        self.assertFalse(self.channel.messages[2002].deleted)

    async def test_remove_tap_logs_one_line(self):
        # Taps used to log nothing (18 Nov 2026): a Remove tap
        # logs its outcome like every other tap.
        await self._seed_full(0, "771747666", 2001)
        view, msg, _text = await self._list_view_and_message()
        btn = self._remove_button(view, "771747666")
        interaction = FakeInteraction(
            channel_id=777, message=msg,
            data={"custom_id": "panel:remove:771747666"})
        interaction.client = self.bot
        with _capture_watch_log() as recs:
            await btn.callback(interaction)
        tap_lines = [r.getMessage() for r in recs
                     if r.name == "watch" and "tap Remove" in r.getMessage()]
        self.assertEqual(len(tap_lines), 1)
        self.assertIn("game=771747666", tap_lines[0])
        self.assertIn("watch removed", tap_lines[0])

    async def test_remove_tap_on_already_removed_game_answers_plainly(self):
        await self._seed_full(2, "771750037", 2002)
        view, msg, _text = await self._list_view_and_message()
        # A Remove button for a game that is already gone (an
        # old list message, tapped twice): no crash, plain words.
        btn = wmod.RemoveWatchButton(
            "771747666", "❌ Remove Republic of… v Ivory Coast")
        interaction = FakeInteraction(
            channel_id=777, message=msg,
            data={"custom_id": "panel:remove:771747666"})
        interaction.client = self.bot
        await btn.callback(interaction)
        self.assertEqual(interaction.followup.sent,
                         ["That game is not watched in this channel "
                          "any more."])
        self.assertEqual(len(interaction.edited), 1)
        self.assertIn((777, "771750037"), wmod._watches)

    async def test_remove_button_survives_restart(self):
        # Simulate a restart: throw the list view away and
        # rebuild the button from its custom_id alone, the way
        # discord.py's DynamicItem dispatch does — no in-memory
        # view state, no registry beyond what load_watches
        # rebuilds from db (here: _watches, as seeded).
        await self._seed_full(0, "771747666", 2001)
        await self._seed_full(2, "771750037", 2002)
        view, msg, _text = await self._list_view_and_message()
        btn = self._remove_button(view, "771747666")
        template = wmod.RemoveWatchButton.__discord_ui_compiled_template__
        self.assertIsNotNone(template.fullmatch(btn.custom_id))
        self.assertIsNone(template.fullmatch("panel:remove:not-a-game"))
        rebuilt = await wmod.RemoveWatchButton.from_custom_id(
            None, btn.item, template.fullmatch(btn.custom_id))
        self.assertEqual(rebuilt.game_id, "771747666")
        self.assertEqual(rebuilt.custom_id, "panel:remove:771747666")
        interaction = FakeInteraction(
            channel_id=777, message=msg,
            data={"custom_id": "panel:remove:771747666"})
        interaction.client = self.bot
        await rebuilt.callback(interaction)
        self.assertNotIn((777, "771747666"), wmod._watches)
        self.assertTrue(self.channel.messages[2001].deleted)
        self.assertIn("❌ Removed", interaction.followup.sent[0])

    async def test_remove_blocked_in_maintenance(self):
        import maintenance
        await self._seed_full(0, "771747666", 2001)
        view, msg, _text = await self._list_view_and_message()
        btn = self._remove_button(view, "771747666")
        await maintenance.set_on(True)
        try:
            interaction = FakeInteraction(
                channel_id=777, message=msg,
                data={"custom_id": "panel:remove:771747666"})
            self.assertFalse(await btn.interaction_check(interaction))
            self.assertIn("maintenance",
                          interaction.response.sent[0].lower())
            self.assertIn((777, "771747666"), wmod._watches)
        finally:
            await maintenance.set_on(False)

    async def test_list_view_caps_buttons_at_25(self):
        # A discord.ui.View needs a running loop to build, so
        # this one is async like the rest of the class.
        rows = [{"game_id": str(1000 + i), "home": f"Home {i}",
                 "away": f"Away {i}"} for i in range(30)]
        view = wmod.WatchingListView(rows)
        self.assertEqual(len(view.children), 25)


class TestWatchGameButton(unittest.IsolatedAsyncioTestCase):
    """The ▶ Watch buttons on /ask replies (18 Nov 2026, the
    user's ask: "add button to like watch when /ask show
    game"): tapping one starts watching that game in the
    channel, exactly like /watch, and confirms with a visible
    new message. The buttons are DynamicItems, so they survive
    a restart like ❌ Remove does."""

    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".watch-test-tmp11"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True
        self.games1x2 = _load("games1x2.json")
        self.detail = prov._onex_detail_from_raw(self.games1x2[0])
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await _cancel_panel_workers()
        await db.close()
        db._DB = self._orig_db
        wmod._watches = self._orig_watches
        wmod._loaded = self._orig_loaded
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _mock_search(self, detail):
        return patch.object(
            prov, "onex_search",
            new=AsyncMock(return_value={"games": [], "detail": detail}))

    def _interaction(self, game_id):
        return FakeInteraction(
            channel_id=777,
            data={"custom_id": f"panel:watch:{game_id}"})

    def _seed_watch(self, game_id="771747666", notify=True):
        wmod._watches[(777, game_id)] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": "simple",
            "fmt": "", "snapshot": {}, "missing": 0,
            "missing_since": 0.0, "notify": notify,
            "panel_kind": "compact", "panel_message_id": 2001,
            "panel_body": "", "panel_base_body": "",
            "panel_static": {}, "panel_momentum": None,
            "panel_momentum_ts": 0.0, "last_panel_edit": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }

    async def test_watch_tap_starts_watch_and_confirms_visibly(self):
        btn = wmod.WatchGameButton("771747666", "▶ Watch")
        interaction = self._interaction("771747666")
        with self._mock_search(self.detail):
            await btn.callback(interaction)
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertTrue(wmod._watches[(777, "771747666")]["notify"])
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertEqual(interaction.followup.ephemeral_flags, [False])
        self.assertIn("Watching", interaction.followup.sent[0])
        self.assertIn("Republic of the Gambia",
                      interaction.followup.sent[0])

    async def test_watch_tap_on_already_watched_game_says_so_plainly(self):
        self._seed_watch()
        btn = wmod.WatchGameButton("771747666", "▶ Watch")
        interaction = self._interaction("771747666")
        await btn.callback(interaction)
        # No second watch, no state change — one plain answer.
        self.assertEqual(len(wmod._watches), 1)
        self.assertEqual(interaction.followup.sent,
                         ["That game is already watched in this channel."])
        self.assertEqual(interaction.followup.ephemeral_flags, [False])

    async def test_watch_tap_on_panel_only_watch_turns_alerts_on(self):
        # A /game panel watches with notify off; /watch would
        # switch alerts on, so the button does the same instead
        # of saying "already watched" while staying silent.
        self._seed_watch(notify=False)
        btn = wmod.WatchGameButton("771747666", "▶ Watch")
        interaction = self._interaction("771747666")
        await btn.callback(interaction)
        self.assertTrue(wmod._watches[(777, "771747666")]["notify"])
        self.assertIn("Now following", interaction.followup.sent[0])

    async def test_watch_tap_on_no_longer_live_game_answers_plainly(self):
        btn = wmod.WatchGameButton("771747666", "▶ Watch")
        interaction = self._interaction("771747666")
        with self._mock_search(None):
            await btn.callback(interaction)
        self.assertNotIn((777, "771747666"), wmod._watches)
        self.assertEqual(interaction.followup.sent,
                         ["That game is no longer live."])
        self.assertEqual(interaction.followup.ephemeral_flags, [True])

    async def test_watch_tap_logs_one_line(self):
        btn = wmod.WatchGameButton("771747666", "▶ Watch")
        interaction = self._interaction("771747666")
        with self._mock_search(self.detail), \
                _capture_watch_log() as recs:
            await btn.callback(interaction)
        tap_lines = [r.getMessage() for r in recs
                     if r.name == "watch" and "tap Watch" in r.getMessage()]
        self.assertEqual(len(tap_lines), 1)
        self.assertIn("game=771747666", tap_lines[0])
        self.assertIn("watch started", tap_lines[0])

    async def test_watch_button_survives_restart(self):
        # Simulate a restart: rebuild the button from its
        # custom_id alone, the way discord.py's DynamicItem
        # dispatch does — no in-memory view state.
        btn = wmod.WatchGameButton("771747666", "▶ Watch")
        template = wmod.WatchGameButton.__discord_ui_compiled_template__
        self.assertIsNotNone(template.fullmatch(btn.custom_id))
        self.assertIsNone(template.fullmatch("panel:watch:not-a-game"))
        rebuilt = await wmod.WatchGameButton.from_custom_id(
            None, btn.item, template.fullmatch(btn.custom_id))
        self.assertEqual(rebuilt.game_id, "771747666")
        self.assertEqual(rebuilt.custom_id, "panel:watch:771747666")
        interaction = self._interaction("771747666")
        with self._mock_search(self.detail):
            await rebuilt.callback(interaction)
        self.assertIn((777, "771747666"), wmod._watches)
        self.assertIn("Watching", interaction.followup.sent[0])

    async def test_watch_blocked_in_maintenance(self):
        btn = wmod.WatchGameButton("771747666", "▶ Watch")
        interaction = self._interaction("771747666")
        with patch.object(wmod.maintenance, "view_blocked",
                          new=AsyncMock(return_value=True)):
            self.assertFalse(await btn.interaction_check(interaction))
        self.assertNotIn((777, "771747666"), wmod._watches)

    async def test_ask_watch_view_caps_buttons_at_five(self):
        games = [{"onex_id": 771747666 + i, "home": f"Home {i}",
                  "away": f"Away {i}"} for i in range(7)]
        view = wmod.AskWatchView(games)
        self.assertEqual(len(view.children), 5)
        self.assertEqual(
            [b.custom_id for b in view.children],
            [f"panel:watch:{771747666 + i}" for i in range(5)])
        self.assertTrue(all(b.item.label.startswith("▶ Watch ")
                            for b in view.children))
        self.assertIn("Home 0", view.children[0].item.label)

    async def test_ask_watch_view_skips_games_without_an_id(self):
        games = [{"home": "No ID", "away": "Team"},
                 {"onex_id": 771747666, "home": "Republic of the Gambia",
                  "away": "Ivory Coast"}]
        view = wmod.AskWatchView(games)
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].custom_id,
                         "panel:watch:771747666")

    def test_ask_game_id_reads_both_id_shapes(self):
        self.assertEqual(wmod._ask_game_id({"onex_id": 771747666}),
                         "771747666")
        self.assertEqual(wmod._ask_game_id({"id": "onex_771747666"}),
                         "771747666")
        self.assertIsNone(wmod._ask_game_id({"home": "No ID"}))
        self.assertIsNone(wmod._ask_game_id({}))

    async def test_detail_view_with_watch_button_has_ten_buttons(self):
        view = wmod.WatchAlertView("771747666", watch_button=True)
        self.assertEqual(len(view.children), 10)
        watch_btn = view.children[-1]
        self.assertEqual(watch_btn.custom_id, "panel:watch:771747666")
        self.assertEqual(watch_btn.item.label, "▶ Watch")
        self.assertIsInstance(watch_btn, wmod.WatchGameButton)

    async def test_detail_view_without_watch_button_unchanged(self):
        # Live panels keep exactly the 9 buttons they had —
        # the ▶ Watch button is /ask-only.
        view = wmod.WatchAlertView("771747666")
        self.assertEqual(len(view.children), 9)
        self.assertFalse(any(b.custom_id.startswith("panel:watch:")
                             for b in view.children))


if __name__ == "__main__":
    unittest.main()
