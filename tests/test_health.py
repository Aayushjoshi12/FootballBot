"""Health monitor (Feature 7): snapshot status levels,
stale_check, log_recovery, never-raises behaviour.

No Discord token, no network — tmp db, mocked bot, monkeypatched
scoreboard / provider timestamps.
Run:  python3 -m pytest tests/test_health.py -q
from the footballbot directory.
"""
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import health
import providers as prov
import scoreboard


class FakeBot:
    def __init__(self, latency=0.05, start_time=None):
        self.latency = latency
        self.start_time = start_time
        self.scoreboard_task = None
        self.barcelona_task = None


class TestHealth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db = db._DB
        self._tmp = Path(__file__).resolve().parent / ".health-test-tmp"
        self._tmp.mkdir(exist_ok=True)
        db._DB = str(self._tmp / "test.db")
        self._orig_tick = scoreboard.last_tick_ts
        self._orig_good_tick = scoreboard.last_good_tick_ts
        self._orig_live_ts = prov._live_cache.get("ts")

    async def asyncSetUp(self):
        await db.init()
        scoreboard.last_tick_ts = time.monotonic()
        scoreboard.last_good_tick_ts = time.monotonic()
        prov._live_cache["ts"] = time.monotonic()

    async def asyncTearDown(self):
        await db.close()
        db._DB = self._orig_db
        scoreboard.last_tick_ts = self._orig_tick
        scoreboard.last_good_tick_ts = self._orig_good_tick
        prov._live_cache["ts"] = self._orig_live_ts
        for p in self._tmp.glob("*"):
            p.unlink()
        self._tmp.rmdir()

    def _fresh_bot(self):
        return FakeBot(start_time=datetime.now(timezone.utc))

    async def test_snapshot_green_with_fresh_timestamps(self):
        text = await health.snapshot(self._fresh_bot())
        self.assertIn("🟢", text)
        self.assertIn("healthy", text)
        self.assertIn("50 ms", text)
        self.assertIn("Live scores data:", text)
        self.assertIn("Watches:", text)
        self.assertIn("Barcelona reminder:", text)
        self.assertIn("Recovery attempts:", text)
        self.assertNotIn("1xBet", text)

    async def test_snapshot_red_when_last_tick_old(self):
        scoreboard.last_tick_ts = time.monotonic() - 120
        text = await health.snapshot(self._fresh_bot())
        self.assertIn("🔴", text)
        self.assertIn("unhealthy", text)

    async def test_snapshot_red_when_tick_never_happened(self):
        scoreboard.last_tick_ts = 0.0
        text = await health.snapshot(self._fresh_bot())
        self.assertIn("🔴", text)
        self.assertIn("has not ticked yet", text)

    async def test_snapshot_red_when_latency_none(self):
        text = await health.snapshot(FakeBot(latency=None))
        self.assertIn("🔴", text)
        self.assertIn("not connected", text)

    async def test_snapshot_red_when_provider_never_succeeded(self):
        prov._live_cache["ts"] = 0.0
        text = await health.snapshot(self._fresh_bot())
        self.assertIn("🔴", text)
        self.assertIn("never", text)

    async def test_snapshot_yellow_when_tick_slightly_old(self):
        scoreboard.last_tick_ts = time.monotonic() - 15
        text = await health.snapshot(self._fresh_bot())
        self.assertIn("🟡", text)
        self.assertIn("degraded", text)

    async def test_snapshot_yellow_on_recent_warning(self):
        await db.log_health("scoreboard", "warning",
                            "no live data for over 60s")
        text = await health.snapshot(self._fresh_bot())
        self.assertIn("🟡", text)
        self.assertIn("no live data for over 60s", text)

    async def test_snapshot_uptime_unknown_without_start_time(self):
        text = await health.snapshot(FakeBot())
        self.assertIn("unknown — started before this check was added", text)

    async def test_snapshot_shows_recent_health_entries(self):
        await db.log_health("scoreboard", "recovery", "tick cache reset")
        text = await health.snapshot(self._fresh_bot())
        self.assertIn("tick cache reset", text)
        self.assertIn("recovery", text)

    async def test_snapshot_counts_recoveries(self):
        await db.log_health("scoreboard", "warning", "stale episode")
        await health.log_recovery("scoreboard", "worker restarted",
                                  {"task": "scoreboard"})
        text = await health.snapshot(self._fresh_bot())
        self.assertIn("1 warning(s), 0 error(s), 1 recoveries", text)

    async def test_snapshot_never_raises_with_uninitialised_db(self):
        await db.close()
        try:
            text = await health.snapshot(self._fresh_bot())
        finally:
            await db.init()
        self.assertIsInstance(text, str)
        self.assertIn("🟢", text)  # fresh ticks still count
        self.assertIn("unknown", text)  # db-backed sections degrade

    async def test_log_recovery_writes_row(self):
        await health.log_recovery("scoreboard", "worker restarted",
                                  {"task": "scoreboard"})
        rows = await db.get_recent_health(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "recovery")
        self.assertEqual(rows[0]["component"], "scoreboard")
        self.assertIn("worker restarted", rows[0]["detail"])
        self.assertIn("scoreboard", rows[0]["detail"])

    async def test_log_recovery_swallows_db_errors(self):
        await db.close()
        try:
            await health.log_recovery("scoreboard", "worker restarted")
        finally:
            await db.init()
        # No exception — and the db is usable again afterwards.
        self.assertEqual(await db.get_recent_health(limit=5), [])

    async def test_stale_check_none_when_fresh(self):
        with patch.object(prov, "onex_last_success_ts",
                          return_value=time.monotonic()):
            self.assertIsNone(await health.stale_check())

    async def test_stale_check_warns_when_old(self):
        with patch.object(prov, "onex_last_success_ts",
                          return_value=time.monotonic() - 120):
            warning = await health.stale_check()
        self.assertIsNotNone(warning)
        self.assertIn("stale", warning)

    async def test_stale_check_warns_when_never(self):
        with patch.object(prov, "onex_last_success_ts", return_value=0.0):
            warning = await health.stale_check()
        self.assertIsNotNone(warning)

    async def test_stale_check_boundary_60s(self):
        with patch.object(prov, "onex_last_success_ts",
                          return_value=time.monotonic() - 30):
            self.assertIsNone(await health.stale_check())


if __name__ == "__main__":
    unittest.main()
