"""Wiring tests for the master-upgrade commands in bot.py:
/ask, /notifications, /history (with pagination). No network,
no Discord connection — handlers are mocked at the boundary.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import history
import bot as botmod


class _Resp:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def defer(self, ephemeral=False, thinking=False, **kwargs):
        return None

    async def send_message(self, content=None, embed=None, ephemeral=False,
                           view=None, **kwargs):
        self.sent.append((content, embed, view))

    async def edit_message(self, content=None, view=None, **kwargs):
        self.edited.append((content, view))


class _Followup:
    def __init__(self):
        self.sent = []
        self.sent_views = []

    async def send(self, content=None, embed=None, ephemeral=False,
                   view=None, **kwargs):
        self.sent.append(content)
        self.sent_views.append(view)


class _Interaction:
    def __init__(self, channel_id=777, user_id=42):
        self.channel_id = channel_id
        self.user = SimpleNamespace(id=user_id)
        self.guild = SimpleNamespace(
            text_channels=[SimpleNamespace(id=777, name="test"),
                           SimpleNamespace(id=778, name="general")])
        self.response = _Resp()
        self.followup = _Followup()


class TestNewCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".new-cmd-test-tmp"
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

    async def test_ask_delegates_to_assistant(self):
        interaction = _Interaction()
        with patch.object(botmod.assistant, "handle_ask",
                          new=AsyncMock()) as handle:
            await botmod.cmd_ask.callback(interaction, "what games are live?")
        handle.assert_awaited_once()
        args = handle.await_args.args
        self.assertIs(args[0], interaction)
        self.assertEqual(args[1], "what games are live?")
        self.assertEqual(args[3], [{"id": 777, "name": "test"},
                                   {"id": 778, "name": "general"}])

    async def test_notifications_delegates_to_manager(self):
        interaction = _Interaction()
        with patch.object(botmod.notify_manager, "show_notifications",
                          new=AsyncMock()) as show:
            await botmod.cmd_notifications.callback(interaction)
        show.assert_awaited_once_with(interaction)

    async def test_history_empty(self):
        interaction = _Interaction()
        await botmod.cmd_history.callback(interaction)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn("No match history yet", interaction.followup.sent[0])
        self.assertIsNone(interaction.followup.sent_views[0])

    async def test_history_lists_finished_match(self):
        await history.record_match_result(
            "771747666", "Republic of the Gambia", "Ivory Coast",
            "Africa Cup of Nations", "Football", 0, 1, "FT")
        interaction = _Interaction()
        await botmod.cmd_history.callback(interaction)
        self.assertIn("Gambia", interaction.followup.sent[0])
        self.assertIn("0–1", interaction.followup.sent[0])

    async def test_history_pagination(self):
        for i in range(6):
            await history.record_match_result(
                f"game{i}", f"Home{i}", f"Away{i}", "League", "Football",
                1, 0, "FT")
        interaction = _Interaction()
        await botmod.cmd_history.callback(interaction)
        view = interaction.followup.sent_views[0]
        self.assertIsNotNone(view)
        self.assertIn("Page 1/2", interaction.followup.sent[0])
        # Next page.
        nxt = _Interaction()
        await view.children[1].callback(nxt)
        self.assertEqual(len(nxt.response.edited), 1)
        self.assertIn("Page 2/2", nxt.response.edited[0][0])
        # Previous page.
        prev = _Interaction()
        await view.children[0].callback(prev)
        self.assertIn("Page 1/2", prev.response.edited[0][0])

    def test_history_pages_chunking(self):
        matches = [{"home": f"H{i}", "away": f"A{i}", "final_home": 1,
                    "final_away": 0, "status": "FT"} for i in range(6)]
        pages = botmod._history_pages(matches, [])
        self.assertEqual(len(pages), 2)
        self.assertIn("Page 1/2", pages[0])
        self.assertIn("Page 2/2", pages[1])

    def test_help_lists_new_commands(self):
        # /help text is built inline in cmd_help; the command
        # descriptions are the contract — new commands must exist.
        names = {c.name for c in botmod.bot.tree.get_commands()}
        for expected in ("ask", "notifications", "history", "health",
                         "watch", "game", "live", "scoreboard"):
            self.assertIn(expected, names)
