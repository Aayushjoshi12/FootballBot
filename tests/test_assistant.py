"""AI Match Assistant (/ask, Feature 3).

No Discord token, no network, no real Gemini — hc.get is stubbed
with the fixtures in tests/fixtures/watch/ (same shapes as
tests/test_watch.py), Gemini is a fake model object, and the
watch starter is mocked where a follow must NOT happen.

Run:  python3 -m pytest tests/test_assistant.py -q
from the footballbot directory.
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import assistant as amod
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
        self.calls = 0

    def generate_content(self, prompt: str):
        self.calls += 1
        response = Mock()
        response.text = self.text
        return response


class NeverCallModel:
    """A model that fails loudly if anything calls it — for the
    deterministic pre-check tests."""

    def __init__(self):
        self.calls = 0

    def generate_content(self, prompt: str):
        self.calls += 1
        raise AssertionError("model must not be called for a pre-check")


class StubHttp:
    """Fake hc.get routing by URL (compact version of the
    test_watch.py stub)."""

    def __init__(self, test):
        self.test = test

    async def get(self, url, headers, params=None, cache_key="",
                  provider="x", refresh_fn=None):
        if "games1x2" in url:
            return self.test.games1x2
        if "topGamesByStat" in url:
            return self.test.top
        if "gameInfo" in url:
            gid = str((params or {}).get("gameId"))
            return self.test.gameinfo.get(gid, {})
        if "leftMenuSports" in url:
            return []
        raise AssertionError(f"unexpected URL in test: {url}")


class FakeResponse:
    async def defer(self, ephemeral=False, thinking=False, **kwargs):
        return None

    async def send_message(self, content=None, ephemeral=False,
                           view=None, **kwargs):
        return None


class FakeFollowup:
    def __init__(self):
        self.sent: list[str] = []
        self.sent_views: list = []

    async def send(self, content=None, ephemeral=False, view=None, **kwargs):
        self.sent.append(content)
        self.sent_views.append(view)


class FakeInteraction:
    """Stand-in for an /ask slash-command interaction."""

    def __init__(self, channel_id=777):
        self.channel_id = channel_id
        self.channel = SimpleNamespace(id=channel_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.user = SimpleNamespace(id=42)


CHANNELS = [{"id": 111, "name": "general"},
            {"id": 222, "name": "test"}]


def _live_games(games1x2):
    """Normalized live matches from the fixture list, as
    handle_ask builds them from onex_live_raw()."""
    return [m for g in games1x2
            if (m := prov._onex_detail_from_raw(g)) is not None]


def _parsed(intent, **kw):
    d = {"intent": intent, "game_id": None, "team": None, "events": None,
         "detail": None, "channel": None, "note": "", "pick": None,
         "mass": False, "error": ""}
    d.update(kw)
    return d


class TestParsePrechecks(unittest.IsolatedAsyncioTestCase):
    async def test_help_precheck_without_model_call(self):
        model = NeverCallModel()
        out = await amod.parse_intent("help", model, CHANNELS)
        self.assertEqual(out["intent"], "HELP")
        self.assertEqual(model.calls, 0)
        out = await amod.parse_intent("what can you do?", None, CHANNELS)
        self.assertEqual(out["intent"], "HELP")

    async def test_live_matches_precheck(self):
        model = NeverCallModel()
        out = await amod.parse_intent("what games are live", model, CHANNELS)
        self.assertEqual(out["intent"], "LIVE_MATCHES")
        self.assertEqual(model.calls, 0)
        out = await amod.parse_intent("show live matches", None, CHANNELS)
        self.assertEqual(out["intent"], "LIVE_MATCHES")

    async def test_highest_scoring_precheck(self):
        model = NeverCallModel()
        out = await amod.parse_intent(
            "show me the highest scoring game live", model, CHANNELS)
        self.assertEqual(out["intent"], "MATCH_DETAILS")
        self.assertEqual(out["pick"], "highest_scoring")
        self.assertEqual(model.calls, 0)

    async def test_mass_follow_precheck(self):
        out = await amod.parse_intent("follow every live match today",
                                      None, CHANNELS)
        self.assertEqual(out["intent"], "FOLLOW_MATCH")
        self.assertTrue(out["mass"])

    async def test_empty_text_never_raises(self):
        out = await amod.parse_intent("", None, CHANNELS)
        self.assertIn("error", out)
        self.assertTrue(out["error"])

    async def test_gemini_json_parsed(self):
        model = FakeModel('{"intent": "MATCH_DETAILS", "game_id": null, '
                          '"team": "Gambia", "events": null, '
                          '"detail": "advanced", "channel": null, '
                          '"note": "Gambia detail"}')
        out = await amod.parse_intent("show me the Gambia game in detail",
                                      model, CHANNELS)
        self.assertEqual(out["intent"], "MATCH_DETAILS")
        self.assertEqual(out["team"], "Gambia")
        self.assertEqual(out["detail"], "advanced")
        self.assertEqual(out["error"], "")

    async def test_gemini_garbage_returns_error_not_raise(self):
        model = FakeModel("I think the Gambia game looks good!")
        out = await amod.parse_intent("gambia", model, CHANNELS)
        self.assertTrue(out["error"])
        self.assertIsNone(out["intent"])

    async def test_no_model_no_precheck_returns_error(self):
        out = await amod.parse_intent("follow the Gambia game",
                                      None, CHANNELS)
        self.assertTrue(out["error"])


class TestValidate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_get = prov.hc.get
        self.games1x2 = _load("games1x2.json")
        self.top = _load("top.json")
        self.gameinfo = {
            "771747666": _load("gameinfo_771747666.json"),
            "771747795": _load("gameinfo_alias.json"),
        }
        prov.hc.get = StubHttp(self).get
        self.live = _live_games(self.games1x2)

    def tearDown(self):
        prov.hc.get = self._orig_get

    async def test_invented_game_id_rejected(self):
        out = await amod.validate_intent(
            _parsed("MATCH_DETAILS", game_id="999999999"), self.live,
            CHANNELS)
        self.assertFalse(out["ok"])
        self.assertIn("999999999", out["error"])

    async def test_alias_game_id_resolves_to_live_game(self):
        # 771747795 is an alias for live game 771747706 (Sierra Leone).
        out = await amod.validate_intent(
            _parsed("MATCH_DETAILS", game_id="771747795"), self.live,
            CHANNELS)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["game_id"], "771747706")

    async def test_unknown_team_rejected(self):
        out = await amod.validate_intent(
            _parsed("MATCH_DETAILS", team="Zzqxj No Such Team"), self.live,
            CHANNELS)
        self.assertFalse(out["ok"])
        self.assertIn("Zzqxj No Such Team", out["error"])

    async def test_unknown_channel_rejected(self):
        out = await amod.validate_intent(
            _parsed("FOLLOW_MATCH", game_id="771747666", channel="random"),
            self.live, CHANNELS)
        self.assertFalse(out["ok"])
        self.assertIn("random", out["error"])

    async def test_known_channel_resolves(self):
        out = await amod.validate_intent(
            _parsed("FOLLOW_MATCH", game_id="771747666", channel="general"),
            self.live, CHANNELS)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["channel"]["id"], 111)

    async def test_bad_event_kind_rejected(self):
        out = await amod.validate_intent(
            _parsed("SET_ALERTS", game_id="771747666",
                    events=["goal", "var"]),
            self.live, CHANNELS)
        self.assertFalse(out["ok"])
        self.assertIn("var", out["error"])

    async def test_second_yellow_is_not_a_kind(self):
        # Correction: the engine has no "second_yellow" kind — a
        # second yellow surfaces as the red-card count going up.
        out = await amod.validate_intent(
            _parsed("SET_ALERTS", game_id="771747666",
                    events=["second_yellow"]),
            self.live, CHANNELS)
        self.assertFalse(out["ok"])

    async def test_real_event_kinds_accepted(self):
        for kind in wmod.EVENT_KINDS:
            out = await amod.validate_intent(
                _parsed("SET_ALERTS", game_id="771747666", events=[kind]),
                self.live, CHANNELS)
            self.assertTrue(out["ok"], kind)

    async def test_valid_follow_match_passes(self):
        out = await amod.validate_intent(
            _parsed("FOLLOW_MATCH", game_id="771747666",
                    detail="simple"),
            self.live, CHANNELS)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["intent"], "FOLLOW_MATCH")
        self.assertEqual(out["game_id"], "771747666")
        self.assertEqual(out["detail"], "simple")

    async def test_team_name_resolves_to_live_game(self):
        out = await amod.validate_intent(
            _parsed("MATCH_DETAILS", team="Gambia"), self.live, CHANNELS)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["game_id"], "771747666")

    async def test_highest_scoring_pick_resolves_to_football_game(self):
        out = await amod.validate_intent(
            _parsed("MATCH_DETAILS", pick="highest_scoring"), self.live,
            CHANNELS)
        self.assertTrue(out["ok"], out)
        # Football only (cricket runs are not goals); both live
        # football games are 0–1, so the first listed wins.
        self.assertEqual(out["game_id"], "771747666")

    async def test_barca_team_allowed_for_fixtures(self):
        out = await amod.validate_intent(
            _parsed("TEAM_FIXTURES", team="FC Barcelona"), self.live,
            CHANNELS)
        self.assertTrue(out["ok"], out)


class TestHandleAsk(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_get = prov.hc.get
        self.games1x2 = _load("games1x2.json")
        self.top = _load("top.json")
        self.gameinfo = {
            "771747666": _load("gameinfo_771747666.json"),
            "771747795": _load("gameinfo_alias.json"),
        }
        prov.hc.get = StubHttp(self).get
        prov._onex_reset_caches()
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".assistant-test-tmp"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        db._DB = self._orig_db
        prov._onex_reset_caches()
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def tearDown(self):
        prov.hc.get = self._orig_get

    async def test_follow_every_live_match_lists_and_starts_no_watch(self):
        interaction = FakeInteraction()
        with patch.object(wmod, "start_watch",
                          new=AsyncMock()) as mock_start:
            await amod.handle_ask(interaction,
                                  "follow every live match today",
                                  None, CHANNELS)
        mock_start.assert_not_awaited()
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("pick one", text)
        self.assertIn("Gambia", text)
        self.assertIn("Sierra Leone", text)

    async def test_match_details_renders_from_stubbed_search(self):
        model = FakeModel('{"intent": "MATCH_DETAILS", "game_id": null, '
                          '"team": "Republic of the Gambia", '
                          '"events": null, "detail": "simple", '
                          '"channel": null, "note": "Gambia"}')
        interaction = FakeInteraction()
        await amod.handle_ask(interaction,
                              "show me the Gambia game", model, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Republic of the Gambia 0 – 1 Ivory Coast", text)
        self.assertIn("Game ID: 771747666", text)
        view = interaction.followup.sent_views[0]
        self.assertIsNotNone(view)
        self.assertEqual([b.label for b in view.children][:2],
                         ["📊 Detail", "🔄 Live"])

    async def test_team_fixtures_non_barca_says_unavailable(self):
        model = FakeModel('{"intent": "TEAM_FIXTURES", "game_id": null, '
                          '"team": "Real Madrid", "events": null, '
                          '"detail": null, "channel": null, '
                          '"note": "Madrid fixtures"}')
        interaction = FakeInteraction()
        with patch.object(prov, "free_fixtures_for_day",
                          new=AsyncMock(
                              side_effect=AssertionError(
                                  "must not fetch fixtures"))) as mock_fix:
            await amod.handle_ask(interaction,
                                  "when do Real Madrid play next?",
                                  model, CHANNELS)
        mock_fix.assert_not_awaited()
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Real Madrid", text)
        self.assertIn("don't have fixtures", text)
        # Nothing invented: no kickoff time or opponent line.
        self.assertNotIn("Kickoff", text)

    async def test_help_lists_capabilities(self):
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "help", None, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Follow a game", text)
        self.assertIn("Barcelona", text)

    async def test_live_matches_lists_live_games(self):
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "what games are live",
                              None, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Gambia", text)
        self.assertIn("771747666", text)

    async def test_valid_follow_match_starts_watch(self):
        model = FakeModel('{"intent": "FOLLOW_MATCH", "game_id": null, '
                          '"team": "Gambia", "events": null, '
                          '"detail": "simple", "channel": null, '
                          '"note": "follow Gambia"}')
        started = {"game_id": "771747666",
                   "home": "Republic of the Gambia",
                   "away": "Ivory Coast",
                   "league": "Africa Cup of Nations",
                   "status": "2H", "detail": "simple"}
        interaction = FakeInteraction()
        with patch.object(wmod, "start_watch",
                          new=AsyncMock(return_value=started)) as mock_start:
            await amod.handle_ask(interaction, "follow the Gambia game",
                                  model, CHANNELS)
        mock_start.assert_awaited_once()
        args, _kwargs = mock_start.call_args
        self.assertEqual(args[0], 777)
        self.assertEqual(args[1]["id"], "771747666")
        self.assertEqual(args[2], "simple")
        self.assertIn("Watching", interaction.followup.sent[0])

    async def test_set_alerts_saves_prefs_and_confirms(self):
        model = FakeModel('{"intent": "SET_ALERTS", "game_id": null, '
                          '"team": "Gambia", "events": ["goal", "red"], '
                          '"detail": null, "channel": null, '
                          '"note": "goals and reds"}')
        interaction = FakeInteraction()
        await amod.handle_ask(
            interaction,
            "only tell me about goals and red cards for the Gambia game",
            model, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Gambia", text)
        self.assertIn("goals", text)
        self.assertIn("red cards", text)
        stored = await db.get_prefs(777, "771747666")
        self.assertEqual(stored["events"], ["goal", "red"])

    async def test_search_no_match_suggests_live_games(self):
        model = FakeModel('{"intent": "SEARCH_MATCH", "game_id": null, '
                          '"team": "Zzqxj No Such Team", "events": null, '
                          '"detail": null, "channel": null, '
                          '"note": "search"}')
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "find the Zzqxj game",
                              model, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("No live match for 'Zzqxj No Such Team'", text)
        self.assertIn("Gambia", text)

    async def test_barca_fixtures_uses_provider_line(self):
        model = FakeModel('{"intent": "TEAM_FIXTURES", "game_id": null, '
                          '"team": "Barcelona", "events": null, '
                          '"detail": null, "channel": null, '
                          '"note": "Barca next"}')
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        class _FrozenDatetime(datetime):
            """now() is always Wed 18 Nov 2026, 12:00 NPT, so the
            Villarreal 22 Nov fixture is always in the future."""

            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 11, 18, 12, 0,
                                tzinfo=ZoneInfo("Asia/Kathmandu"))

        # Real kickoff: Sun 22 Nov 2026, 18:45 Nepal time = 13:00 UTC.
        kickoff = datetime(2026, 11, 22, 13, 0, tzinfo=timezone.utc)
        fixture = {"home": "FC Barcelona", "away": "Villarreal",
                   "kickoff": kickoff.isoformat()}
        interaction = FakeInteraction()
        with patch.object(amod, "datetime", _FrozenDatetime), \
                patch.object(prov, "free_fixtures_for_day",
                             new=AsyncMock(return_value=[fixture])):
            await amod.handle_ask(interaction,
                                  "when do Barcelona play next?",
                                  model, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("FC Barcelona vs Villarreal", text)
        self.assertIn("Kickoff:", text)
        # The reply carries the provider's real kickoff timestamp —
        # never the stale Rayo Vallecano / May 19 answer /ai gave.
        self.assertIn(f"<t:{int(kickoff.timestamp())}:F>", text)
        self.assertNotIn("Rayo", text)


if __name__ == "__main__":
    unittest.main()
