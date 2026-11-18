"""Maintenance mode + /shutdown tests (18 Nov 2026).

/maintenance off silences ALL bot output (scoreboard ticks, watch
panels/alerts, the Barcelona reminder, every slash command except
/maintenance and /shutdown); /maintenance on resumes. The flag
lives in sqlite so it survives restarts. Owner-only: the guild
owner, or MAINTENANCE_OWNER_ID when set.

No Discord token, no network, no real shutdown — the exit is
patched out in the shutdown test.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import barcelona_reminder as barca
import bot as botmod
import db
import maintenance
import providers as prov
import scoreboard as sb
import watch as wmod

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "watch"
SB_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scoreboard"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeMessage:
    def __init__(self, mid, content, view=None):
        self.id = mid
        self.content = content
        self.view = view
        self.edits = []
        self.deleted = False

    async def edit(self, content=None, allowed_mentions=None, view=None):
        self.content = content
        if view is not None:
            self.view = view
        self.edits.append(content)

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.messages = {}
        self._next_id = 3000

    async def send(self, content, allowed_mentions=None, view=None):
        self._next_id += 1
        msg = FakeMessage(self._next_id, content, view=view)
        self.sent.append(msg)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        return self.messages[mid]


class FakeBot:
    def __init__(self, channel):
        self._channel = channel
        self._ready = asyncio.Event()
        self._ready.set()
        self.closed = False

    def get_channel(self, cid):
        return self._channel

    async def fetch_channel(self, cid):
        return self._channel

    async def wait_until_ready(self):
        await self._ready.wait()

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def defer(self, ephemeral=False, thinking=False, **kwargs):
        return None

    async def send_message(self, content=None, embed=None, ephemeral=False,
                           view=None, **kwargs):
        self.sent.append((content, view))


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=False,
                   view=None, **kwargs):
        self.sent.append(content)


class FakeInteraction:
    """Slash-command interaction with a guild owner."""

    def __init__(self, channel_id=777, user_id=42, owner_id=42,
                 command_name="live"):
        self.channel_id = channel_id
        self.user = SimpleNamespace(id=user_id)
        self.guild = SimpleNamespace(owner_id=owner_id)
        self.data = {"name": command_name}
        self.type = discord.InteractionType.application_command
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.client = FakeBot(FakeChannel())


class TestMaintenanceFlag(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".maint-test-tmp"
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

    def _seed_watch(self, with_panel=True):
        snap = wmod.snapshot_from_detail(
            {"home": "Republic of the Gambia", "away": "Ivory Coast",
             "home_score": 0, "away_score": 1, "status": "2H", "minute": 67,
             "clock": "67'", "stats": [], "cricket": None})
        snap["_home"], snap["_away"] = "Republic of the Gambia", "Ivory Coast"
        panel_id = 2001 if with_panel else None
        panel_body = None
        if with_panel:
            detail = prov._onex_detail_from_raw(self.games1x2[0])
            panel_body = wmod.render_panel(detail, "67:00")
            self.channel.messages[2001] = FakeMessage(2001, panel_body)
        wmod._watches[(777, "771747666")] = {
            "home": "Republic of the Gambia", "away": "Ivory Coast",
            "league": "Africa Cup of Nations", "detail": "simple", "fmt": "",
            "snapshot": snap, "missing": 0,
            "panel_message_id": panel_id, "panel_body": panel_body,
            "last_panel_edit": 0.0, "panel_blocked_until": 0.0,
            "panel_timer_sec": None, "panel_timer_ts": 0.0,
        }

    async def test_flag_defaults_off(self):
        self.assertFalse(await maintenance.is_on())

    async def test_maintenance_silences_watch_tick(self):
        self._seed_watch()
        await maintenance.set_on(True)
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        # A goal during maintenance sends nothing and edits nothing.
        self.assertEqual(len(self.channel.sent), 0)
        panel = self.channel.messages[2001]
        self.assertEqual(len(panel.edits), 0)
        self.assertIn((777, "771747666"), wmod._watches)

    async def test_maintenance_on_resumes_watch_tick(self):
        self._seed_watch()
        await maintenance.set_on(True)
        changed = json.loads(json.dumps(self.games1x2[0]))
        changed["scores"]["scoreOpp1"] = 1
        await wmod.on_tick(self.bot, [changed])
        self.assertEqual(len(self.channel.sent), 0)
        await maintenance.set_on(False)
        await wmod.on_tick(self.bot, [changed])
        # Resumed: the goal alert goes out and the panel ticks.
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("GOAL", self.channel.sent[0].content)
        panel = self.channel.messages[2001]
        self.assertEqual(len(panel.edits), 1)

    async def test_flag_persists_across_restart(self):
        await maintenance.set_on(True)
        # Simulate a restart: close and reopen the database.
        await db.close()
        await db.init()
        self.assertTrue(await maintenance.is_on())
        await maintenance.set_on(False)
        await db.close()
        await db.init()
        self.assertFalse(await maintenance.is_on())

    async def test_reminder_deferred_during_maintenance(self):
        await maintenance.set_on(True)
        from datetime import datetime, timezone
        result = await barca._deliver(
            self.bot, {"home": "FC Barcelona", "away": "Villarreal"},
            datetime(2026, 11, 22, 13, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 22, 6, 45, tzinfo=timezone.utc))
        self.assertEqual(result, "deferred")
        self.assertEqual(len(self.channel.sent), 0)

    async def test_view_blocked_during_maintenance(self):
        view = wmod.WatchAlertView("771747666")
        interaction = FakeInteraction()
        self.assertFalse(await maintenance.is_on())
        self.assertTrue(await view.interaction_check(interaction))
        await maintenance.set_on(True)
        self.assertFalse(await view.interaction_check(interaction))
        self.assertEqual(len(interaction.response.sent), 1)
        self.assertIn("maintenance", interaction.response.sent[0][0])


class TestMaintenanceCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".maint-test-tmp2"
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

    async def test_non_owner_refused(self):
        interaction = FakeInteraction(user_id=99, owner_id=42)
        await botmod.cmd_maintenance.callback(interaction, "off")
        self.assertIn("Only the server owner",
                      interaction.response.sent[0][0])
        self.assertFalse(await maintenance.is_on())

    async def test_owner_can_toggle(self):
        interaction = FakeInteraction(user_id=42, owner_id=42)
        await botmod.cmd_maintenance.callback(interaction, "off")
        self.assertTrue(await maintenance.is_on())
        self.assertIn("Maintenance mode ON",
                      interaction.response.sent[0][0])
        interaction2 = FakeInteraction(user_id=42, owner_id=42)
        await botmod.cmd_maintenance.callback(interaction2, "on")
        self.assertFalse(await maintenance.is_on())
        self.assertIn("Maintenance mode OFF",
                      interaction2.response.sent[0][0])

    async def test_env_owner_id_works_without_guild(self):
        with patch.object(maintenance, "MAINTENANCE_OWNER_ID", 77):
            interaction = FakeInteraction(user_id=77, owner_id=42)
            interaction.guild = None
            await botmod.cmd_maintenance.callback(interaction, "off")
        self.assertTrue(await maintenance.is_on())

    async def test_tree_check_blocks_commands_during_maintenance(self):
        await maintenance.set_on(True)
        interaction = FakeInteraction(command_name="live")
        self.assertFalse(
            await botmod._tree_interaction_check(interaction))
        self.assertIn("maintenance", interaction.response.sent[0][0])
        # /maintenance itself is exempt, so the owner can end it.
        interaction2 = FakeInteraction(command_name="maintenance")
        self.assertTrue(
            await botmod._tree_interaction_check(interaction2))
        self.assertEqual(len(interaction2.response.sent), 0)
        # ...and /shutdown is exempt too.
        interaction3 = FakeInteraction(command_name="shutdown")
        self.assertTrue(
            await botmod._tree_interaction_check(interaction3))
        await maintenance.set_on(False)
        interaction4 = FakeInteraction(command_name="live")
        self.assertTrue(
            await botmod._tree_interaction_check(interaction4))
        self.assertEqual(len(interaction4.response.sent), 0)

    async def test_shutdown_requires_owner(self):
        interaction = FakeInteraction(user_id=99, owner_id=42)
        await botmod.cmd_shutdown.callback(interaction)
        self.assertIn("Only the server owner",
                      interaction.response.sent[0][0])

    async def test_shutdown_confirm_schedules_close(self):
        view = botmod.ShutdownConfirmView()
        interaction = FakeInteraction(user_id=42, owner_id=42)
        with patch.object(botmod, "_schedule_shutdown") as mock_sched:
            await view.children[0].callback(interaction)
        mock_sched.assert_called_once()
        self.assertEqual(mock_sched.call_args.args[0],
                         interaction.client)
        self.assertIn("Shutting down",
                      interaction.response.sent[0][0])
        # Cancel does not schedule anything.
        interaction2 = FakeInteraction(user_id=42, owner_id=42)
        with patch.object(botmod, "_schedule_shutdown") as mock_sched:
            await view.children[1].callback(interaction2)
        mock_sched.assert_not_called()
        self.assertIn("cancelled", interaction2.response.sent[0][0])

    async def test_schedule_shutdown_exits_zero(self):
        client = FakeBot(FakeChannel())
        with patch("os._exit") as mock_exit:
            botmod._schedule_shutdown(client, delay=0)
            for _ in range(10):
                await asyncio.sleep(0)
        self.assertTrue(client.closed)
        mock_exit.assert_called_once_with(0)


class TestMaintenanceScoreboardLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._orig_state_path = sb.STATE_PATH
        self._orig_state = dict(sb._state)
        self._orig_stopped = set(sb._stopped)
        self._orig_env = sb.SCOREBOARD_CHANNEL_ID
        self._orig_fetch = sb._fetch_all
        self._tmp = Path(__file__).resolve().parent / ".maint-test-tmp3"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        sb.STATE_PATH = self._tmp / "scoreboard.json"
        sb._state = {}
        sb._stopped = set()
        sb.SCOREBOARD_CHANNEL_ID = sb.TEST_CHANNEL_ID
        sb._fetch_all = lambda: asyncio.sleep(
            0, result=([], [], []))
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)

    async def asyncSetUp(self):
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        db._DB = self._orig_db
        sb.STATE_PATH = self._orig_state_path
        sb._state = self._orig_state
        sb._stopped = self._orig_stopped
        sb.SCOREBOARD_CHANNEL_ID = self._orig_env
        sb._fetch_all = self._orig_fetch
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    async def _run_loop(self, seconds=0.25):
        orig_watch = sb.WATCH_SECONDS
        sb.WATCH_SECONDS = 0.05
        try:
            task = asyncio.create_task(sb.scoreboard_loop(self.bot))
            await asyncio.sleep(seconds)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            sb.WATCH_SECONDS = orig_watch

    async def test_loop_silent_during_maintenance(self):
        await maintenance.set_on(True)
        await self._run_loop()
        self.assertEqual(len(self.channel.sent), 0)

    async def test_loop_posts_when_live(self):
        await self._run_loop()
        self.assertGreaterEqual(len(self.channel.sent), 1)


if __name__ == "__main__":
    unittest.main()
