"""history.py: event dedup, match/notification records, stat
snapshots + momentum estimate, retention cleanup. No network —
a throwaway sqlite db per test, like tests/test_watch.py.
"""
import time
import unittest
from pathlib import Path

import db
import history


class TestHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".history-test-tmp"
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

    async def test_event_dedup_round_trip(self):
        self.assertFalse(await history.event_seen("k1"))
        self.assertTrue(await history.record_sent_event(
            "k1", "111", 777, "goal", "GOAL"))
        self.assertTrue(await history.event_seen("k1"))
        # Recording the same key again is a duplicate.
        self.assertFalse(await history.record_sent_event(
            "k1", "111", 777, "goal", "GOAL"))
        self.assertFalse(await history.event_seen("other-key"))

    async def test_match_results_listed_newest_first(self):
        await history.record_match_result(
            "111", "Home A", "Away A", "League", "Football", 2, 1, "FT")
        await history.record_match_result(
            "222", "Home B", "Away B", "League", "Football", 0, 0, "FT")
        matches = await history.recent_matches(limit=10)
        self.assertEqual(len(matches), 2)
        by_id = {m["game_id"]: m for m in matches}
        self.assertEqual(set(by_id), {"111", "222"})
        self.assertEqual(by_id["111"]["final_home"], "2")
        self.assertEqual(by_id["222"]["final_home"], "0")

    async def test_notification_log(self):
        await history.record_notification(
            "111", 777, "goal", "GOAL — Home 1–0 Away", True)
        notes = await history.recent_notifications(limit=10)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "goal")
        self.assertTrue(notes[0]["delivered"])

    async def test_momentum_needs_two_snapshots(self):
        self.assertIsNone(await history.momentum_summary("111"))
        await history.record_stat_snapshot(
            "111", {"Attacks": ("47", "36")})
        self.assertIsNone(await history.momentum_summary("111"))

    async def test_momentum_from_real_snapshots(self):
        # Distinct timestamps: two samples in the same second
        # would collapse into one row (PRIMARY KEY game_id, ts).
        now = int(time.time())
        await db.record_snapshot(
            "111", now - 60,
            {"Attacks": ("47", "36"), "Possession %": ("55", "45")})
        await db.record_snapshot(
            "111", now,
            {"Attacks": ("60", "38"), "Possession %": ("58", "42")})
        line = await history.momentum_summary("111")
        self.assertIsNotNone(line)
        self.assertIn("Momentum (estimate", line)
        self.assertIn("home side", line)
        self.assertIn("possession now 58%–42%", line)

    async def test_momentum_none_when_nothing_changed(self):
        now = int(time.time())
        stats = {"Attacks": ("47", "36")}
        await db.record_snapshot("111", now - 60, stats)
        await db.record_snapshot("111", now, stats)
        self.assertIsNone(await history.momentum_summary("111"))

    async def test_cleanup_removes_only_old_history(self):
        import aiosqlite
        # A live watch + prefs + a fresh match must survive.
        await db.add_watch(777, "999", "Live Home", "Live Away",
                           "League", "simple", "", None)
        await db.set_prefs(777, "999", events=["goal"])
        await history.record_match_result(
            "fresh", "Fresh H", "Fresh A", "League", "Football", 1, 0, "FT")
        # Age some rows 40 days by hand.
        old_ts = int(time.time()) - 40 * 86400
        async with aiosqlite.connect(db._DB) as conn:
            await conn.execute(
                "INSERT INTO matches (game_id, home, away, league, sport,"
                " final_home, final_away, status, ended_ts)"
                " VALUES ('old','Old H','Old A','League','Football',"
                " 0, 0, 'FT', ?)", (old_ts,))
            await conn.execute(
                "INSERT INTO match_events (event_key, game_id, channel_id,"
                " kind, text, created_ts) VALUES ('old-key','old',777,"
                " 'goal','GOAL', ?)", (old_ts,))
            await conn.execute(
                "INSERT INTO notification_log (game_id, channel_id, kind,"
                " text, delivered, sent_ts) VALUES ('old',777,'goal',"
                " 'GOAL', 1, ?)", (old_ts,))
            await conn.commit()
        removed = await history.cleanup_old_history(keep_days=30)
        self.assertEqual(removed.get("matches"), 1)
        self.assertEqual(removed.get("match_events"), 1)
        self.assertEqual(removed.get("notification_log"), 1)
        matches = await history.recent_matches(limit=10)
        self.assertEqual([m["game_id"] for m in matches], ["fresh"])
        # Active state untouched.
        self.assertEqual(len(await db.list_watches(777)), 1)
        self.assertIsNotNone(await db.get_prefs(777, "999"))

    async def test_snapshot_pruning(self):
        now = int(time.time())
        await db.record_snapshot("111", now - 3 * 3600,
                                 {"Attacks": ("10", "10")})
        await db.record_snapshot("111", now, {"Attacks": ("20", "10")})
        await db.prune_snapshots("111", now - 2 * 3600)
        snaps = await db.get_snapshots("111", 0)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["ts"], now)
