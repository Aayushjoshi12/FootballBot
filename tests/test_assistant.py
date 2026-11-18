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


class _Missing:
    """Sentinel mirroring discord.utils.MISSING: an omitted view
    argument is fine, an explicit view=None is not."""

    def __repr__(self):
        return "MISSING"


MISSING = _Missing()


class StrictFakeFollowup(FakeFollowup):
    """followup.send that enforces discord.py 2.4.0's real rule
    (Webhook.send): `view` omitted is fine, but an explicit
    view=None raises TypeError — "expected view parameter to be
    of type View not NoneType". The old permissive fake accepted
    view=None, which hid the production /ask failure: every _send
    without a view raised in production while the tests passed."""

    async def send(self, content=None, ephemeral=False, view=MISSING,
                   **kwargs):
        if view is not MISSING and view is None:
            raise TypeError(
                "expected view parameter to be of type View not NoneType")
        self.sent.append(content)
        self.sent_views.append(None if view is MISSING else view)


class StrictFakeInteraction(FakeInteraction):
    """An /ask interaction whose followup enforces the real
    discord.py rule."""

    def __init__(self, channel_id=777):
        super().__init__(channel_id)
        self.followup = StrictFakeFollowup()


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

    async def test_live_football_precheck(self):
        # 18 Nov 2026: "show me all live football" missed every
        # _LIVE_WORDS phrase, went to Gemini, and /ask failed.
        # A sport word + "live" is a live list, no model needed.
        model = NeverCallModel()
        out = await amod.parse_intent("show me all live football",
                                      model, CHANNELS)
        self.assertEqual(out["intent"], "LIVE_MATCHES")
        self.assertEqual(model.calls, 0)

    async def test_bare_sport_match_precheck(self):
        # 18 Nov 2026: /ask "football match" went to Gemini, failed,
        # and dead-ended with "Tell me which game". A bare sport
        # word + "match"/"game" is a live list, no model needed.
        model = NeverCallModel()
        for text in ("football match", "cricket match", "football",
                     "soccer game", "show me a cricket match"):
            out = await amod.parse_intent(text, model, CHANNELS)
            self.assertEqual(out["intent"], "LIVE_MATCHES", text)
        self.assertEqual(model.calls, 0)

    async def test_sport_plus_team_still_needs_model(self):
        # "football" alone is a list; "football Gambia game" names
        # a team, so it must still reach the model.
        model = FakeModel('{"intent": "MATCH_DETAILS", "game_id": null, '
                          '"team": "Gambia", "events": null, '
                          '"detail": null, "channel": null, "note": ""}')
        out = await amod.parse_intent("show the Gambia football game",
                                      model, CHANNELS)
        self.assertEqual(out["intent"], "MATCH_DETAILS")
        self.assertEqual(out["team"], "Gambia")
        self.assertEqual(model.calls, 1)

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

    async def test_no_team_no_id_match_details_passes_for_pick_list(self):
        # 18 Nov 2026: "football match" parsed to MATCH_DETAILS
        # with no team and no id, and validation dead-ended with
        # "Tell me which game". Now it passes so the handler can
        # answer with the top live games as a pick list.
        out = await amod.validate_intent(_parsed("MATCH_DETAILS"),
                                         self.live, CHANNELS)
        self.assertTrue(out["ok"], out)
        self.assertIsNone(out["game_id"])
        self.assertIsNone(out["team"])

    async def test_no_team_no_id_search_match_passes_for_pick_list(self):
        out = await amod.validate_intent(_parsed("SEARCH_MATCH"),
                                         self.live, CHANNELS)
        self.assertTrue(out["ok"], out)
        self.assertIsNone(out["game_id"])

    async def test_no_team_no_id_follow_still_errors(self):
        # Only MATCH_DETAILS / SEARCH_MATCH get the pick list;
        # follow/unfollow still need a game.
        out = await amod.validate_intent(_parsed("FOLLOW_MATCH"),
                                         self.live, CHANNELS)
        self.assertFalse(out["ok"])
        self.assertIn("which game", out["error"])

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
        # The 10th child is a DynamicItem (▶ Watch), whose label
        # lives on .item — plain buttons carry .label directly.
        labels = [getattr(b, "label", None) or b.item.label
                  for b in view.children]
        self.assertEqual(labels[:2], ["📊 Detail", "🔄 Live"])

    async def test_match_details_reply_has_watch_button(self):
        # 18 Nov 2026: the user asked for a Watch button when
        # /ask shows a game. The detail reply carries a 10th
        # button, ▶ Watch, wired to this game's id.
        model = FakeModel('{"intent": "MATCH_DETAILS", "game_id": null, '
                          '"team": "Republic of the Gambia", '
                          '"events": null, "detail": "simple", '
                          '"channel": null, "note": "Gambia"}')
        interaction = FakeInteraction()
        await amod.handle_ask(interaction,
                              "show me the Gambia game", model, CHANNELS)
        view = interaction.followup.sent_views[0]
        self.assertIsNotNone(view)
        self.assertEqual(len(view.children), 10)
        watch_btn = view.children[-1]
        self.assertEqual(watch_btn.item.label, "▶ Watch")
        self.assertEqual(watch_btn.custom_id, "panel:watch:771747666")
        self.assertIsInstance(watch_btn, wmod.WatchGameButton)

    async def test_live_matches_reply_has_watch_buttons(self):
        # The live list is a pick list: one ▶ Watch button per
        # game, labelled with the teams, so the user can watch
        # one without typing /watch.
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "what games are live",
                              None, CHANNELS)
        view = interaction.followup.sent_views[0]
        self.assertIsNotNone(view)
        self.assertIsInstance(view, wmod.AskWatchView)
        self.assertTrue(view.children)
        self.assertTrue(all(b.custom_id.startswith("panel:watch:")
                            for b in view.children))
        self.assertTrue(all(b.item.label.startswith("▶ Watch ")
                            for b in view.children))
        self.assertIn("panel:watch:771747666",
                      [b.custom_id for b in view.children])

    async def test_football_match_reply_has_watch_buttons(self):
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "football match",
                              None, CHANNELS)
        view = interaction.followup.sent_views[0]
        self.assertIsNotNone(view)
        self.assertIsInstance(view, wmod.AskWatchView)
        self.assertTrue(view.children)
        self.assertIn("panel:watch:771747666",
                      [b.custom_id for b in view.children])

    async def test_search_match_pick_list_has_watch_buttons(self):
        model = FakeModel('{"intent": "SEARCH_MATCH", "game_id": null, '
                          '"team": null, "events": null, "detail": null, '
                          '"channel": null, "note": ""}')
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "find a game", model, CHANNELS)
        view = interaction.followup.sent_views[0]
        self.assertIsNotNone(view)
        self.assertIsInstance(view, wmod.AskWatchView)
        self.assertTrue(view.children)
        self.assertIn("panel:watch:771747666",
                      [b.custom_id for b in view.children])

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

    async def test_football_match_lists_live_games(self):
        # 18 Nov 2026: /ask "football match" failed twice
        # ("I couldn't understand that right now", then
        # "Tell me which game"). The pre-check answers it
        # with the live list — football first — no model call.
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "football match",
                              None, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Gambia", text)
        self.assertNotIn("Tell me which game", text)
        self.assertNotIn("couldn't understand", text)
        # Football only: the cricket games are not in this list.
        self.assertNotIn("JYC", text)

    async def test_match_details_no_game_shows_top5_pick_list(self):
        # A parsed MATCH_DETAILS with no team and no id gets
        # the top 5 live games (football first), not the bare
        # "Tell me which game" dead end. Nothing is invented:
        # every listed game is live, with its real id.
        model = FakeModel('{"intent": "MATCH_DETAILS", "game_id": null, '
                          '"team": null, "events": null, "detail": null, '
                          '"channel": null, "note": ""}')
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "show me a match",
                              model, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("pick one", text)
        self.assertIn("Gambia", text)
        self.assertIn("Sierra Leone", text)
        self.assertIn("771747666", text)
        self.assertNotIn("Tell me which game", text)
        self.assertLess(text.index("Gambia"), text.index("JYC"))

    async def test_search_match_no_game_shows_top5_pick_list(self):
        model = FakeModel('{"intent": "SEARCH_MATCH", "game_id": null, '
                          '"team": null, "events": null, "detail": null, '
                          '"channel": null, "note": ""}')
        interaction = FakeInteraction()
        await amod.handle_ask(interaction, "find a game", model, CHANNELS)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("pick one", text)
        self.assertIn("Gambia", text)
        self.assertNotIn("Tell me which game", text)

    async def test_live_football_lists_football_only(self):
        # 18 Nov 2026: /ask "show me all live football" failed
        # twice ("Something went wrong") and would have listed
        # every sport anyway. With a mixed live list, the football
        # question lists football only — and never fails.
        npl = json.loads((Path(__file__).resolve().parent
                          / "fixtures" / "scoreboard"
                          / "games1x2_npl.json").read_text(encoding="utf-8"))
        mixed = self.games1x2 + npl
        with patch.object(prov, "onex_live_raw",
                          new=AsyncMock(return_value=mixed)):
            interaction = FakeInteraction()
            await amod.handle_ask(interaction,
                                  "show me all live football",
                                  None, CHANNELS)
            text = interaction.followup.sent[0]
            self.assertNotIn("Something went wrong", text)
            self.assertIn("Gambia", text)
            self.assertNotIn("Lumbini", text)
            # Control: without the sport word, the cricket game
            # IS listed (it is live) — the filter is what hides it.
            interaction2 = FakeInteraction()
            await amod.handle_ask(interaction2, "what games are live",
                                  None, CHANNELS)
            self.assertIn("Lumbini", interaction2.followup.sent[0])

    async def test_live_football_strict_followup_no_view_none(self):
        # The 18 Nov 2026 production traceback: handle_ask ->
        # _send -> followup.send(view=None) raised TypeError in
        # real discord.py, so EVERY /ask live-list question failed
        # in production while the permissive fakes passed. With a
        # followup that enforces the real rule, the same question
        # must answer with the list, not "Something went wrong".
        npl = json.loads((Path(__file__).resolve().parent
                          / "fixtures" / "scoreboard"
                          / "games1x2_npl.json").read_text(encoding="utf-8"))
        mixed = self.games1x2 + npl
        with patch.object(prov, "onex_live_raw",
                          new=AsyncMock(return_value=mixed)):
            interaction = StrictFakeInteraction()
            await amod.handle_ask(interaction,
                                  "show me all live football",
                                  None, CHANNELS)
            self.assertEqual(len(interaction.followup.sent), 1)
            text = interaction.followup.sent[0]
            self.assertNotIn("Something went wrong", text)
            self.assertIn("Gambia", text)
            self.assertNotIn("Lumbini", text)

    async def test_live_cricket_strict_followup_no_view_none(self):
        # Same production failure, cricket wording.
        npl = json.loads((Path(__file__).resolve().parent
                          / "fixtures" / "scoreboard"
                          / "games1x2_npl.json").read_text(encoding="utf-8"))
        mixed = self.games1x2 + npl
        with patch.object(prov, "onex_live_raw",
                          new=AsyncMock(return_value=mixed)):
            interaction = StrictFakeInteraction()
            await amod.handle_ask(interaction,
                                  "show me all live cricket",
                                  None, CHANNELS)
            self.assertEqual(len(interaction.followup.sent), 1)
            text = interaction.followup.sent[0]
            self.assertNotIn("Something went wrong", text)
            self.assertIn("Lumbini", text)
            self.assertNotIn("Gambia", text)

    async def test_send_without_view_omits_view_argument(self):
        # _send with no view must not pass view=None at all — not
        # on a single chunk and not on any chunk of a split reply.
        interaction = StrictFakeInteraction()
        await amod._send(interaction, "hello")
        self.assertEqual(interaction.followup.sent, ["hello"])
        self.assertEqual(interaction.followup.sent_views, [None])
        interaction2 = StrictFakeInteraction()
        await amod._send(interaction2, "first line\n" + "x" * 2500)
        # "first line" + the 2500-char line hard-cut at 1900 = 3 chunks.
        self.assertEqual(len(interaction2.followup.sent), 3)
        self.assertEqual(interaction2.followup.sent_views,
                         [None, None, None])

    async def test_send_with_view_attaches_it_to_last_chunk_only(self):
        # A view still reaches the last chunk — and only it.
        view = Mock()
        interaction = StrictFakeInteraction()
        await amod._send(interaction, "first line\n" + "x" * 2500,
                         view=view)
        # 3 chunks (see above): the view rides the last one only.
        self.assertEqual(len(interaction.followup.sent), 3)
        self.assertIsNone(interaction.followup.sent_views[0])
        self.assertIsNone(interaction.followup.sent_views[1])
        self.assertIs(interaction.followup.sent_views[2], view)

    async def test_live_list_stays_under_discord_limit(self):
        # A 15-game list of long cricket names can pass Discord's
        # 2000-character message limit on its own — the send then
        # raises and the whole answer is lost ("Something went
        # wrong", 18 Nov 2026). The list is budgeted, with an
        # "…and N more" tail.
        games = []
        for i in range(15):
            games.append({
                "home": f"Royal Challengers Bengaluru Long Name {i}",
                "away": f"Mumbai Indians Very Long Team Name {i}",
                "home_score": 186, "away_score": 182,
                "league": "Indian Premier League, Super Over Eliminator",
                "onex_id": 771000000 + i, "status": "2H",
                "clock": "19.2 ov",
                "cricket": {"over_str": "19.2", "is_break": False}})
        msg = ("⚽ **Live games right now — pick one:**\n"
               + amod._live_line_list(games, 15)
               + "\n\nUse `/game <ID>` for the full detail, "
                 "or ask me to follow one.")
        self.assertLess(len(msg), 2000)
        self.assertIn("…and", msg)

    async def test_live_football_fresh_snapshot(self):
        # 18 Nov 2026: /ask "show me all live football" answered
        # "Something went wrong" in production twice on the old
        # code and once more after the first fix — while the same
        # question passed against small fixtures. This test runs
        # the full handler against a REAL fresh live snapshot
        # (games1x2 + the topGames-only game, captured from the
        # lite mirror 18 Nov 2026 ~15:16 NPT): the pre-check must
        # answer without Gemini, the reply must list football
        # only, and it must stay under Discord's limit.
        fresh = json.loads((Path(__file__).resolve().parent
                            / "fixtures" / "assistant"
                            / "games1x2_fresh_20261118.json")
                           .read_text(encoding="utf-8"))
        model = NeverCallModel()
        with patch.object(prov, "onex_live_raw",
                          new=AsyncMock(return_value=fresh)):
            interaction = FakeInteraction()
            await amod.handle_ask(interaction,
                                  "show me all live football",
                                  model, CHANNELS)
        self.assertEqual(model.calls, 0)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertNotIn("Something went wrong", text)
        self.assertLess(len(text), 2000)
        self.assertIn("Nahia U19", text)
        self.assertIn("Cerezo Osaka", text)
        # The topGames-only game (not in games1x2) is listed too.
        self.assertIn("Persibangga", text)
        self.assertIn("771937351", text)
        # …and no other sport leaks into the football reply.
        self.assertNotIn("Kolkata Knight Riders", text)
        self.assertNotIn("Chennai Cine Docs", text)

    async def test_handle_ask_logs_traceback_on_failure(self):
        # The production failures of 18 Nov 2026 left only an
        # exception TYPE in the journal, which was not enough to
        # find the cause. The catch-all now logs the full
        # traceback — while the user still gets exactly the same
        # short error reply.
        interaction = FakeInteraction()
        with patch.object(amod, "_handle_ask",
                          side_effect=RuntimeError("boom")):
            with self.assertLogs("assistant", level="ERROR") as logs:
                await amod.handle_ask(interaction,
                                      "show me all live football",
                                      None, CHANNELS)
        self.assertEqual(interaction.followup.sent,
                         ["Something went wrong — please try again."])
        joined = "\n".join(logs.output)
        self.assertIn("Traceback", joined)
        self.assertIn("RuntimeError: boom", joined)

    def test_grounding_skips_malformed_entry(self):
        # One entry the normalizer cannot handle must not kill
        # the whole reply — it is skipped, the rest are listed.
        good = self.games1x2[0]
        real = prov._onex_detail_from_raw
        calls = {"n": 0}

        def flaky(g):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("malformed entry")
            return real(g)

        with patch.object(prov, "_onex_detail_from_raw",
                          side_effect=flaky):
            out = amod._grounding_entries([{"id": "bad"}, good])
        self.assertEqual(len(out), 1)
        self.assertEqual(str(out[0]["onex_id"]), "771747666")

    def test_event_in_progress_cricket_counts_live(self):
        # Live cricket often carries only "Event in progress" —
        # no timer, no period. It is live, not "?".
        status, _minute = prov._onex_status(
            {"statusLineStr": "Event in progress"})
        self.assertEqual(status, "Live")
        self.assertTrue(amod._is_live({"status": "Live"}))
        # End to end: a raw cricket entry with that status line
        # grounds to a live match instead of being filtered out.
        npl = json.loads((Path(__file__).resolve().parent
                          / "fixtures" / "scoreboard"
                          / "games1x2_npl.json").read_text(encoding="utf-8"))
        entry = json.loads(json.dumps(npl[0]))
        entry["scores"]["statusLineStr"] = "Event in progress"
        entry["scores"]["currentPeriod"] = None
        entry["scores"]["currentPeriodName"] = ""
        entry["scores"]["timer"] = None
        detail = prov._onex_detail_from_raw(entry)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["status"], "Live")
        self.assertTrue(amod._is_live(detail))

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


class FakeChannel:
    """A channel the bot can send a normal visible message to —
    the last-resort /ask reply path when the token is dead."""

    def __init__(self):
        self.sent: list[str] = []
        self.sent_views: list = []

    async def send(self, content=None, view=None, **kwargs):
        self.sent.append(content)
        self.sent_views.append(view)
        return SimpleNamespace(id=9999)


class FailingDeferResponse(FakeResponse):
    """response.defer that fails the way Discord did on 18 Nov
    2026 ~22:44 NPT (three REST timeouts, then the token was
    discarded)."""

    async def defer(self, ephemeral=False, thinking=False, **kwargs):
        raise RuntimeError("patch transport failed (TimeoutError)")


class ExplodingFollowup(FakeFollowup):
    """followup.send that fails loudly if anything calls it —
    proves the dead-token path never attempts a followup."""

    async def send(self, content=None, ephemeral=False, view=None,
                   **kwargs):
        raise AssertionError("followup must not be used on a dead token")


class DeadTokenInteraction(FakeInteraction):
    """An /ask interaction whose defer failed: the token is
    dead, the channel is the only way to answer."""

    def __init__(self, channel_id=777):
        super().__init__(channel_id=channel_id)
        self.response = FailingDeferResponse()
        self.followup = ExplodingFollowup()
        self.channel = FakeChannel()


class DyingFollowup(FakeFollowup):
    """followup.send that fails like production did (Unknown
    Webhook 10015 — the token died after the defer)."""

    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def send(self, content=None, ephemeral=False, view=None,
                   **kwargs):
        self.attempts += 1
        raise RuntimeError("Unknown Webhook (10015)")


class DyingFollowupInteraction(FakeInteraction):
    """Defer succeeds, but every followup is rejected."""

    def __init__(self, channel_id=777):
        super().__init__(channel_id=channel_id)
        self.followup = DyingFollowup()
        self.channel = FakeChannel()


class TestAskDeferFailure(unittest.IsolatedAsyncioTestCase):
    """18 Nov 2026 ~22:44 NPT: /ask and /help both failed with
    'application did not respond' after three Discord REST
    timeouts. The bot never crashed — the tokens were discarded
    before the replies arrived. A failed defer must be logged,
    and the answer must still reach the user as a channel
    message instead of being lost."""

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

    async def test_failing_defer_logs_and_uses_channel(self):
        interaction = DeadTokenInteraction()
        with self.assertLogs("assistant", level="WARNING") as logs:
            await amod.handle_ask(interaction, "what games are live",
                                  NeverCallModel(), CHANNELS)
        self.assertTrue(any("defer failed" in line for line in logs.output),
                        logs.output)
        # followup was never attempted on the dead token…
        self.assertEqual(interaction.followup.sent, [])
        # …and the answer still reached the channel, visibly.
        self.assertEqual(len(interaction.channel.sent), 1)
        self.assertIn("Gambia", interaction.channel.sent[0])

    async def test_followup_failure_falls_back_to_channel(self):
        interaction = DyingFollowupInteraction()
        with self.assertLogs("assistant", level="WARNING") as logs:
            await amod.handle_ask(interaction, "help", None, CHANNELS)
        self.assertTrue(any("followup failed" in line
                            for line in logs.output), logs.output)
        self.assertEqual(interaction.followup.attempts, 1)
        self.assertEqual(len(interaction.channel.sent), 1)
        self.assertIn("Here's what you can ask me",
                      interaction.channel.sent[0])

    async def test_defer_failure_error_path_uses_channel(self):
        # Even when the handler itself blows up, a dead token
        # must not swallow the error reply.
        interaction = DeadTokenInteraction()
        with patch.object(amod, "_handle_ask",
                          new=AsyncMock(
                              side_effect=RuntimeError("boom"))):
            await amod.handle_ask(interaction, "help", None, CHANNELS)
        self.assertEqual(interaction.followup.sent, [])
        self.assertEqual(interaction.channel.sent,
                         ["Something went wrong — please try again."])


if __name__ == "__main__":
    unittest.main()
