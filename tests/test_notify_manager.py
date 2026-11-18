"""Notification manager panel (Feature 5): view rendering,
owner checks, pause/detail/unfollow behaviour, event prefs.

No Discord token, no network — fake interactions, tmp db.
Run:  python3 -m pytest tests/test_notify_manager.py -q
from the footballbot directory.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import notify_manager as nm
import watch as wmod
from watch import EVENT_KINDS

OWNER_ID = 42
OTHER_ID = 99
CHANNEL_ID = 777
GAME_ID = "771747666"
WATCH_ROW = {"game_id": GAME_ID, "home": "Gambia", "away": "Ivory Coast",
             "league": "AFCON", "detail": "simple",
             "snapshot": {"home_score": "0", "away_score": "1"}}


class FakeResponse:
    def __init__(self):
        self.sent: list[str] = []
        self.deferred = False
        self.edits: list = []

    async def defer(self, ephemeral=False, thinking=False, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, ephemeral=False,
                           view=None, **kwargs):
        self.sent.append(content)

    async def edit_message(self, content=None, view=None, **kwargs):
        self.edits.append((content, view))


class FakeFollowup:
    def __init__(self):
        self.sent: list[str] = []
        self.sent_views: list = []

    async def send(self, content=None, ephemeral=False, view=None, **kwargs):
        self.sent.append(content)
        self.sent_views.append(view)


class FakeInteraction:
    def __init__(self, user_id=OWNER_ID, channel_id=CHANNEL_ID, data=None):
        self.channel_id = channel_id
        self.channel = SimpleNamespace(id=channel_id)
        self.user = SimpleNamespace(id=user_id)
        self.data = data
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _view(watches=None, prefs=None):
    return nm.NotifyManagerView(OWNER_ID, CHANNEL_ID,
                                watches if watches is not None else [WATCH_ROW],
                                prefs or {})


class TestEventLabels(unittest.TestCase):
    def test_covers_every_real_event_kind(self):
        self.assertEqual(set(nm.EVENT_LABELS), set(EVENT_KINDS))

    def test_no_second_yellow_or_var_promised(self):
        self.assertNotIn("second_yellow", nm.EVENT_LABELS)
        self.assertNotIn("var", nm.EVENT_LABELS)
        self.assertNotIn("red_card", nm.EVENT_LABELS)
        self.assertNotIn("yellow_card", nm.EVENT_LABELS)

    def test_no_provider_name_in_visible_strings(self):
        for name in ("notify_manager.py", "health.py"):
            text = (Path(__file__).resolve().parent.parent / name).read_text()
            self.assertNotIn("1xBet", text, name)


class TestViewRenders(unittest.IsolatedAsyncioTestCase):
    # Async: discord.py's View.__init__ needs a running loop.
    async def test_no_watches_renders_disabled_option(self):
        view = nm.NotifyManagerView(OWNER_ID, CHANNEL_ID, [])
        self.assertEqual(len(view.children), 5)
        game_select = view.children[0]
        self.assertEqual(len(game_select.options), 1)
        self.assertEqual(game_select.options[0].label,
                         "No games watched in this channel")
        self.assertTrue(game_select.disabled)
        self.assertEqual(game_select.placeholder, "Pick a game to manage")

    async def test_with_watches_lists_home_vs_away(self):
        view = _view()
        game_select = view.children[0]
        self.assertEqual(len(game_select.options), 1)
        self.assertEqual(game_select.options[0].label,
                         "Gambia vs Ivory Coast")
        self.assertEqual(game_select.options[0].value, GAME_ID)

    async def test_event_select_allows_empty_and_all_kinds(self):
        view = _view()
        events_select = view.children[1]
        self.assertEqual(events_select.min_values, 0)
        self.assertEqual(events_select.max_values, len(EVENT_KINDS))
        self.assertEqual(
            {o.value for o in events_select.options}, set(EVENT_KINDS))

    async def test_persistent_view(self):
        view = _view()
        self.assertIsNone(view.timeout)
        self.assertEqual(
            [c.custom_id for c in view.children],
            ["notify:game-select", "notify:events-select", "notify:pause",
             "notify:detail", "notify:unfollow"])


class TestNotifyManagerDb(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".notify-test-tmp"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_watches = dict(wmod._watches)
        self._orig_loaded = wmod._loaded
        wmod._watches = {}
        wmod._loaded = True

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

    async def _seed_watch(self):
        await db.add_watch(CHANNEL_ID, GAME_ID, "Gambia", "Ivory Coast",
                           "AFCON", "simple", "",
                           {"home_score": "0", "away_score": "1"})
        wmod._watches[(CHANNEL_ID, GAME_ID)] = {
            "home": "Gambia", "away": "Ivory Coast", "league": "AFCON",
            "detail": "simple", "fmt": "", "snapshot": {}, "missing": 0,
        }

    def _select_game(self, view, interaction):
        interaction.data = {"values": [GAME_ID]}
        return view.children[0].callback(interaction)

    async def test_game_select_sets_selection(self):
        view = _view()
        interaction = FakeInteraction()
        await self._select_game(view, interaction)
        self.assertEqual(view.selected_game_id, GAME_ID)
        self.assertTrue(any("Gambia vs Ivory Coast" in s
                            for s in interaction.response.sent))

    async def test_non_owner_blocked_everywhere(self):
        view = _view()
        stranger = FakeInteraction(user_id=OTHER_ID)
        stranger.data = {"values": [GAME_ID]}
        await view.children[0].callback(stranger)
        await view.children[2].callback(stranger)
        await view.children[3].callback(stranger)
        await view.children[4].callback(stranger)
        self.assertTrue(all(
            "These settings are for the person who opened them." in s
            for s in stranger.response.sent))
        self.assertEqual(len(stranger.response.sent), 4)
        self.assertIsNone(await db.get_prefs(CHANNEL_ID, GAME_ID))
        self.assertIsNone(view.selected_game_id)

    async def test_buttons_without_game_say_pick_first(self):
        view = _view()
        interaction = FakeInteraction()
        await view.children[2].callback(interaction)  # Pause
        await view.children[3].callback(interaction)  # Detail
        await view.children[4].callback(interaction)  # Unfollow
        self.assertEqual(interaction.response.sent,
                         ["Pick a game first."] * 3)

    async def test_owner_pause_toggles_pref(self):
        await self._seed_watch()
        view = _view()
        interaction = FakeInteraction()
        await self._select_game(view, interaction)
        await view.children[2].callback(interaction)  # Pause
        prefs = await db.get_prefs(CHANNEL_ID, GAME_ID)
        self.assertEqual(prefs["paused"], 1)
        self.assertEqual(view.pause_button.label, "▶ Resume")
        await view.children[2].callback(interaction)  # Resume
        prefs = await db.get_prefs(CHANNEL_ID, GAME_ID)
        self.assertEqual(prefs["paused"], 0)
        self.assertEqual(view.pause_button.label, "⏸ Pause")

    async def test_detail_toggle_sets_pref(self):
        await self._seed_watch()
        view = _view()
        interaction = FakeInteraction()
        await self._select_game(view, interaction)
        await view.children[3].callback(interaction)  # -> advanced
        prefs = await db.get_prefs(CHANNEL_ID, GAME_ID)
        self.assertEqual(prefs["detail"], "advanced")
        self.assertEqual(wmod._watches[(CHANNEL_ID, GAME_ID)]["detail"],
                         "advanced")
        await view.children[3].callback(interaction)  # -> simple
        prefs = await db.get_prefs(CHANNEL_ID, GAME_ID)
        self.assertEqual(prefs["detail"], "simple")

    async def test_event_multiselect_stores_only_valid_kinds(self):
        await self._seed_watch()
        view = _view()
        interaction = FakeInteraction()
        await self._select_game(view, interaction)
        interaction.data = {"values": ["goal", "bogus_kind", "red"]}
        await view.children[1].callback(interaction)
        prefs = await db.get_prefs(CHANNEL_ID, GAME_ID)
        self.assertEqual(prefs["events"], ["goal", "red"])

    async def test_event_multiselect_empty_means_all(self):
        await self._seed_watch()
        await db.set_prefs(CHANNEL_ID, GAME_ID, events=["goal"])
        view = _view()
        interaction = FakeInteraction()
        await self._select_game(view, interaction)
        interaction.data = {"values": []}
        await view.children[1].callback(interaction)
        prefs = await db.get_prefs(CHANNEL_ID, GAME_ID)
        self.assertEqual(prefs["events"], [])
        self.assertTrue(any("all events" in s
                            for s in interaction.response.sent))

    async def test_unfollow_keeps_panel_stops_alerts(self):
        await self._seed_watch()
        await db.set_prefs(CHANNEL_ID, GAME_ID, events=["goal"])
        view = _view()
        interaction = FakeInteraction()
        await self._select_game(view, interaction)
        await view.children[4].callback(interaction)
        # The panel (and its db row) stays — only the alerts stop.
        self.assertIn((CHANNEL_ID, GAME_ID), wmod._watches)
        self.assertFalse(wmod._watches[(CHANNEL_ID, GAME_ID)]["notify"])
        rows = await db.list_watches(CHANNEL_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["notify"], 0)
        self.assertIsNone(await db.get_prefs(CHANNEL_ID, GAME_ID))
        self.assertTrue(any("Notifications off" in s
                            for s in interaction.response.sent))
        self.assertTrue(any("live panel keeps ticking" in s
                            for s in interaction.response.sent))
        # The game select now shows the empty state again — a
        # view-only panel is not a watched game.
        self.assertEqual(view.children[0].options[0].label,
                         "No games watched in this channel")

    async def test_show_notifications_lists_watches_follows_prefs(self):
        await self._seed_watch()
        await db.set_prefs(CHANNEL_ID, GAME_ID, events=["goal"],
                           paused=True)
        await db.add_team_follow(OWNER_ID, "lumbini lions",
                                 "Lumbini Lions", CHANNEL_ID, "simple")
        await db.add_team_follow(OWNER_ID, "pokhara avengers",
                                 "Pokhara Avengers", 999, "simple")
        interaction = FakeInteraction()
        await nm.show_notifications(interaction)
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.followup.sent), 1)
        text = interaction.followup.sent[0]
        self.assertIn("Gambia vs Ivory Coast", text)
        self.assertIn("paused", text)
        self.assertIn("Lumbini Lions", text)
        self.assertNotIn("Pokhara Avengers", text)
        self.assertIn("unfollow <team>", text)
        view = interaction.followup.sent_views[0]
        self.assertIsInstance(view, nm.NotifyManagerView)
        self.assertEqual(view.owner_id, OWNER_ID)

    async def test_show_notifications_empty_channel(self):
        interaction = FakeInteraction()
        await nm.show_notifications(interaction)
        text = interaction.followup.sent[0]
        self.assertIn("No games watched in this channel", text)
        view = interaction.followup.sent_views[0]
        self.assertEqual(view.children[0].options[0].label,
                         "No games watched in this channel")


if __name__ == "__main__":
    unittest.main()
