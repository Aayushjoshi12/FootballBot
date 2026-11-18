"""18 Nov 2026 — /ai was removed (replaced by /ask, which answers
fixture questions from the live provider instead of raw Gemini).
These tests lock the removal in: the command is gone from the
tree and from the module, and /ask still covers the fixture
questions /ai used to answer."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as botmod

REMOVED = ("ai", "today", "tomorrow", "fixtures", "stats", "ping")
REMOVED_CALLBACKS = ("cmd_ai", "cmd_today", "cmd_tomorrow", "cmd_fixtures",
                     "cmd_stats", "cmd_ping")
KEPT = ("watch", "unwatch", "watching", "game", "live", "scoreboard",
        "stopscoreboard", "ask", "notifications", "history", "nextbarca",
        "health", "help", "maintenance", "shutdown")


class TestRemovedCommands(unittest.TestCase):
    def test_removed_commands_not_in_tree(self):
        names = {c.name for c in botmod.bot.tree.get_commands()}
        for name in REMOVED:
            self.assertNotIn(name, names)

    def test_removed_command_callbacks_gone(self):
        for attr in REMOVED_CALLBACKS:
            self.assertFalse(hasattr(botmod, attr))

    def test_kept_commands_still_in_tree(self):
        names = {c.name for c in botmod.bot.tree.get_commands()}
        for name in KEPT:
            self.assertIn(name, names)

    def test_ai_fixture_grounding_helpers_gone(self):
        # The /ai-only helpers (_ask_gemini, _ai_is_fixture_question,
        # _ai_fixture_reply) are gone; /ask owns fixture questions now.
        for attr in ("_ask_gemini", "_ai_is_fixture_question",
                     "_ai_fixture_reply"):
            self.assertFalse(hasattr(botmod, attr))

    def test_ask_still_exists_and_covers_fixtures(self):
        self.assertTrue(hasattr(botmod, "cmd_ask"))
        self.assertTrue(hasattr(botmod, "cmd_nextbarca"))


if __name__ == "__main__":
    unittest.main()
