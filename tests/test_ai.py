""" /ai fixture grounding (18 Nov 2026 regression).

/ai answered "When is FC Barcelona's next match?" with
"Rayo Vallecano, Sunday May 19th" — a stale fixture from the
model's training data, because the raw question went straight to
Gemini. Fixture questions must now be answered from the provider
(the /nextbarca lookup); Gemini only words the grounded facts,
and any other team's fixture question is answered as unavailable
rather than guessed.

No network, no Discord connection, no real Gemini — the fixture
provider and the Gemini call are both mocked at the boundary.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as botmod

# Real next Barcelona fixture at fix time: Villarreal, Sun 22 Nov
# 2026, 18:45 Nepal time (= 13:00 UTC).
BARCA_KICKOFF = datetime(2026, 11, 22, 13, 0, tzinfo=timezone.utc)
BARCA_FIXTURE = {"home": "FC Barcelona", "away": "Villarreal",
                 "kickoff": BARCA_KICKOFF.isoformat()}


class _FrozenDatetime(datetime):
    """now() is always Wed 18 Nov 2026, 12:00 NPT — the day the
    stale /ai answer was caught — so the Villarreal 22 Nov fixture
    is always in the future no matter when the suite runs."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 18, 12, 0,
                        tzinfo=ZoneInfo("Asia/Kathmandu"))


class _Resp:
    async def defer(self, ephemeral=False, thinking=False, **kwargs):
        return None

    async def send_message(self, content=None, embed=None, ephemeral=False,
                           view=None, **kwargs):
        return None


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=False,
                   view=None, **kwargs):
        self.sent.append(content)


class _Interaction:
    def __init__(self):
        self.response = _Resp()
        self.followup = _Followup()
        self.user = SimpleNamespace(id=42)


def _text(interaction):
    return "\n".join(str(t) for t in interaction.followup.sent)


class TestAiFixtureDetection(unittest.TestCase):
    """_ai_fixture_team: fixture questions are caught, general
    questions pass through to Gemini untouched."""

    def test_barca_next_match_detected(self):
        self.assertEqual(
            botmod._ai_fixture_team(
                "When is FC Barcelona's next match and who is it against?"),
            "barcelona")

    def test_barca_play_next_detected(self):
        self.assertEqual(
            botmod._ai_fixture_team("when do barcelona play next"),
            "barcelona")

    def test_barca_playing_today_detected(self):
        self.assertEqual(
            botmod._ai_fixture_team("Is Barcelona playing today?"),
            "barcelona")

    def test_other_team_detected(self):
        self.assertEqual(
            botmod._ai_fixture_team("When do Real Madrid play next?"),
            "other")

    def test_general_question_passes_through(self):
        self.assertIsNone(
            botmod._ai_fixture_team("Who won the World Cup in 2022?"))

    def test_live_score_question_passes_through(self):
        self.assertIsNone(
            botmod._ai_fixture_team("who is winning the Arsenal match?"))


class TestAiGroundedFixtures(unittest.IsolatedAsyncioTestCase):
    """The 18 Nov incident, replayed: the exact question that got
    the stale Rayo/May-19 answer must now return Villarreal 22 Nov
    from provider data."""

    def _run_ai(self, prompt, fixtures, gemini):
        interaction = _Interaction()
        patches = [
            patch.object(botmod, "datetime", _FrozenDatetime),
            patch.object(botmod, "free_fixtures_for_day",
                         new=AsyncMock(return_value=fixtures)),
            patch.object(botmod, "_ask_gemini", new=gemini),
        ]
        return interaction, patches

    async def test_barca_next_match_grounded(self):
        gemini = AsyncMock(return_value=(
            "Barcelona face Villarreal on Sunday 22 November at "
            "6:45 PM Nepal time."))
        interaction, patches = self._run_ai(
            "When is FC Barcelona's next match and who is it against?",
            [BARCA_FIXTURE], gemini)
        with patches[0], patches[1], patches[2]:
            await botmod.cmd_ai.callback(
                interaction,
                "When is FC Barcelona's next match and who is it against?")
        text = _text(interaction)
        self.assertIn("Villarreal", text)
        self.assertNotIn("Rayo", text)
        self.assertNotIn("May 19", text)
        # Gemini was only asked to WORD the grounded facts — the
        # prompt it got carries the provider's Villarreal fixture,
        # not the user's raw question.
        gemini_prompt = gemini.call_args.args[0]
        self.assertIn("Villarreal", gemini_prompt)
        self.assertIn("22 November", gemini_prompt)
        self.assertNotIn("Rayo Vallecano", gemini_prompt)

    async def test_barca_next_match_gemini_failure_falls_back(self):
        gemini = AsyncMock(side_effect=RuntimeError("model down"))
        interaction, patches = self._run_ai(
            "when do barcelona play next", [BARCA_FIXTURE], gemini)
        with patches[0], patches[1], patches[2]:
            await botmod.cmd_ai.callback(interaction,
                                         "when do barcelona play next")
        text = _text(interaction)
        self.assertIn("Villarreal", text)
        self.assertIn("22 November", text)
        self.assertNotIn("Rayo", text)

    async def test_playing_today_grounded(self):
        gemini = AsyncMock(return_value="No — Barcelona don't play today.")
        interaction, patches = self._run_ai(
            "Is Barcelona playing today?", [BARCA_FIXTURE], gemini)
        with patches[0], patches[1], patches[2]:
            await botmod.cmd_ai.callback(interaction,
                                         "Is Barcelona playing today?")
        gemini_prompt = gemini.call_args.args[0]
        self.assertIn("don't play today", gemini_prompt)
        self.assertIn("Villarreal", gemini_prompt)

    async def test_no_fixture_available_says_so(self):
        gemini = AsyncMock(side_effect=RuntimeError("model down"))
        interaction, patches = self._run_ai(
            "when do barcelona play next", [], gemini)
        with patches[0], patches[1], patches[2]:
            await botmod.cmd_ai.callback(interaction,
                                         "when do barcelona play next")
        text = _text(interaction)
        self.assertIn("No upcoming Barcelona fixture", text)
        self.assertNotIn("Rayo", text)

    async def test_other_team_fixture_not_guessed(self):
        gemini = AsyncMock(return_value="Real Madrid play ...")
        interaction, patches = self._run_ai(
            "When do Real Madrid play next?", [BARCA_FIXTURE], gemini)
        with patches[0], patches[1], patches[2]:
            await botmod.cmd_ai.callback(
                interaction, "When do Real Madrid play next?")
        gemini.assert_not_called()
        text = _text(interaction)
        self.assertIn("only have live fixtures for Barcelona", text)
        self.assertNotIn("Real Madrid play", text)

    async def test_general_question_still_uses_gemini(self):
        gemini = AsyncMock(return_value="Argentina won the 2022 World Cup.")
        interaction = _Interaction()
        with patch.object(botmod, "_ask_gemini", new=gemini):
            await botmod.cmd_ai.callback(
                interaction, "Who won the World Cup in 2022?")
        gemini.assert_called_once_with("Who won the World Cup in 2022?")
        self.assertIn("Argentina won", _text(interaction))


if __name__ == "__main__":
    unittest.main()
