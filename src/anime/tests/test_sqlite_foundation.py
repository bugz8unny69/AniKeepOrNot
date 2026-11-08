"""Offline tests for the NextGen SQLite foundation; never touches production."""
import os
import sqlite3
import tempfile
import unittest

from src.anime.storage.sqlite import connect, initialize, accept_event, save_identity


class SQLiteFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.sqlite")
        self.db = connect(self.path)
        initialize(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def candidate(self, episodes=None):
        return {"schema_version": 2, "anime": {
            "anidb_id": 123, "tvdb_series_id": 456, "title": "Test Anime"},
            "episodes": [] if episodes is None else episodes}

    def test_every_duplicate_delivery_has_independent_trace_and_history(self):
        a = accept_event(self.db, "autobrr", b"same")
        b = accept_event(self.db, "autobrr", b"same")
        self.assertNotEqual(a, b)
        self.assertEqual(self.db.execute("SELECT count(*) FROM incoming_events").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM event_history").fetchone()[0], 2)

    def test_pending_work_survives_database_reopen(self):
        event = accept_event(self.db, "autobrr", b"payload")
        self.db.close()
        self.db = connect(self.path)
        self.assertEqual(self.db.execute("SELECT state FROM incoming_events WHERE event_id=?",
                                         (event,)).fetchone()[0], "PENDING")

    def test_valid_series_without_episodes(self):
        event = accept_event(self.db, "autobrr", b"batch")
        save_identity(self.db, event, self.candidate())
        self.assertEqual(self.db.execute("SELECT count(*) FROM identity_results").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM identified_episodes").fetchone()[0], 0)

    def test_verified_episode_links_reused_across_events(self):
        candidate = self.candidate([{"anidb_episode_id": 987}])
        for _ in range(2):
            save_identity(self.db, accept_event(self.db, "autobrr", b"episode"), candidate)
        self.assertEqual(self.db.execute("SELECT count(*) FROM identified_episodes").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM result_episode_links").fetchone()[0], 2)

    def test_invalid_identity_keeps_original_event(self):
        event = accept_event(self.db, "autobrr", b"invalid")
        candidate = self.candidate([{"anidb_episode_id": -1}])
        with self.assertRaises(ValueError):
            save_identity(self.db, event, candidate)
        self.assertEqual(self.db.execute("SELECT count(*) FROM identity_results").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT state FROM incoming_events WHERE event_id=?",
                                         (event,)).fetchone()[0], "PENDING")

    def test_foreign_keys_reject_unknown_event(self):
        with self.assertRaises(ValueError):
            save_identity(self.db, 999, self.candidate())
        self.assertEqual(self.db.execute("SELECT count(*) FROM identified_anime").fetchone()[0], 0)

    def test_success_updates_event_state(self):
        event = accept_event(self.db, "autobrr", b"payload")
        save_identity(self.db, event, self.candidate())
        self.assertEqual(self.db.execute("SELECT state FROM incoming_events WHERE event_id=?",
                                         (event,)).fetchone()[0], "IDENTIFIED")

    def test_identical_retry_is_idempotent(self):
        event = accept_event(self.db, "autobrr", b"payload")
        first = save_identity(self.db, event, self.candidate())
        again = save_identity(self.db, event, self.candidate())
        self.assertEqual(first, again)
        self.assertEqual(self.db.execute("SELECT count(*) FROM identity_results").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM event_history WHERE event_code='SERIES_IDENTIFIED'").fetchone()[0], 1)

    def test_metadata_correction_keeps_old_snapshot(self):
        first = accept_event(self.db, "autobrr", b"a")
        save_identity(self.db, first, self.candidate())
        second = accept_event(self.db, "autobrr", b"b")
        revised = self.candidate()
        revised["anime"]["title"] = "Corrected Anime"
        save_identity(self.db, second, revised)
        self.assertEqual(self.db.execute("SELECT title FROM identified_anime").fetchone()[0], "Corrected Anime")
        self.assertIn("Test Anime", self.db.execute(
            "SELECT series_json FROM identity_results WHERE event_id=?", (first,)).fetchone()[0])

    def test_conflicting_tvdb_identity_is_rejected_atomically(self):
        save_identity(self.db, accept_event(self.db, "autobrr", b"a"), self.candidate())
        event = accept_event(self.db, "autobrr", b"b")
        conflict = self.candidate()
        conflict["anime"]["tvdb_series_id"] = 999
        with self.assertRaises(ValueError):
            save_identity(self.db, event, conflict)
        self.assertEqual(self.db.execute("SELECT count(*) FROM identity_results").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT state FROM incoming_events WHERE event_id=?",
                                         (event,)).fetchone()[0], "PENDING")


if __name__ == "__main__":
    unittest.main()
