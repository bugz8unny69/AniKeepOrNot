"""Tests for series job storage and lease management."""
import logging
import sqlite3
import unittest

from src.anime.series_job.store import (
    Claim, LeaseLost, SeriesRow, add_torrent, claim, get_series, note, save, wake
)
from src.anime.storage.sqlite import initialize


class SeriesStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def test_wake_creates_new_series(self):
        series = wake(self.db, 16999, "Cyberpunk: Edgerunners", now=1000)
        self.assertEqual(series.anidb_id, 16999)
        self.assertEqual(series.title, "Cyberpunk: Edgerunners")
        self.assertEqual(series.state, "WORKING")
        self.assertIsNone(series.wake_at_ms)
        self.assertEqual(series.torrents_at_once, 1)
        self.assertEqual(series.torrents, [])
        self.assertEqual(series.episodes, {})
        self.assertEqual(series.recent, [])

        persisted = get_series(self.db, 16999)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.title, "Cyberpunk: Edgerunners")
        self.assertEqual(persisted.state, "WORKING")

    def test_wake_wakes_asleep_series_but_preserves_decided(self):
        # Create and put asleep
        wake(self.db, 100, "Show A", now=1000)
        self.db.execute("UPDATE series SET state='ASLEEP', wake_at_ms=5000 WHERE anidb_id=100")
        woken = wake(self.db, 100, "Show A Updated", now=2000)
        self.assertEqual(woken.state, "WORKING")
        self.assertIsNone(woken.wake_at_ms)

        # Decided series: KEPT
        self.db.execute("UPDATE series SET state='KEPT' WHERE anidb_id=100")
        woken_kept = wake(self.db, 100, "Show A Kept", now=3000)
        self.assertEqual(woken_kept.state, "KEPT")

        # Decided series: REJECTED
        self.db.execute("UPDATE series SET state='REJECTED' WHERE anidb_id=100")
        woken_rejected = wake(self.db, 100, "Show A Rejected", now=4000)
        self.assertEqual(woken_rejected.state, "REJECTED")
        # "Process anyway" on one of its releases reopens it.
        self.assertEqual(wake(self.db, 100, "Show A", now=5000, reopen=True).state, "WORKING")

    def test_claim_working_and_due_asleep_series(self):
        wake(self.db, 101, "Working Show", now=1000)
        claimed = claim(self.db, now=1000, lease_ms=60000)
        self.assertIsNotNone(claimed)
        claim_obj, series = claimed
        self.assertEqual(claim_obj.anidb_id, 101)
        self.assertEqual(claim_obj.expires_at_ms, 61000)
        self.assertEqual(series.state, "WORKING")
        self.assertEqual(series.lease_token, claim_obj.token)

        # Claim again while lease active: returns None
        self.assertIsNone(claim(self.db, now=2000))

        # Once lease expires: can be claimed again
        reclaimed = claim(self.db, now=62000, lease_ms=60000)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed[0].anidb_id, 101)

    def test_claim_asleep_series_due_vs_future(self):
        wake(self.db, 201, "Asleep Future", now=1000)
        self.db.execute("UPDATE series SET state='ASLEEP', wake_at_ms=50000 WHERE anidb_id=201")

        # Not due at now=20000
        self.assertIsNone(claim(self.db, now=20000))

        # Due at now=50000
        claimed = claim(self.db, now=50000, lease_ms=30000)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].anidb_id, 201)
        self.assertEqual(claimed[1].state, "WORKING")

    def test_claim_ignores_asleep_without_wake_time(self):
        wake(self.db, 301, "Asleep Indefinite", now=1000)
        self.db.execute("UPDATE series SET state='ASLEEP', wake_at_ms=NULL WHERE anidb_id=301")
        self.assertIsNone(claim(self.db, now=999999))

    def test_save_persists_changes_and_handles_lease_loss(self):
        wake(self.db, 401, "Test Show", now=1000)
        claim_obj, series = claim(self.db, now=1000, lease_ms=60000)

        series.title = "Test Show Modified"
        series.episodes["S01E01"] = {"state": "reserved", "torrent": "hash1", "file": None}
        save(self.db, claim_obj, series, now=2000)

        persisted = get_series(self.db, 401)
        self.assertEqual(persisted.title, "Test Show Modified")
        self.assertEqual(persisted.episodes["S01E01"]["state"], "reserved")

        # Lease lost: expiry reached
        with self.assertRaises(LeaseLost):
            save(self.db, claim_obj, series, now=65000)

        # Lease lost: another claim with different token
        stolen_claim = Claim(401, "wrong_token", 90000)
        with self.assertRaises(LeaseLost):
            save(self.db, stolen_claim, series, now=3000)

    def test_add_torrent_appends_and_is_noop_on_duplicate(self):
        wake(self.db, 501, "Show Torrent", now=1000)
        torrent1 = {
            "infohash": "aabbccddeeff00112233445566778899aabbccdd",
            "release_name": "[SubsPlease] Show - 01 (1080p).mkv",
            "raw_body": '{"torrent_infohash": "aabbccddeeff00112233445566778899aabbccdd"}',
        }
        added = add_torrent(self.db, 501, torrent1, now=1000)
        self.assertTrue(added)

        row = get_series(self.db, 501)
        self.assertEqual(len(row.torrents), 1)
        self.assertEqual(row.torrents[0]["infohash"], "aabbccddeeff00112233445566778899aabbccdd")
        self.assertEqual(row.torrents[0]["step"], "check_release")
        self.assertEqual(row.torrents[0]["state"], "waiting")

        # Adding same infohash again is a no-op
        duplicate = add_torrent(self.db, 501, torrent1, now=2000)
        self.assertFalse(duplicate)

        row_after = get_series(self.db, 501)
        self.assertEqual(len(row_after.torrents), 1)

    def test_note_caps_at_50_lines_and_logs(self):
        row = SeriesRow(
            anidb_id=601,
            title="Logging Show",
            state="WORKING",
            wake_at_ms=None,
            torrents_at_once=1,
        )
        custom_logger = logging.getLogger("test.series")
        with self.assertLogs("test.series", level="INFO") as captured:
            for i in range(60):
                note(row, f"line {i}", custom_logger=custom_logger)

        self.assertEqual(len(row.recent), 50)
        self.assertEqual(row.recent[0], "line 10")
        self.assertEqual(row.recent[-1], "line 59")
        self.assertEqual(len(captured.output), 60)


if __name__ == "__main__":
    unittest.main()
