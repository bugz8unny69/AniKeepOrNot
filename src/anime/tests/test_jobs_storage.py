"""Offline tests for independent NextGen jobs; no running worker or services."""
import os
from pathlib import Path
import tempfile
import unittest

from src.anime.storage.sqlite import (
    connect, initialize, enqueue_job, accept_event_and_job, transaction
)


class JobsStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(os.path.join(self.tmp.name, "jobs.sqlite"))
        initialize(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_intake_commits_event_and_first_job(self):
        event_id, job_id = accept_event_and_job(
            self.db, "autobrr", b"raw", kind="identity", payload={"release": "a"})
        record = self.db.execute(
            "SELECT event_id,created_by_job_id,state FROM jobs WHERE job_id=?",
            (job_id,)).fetchone()
        self.assertEqual(record, (event_id, None, "PENDING"))

    def test_independent_successor_has_only_provenance_reference(self):
        event, first = accept_event_and_job(
            self.db, "autobrr", b"raw", kind="identity", payload={})
        with transaction(self.db):
            successor = enqueue_job(
                self.db, component="identity", event_id=event, created_by_job_id=first,
                kind="next_stage", payload={"ready": True})
        self.assertEqual(self.db.execute(
            "SELECT created_by_job_id FROM jobs WHERE job_id=?",
            (successor,)).fetchone()[0], first)
        self.assertEqual(self.db.execute(
            "SELECT state FROM jobs WHERE job_id=?",
            (successor,)).fetchone()[0], "PENDING")

    def test_rollback_discards_successor(self):
        event, first = accept_event_and_job(
            self.db, "autobrr", b"raw", kind="identity", payload={})
        try:
            with transaction(self.db):
                enqueue_job(self.db, component="identity", event_id=event, created_by_job_id=first,
                            kind="next_stage", payload={})
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_unknown_job_source_rejected(self):
        with self.assertRaises(Exception):
            with transaction(self.db):
                enqueue_job(self.db, component="identity", created_by_job_id=999,
                            kind="identity", payload={})

    def test_versioned_sql_from_v1_to_v2_preserves_events(self):
        sql_dir = Path(__file__).resolve().parents[1] / "storage" / "sql"
        v1 = (sql_dir / "001_foundation.sql").read_text(encoding="utf-8")
        v2 = (sql_dir / "002_independent_jobs.sql").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(os.path.join(tmp, "upgrade.sqlite"))
            try:
                db.executescript(v1)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                db.execute("INSERT INTO webhook_events(source,raw_body,received_at_ms) VALUES ('autobrr', X'61', 1)")
                db.commit()
                db.executescript(v2)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM webhook_events").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM job_history").fetchone()[0], 0)
                with self.assertRaises(ValueError):
                    initialize(db)
                v3 = (sql_dir / "003_incoming_events.sql").read_text(encoding="utf-8")
                db.executescript(v3)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT count(*) FROM incoming_events").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                with self.assertRaises(ValueError):
                    initialize(db)
                db.executescript((sql_dir / "004_diagnostic_components.sql").read_text())
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
                try:
                    db.executescript((sql_dir / "005_job_statuses.sql").read_text())
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
                db.executescript((sql_dir / "006_api_diagnostics.sql").read_text())
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
                db.executescript((sql_dir / "007_worker_component.sql").read_text())
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
                db.executescript((sql_dir / "008_handler_history.sql").read_text())
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
                db.executescript((sql_dir / "009_provider_cache.sql").read_text())
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
                db.executescript((sql_dir / "010_release_verification.sql").read_text())
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
                db.executescript((sql_dir / "011_trial_windows.sql").read_text())
                db.executescript((sql_dir / "012_episode_reservations.sql").read_text())
                db.executescript((sql_dir / "013_acquisition_plans.sql").read_text())
                db.executescript((sql_dir / "014_acquisition_budgets.sql").read_text())
                db.executescript((sql_dir / "015_torrent_submissions.sql").read_text())
                db.executescript((sql_dir / "016_torrent_transfers.sql").read_text())
                db.executescript((sql_dir / "017_cancellation.sql").read_text())
                db.executescript((sql_dir / "018_management_actions.sql").read_text())
                db.executescript((sql_dir / "019_media_verifications.sql").read_text())
                db.executescript((sql_dir / "020_staging_publications.sql").read_text())
                db.executescript((sql_dir / "021_reject_decisions.sql").read_text())
                db.executescript((sql_dir / "022_keep_imports.sql").read_text())
                db.executescript((sql_dir / "023_reservation_release.sql").read_text())
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],23)
                from src.anime.storage.migrations import SCRIPTS as current_scripts
                for name in current_scripts[23:]:db.executescript((sql_dir/name).read_text())
                initialize(db)
            finally:
                db.close()

    def test_job_history_is_linked_and_validated(self):
        _, job = accept_event_and_job(self.db, "autobrr", b"raw", kind="identity", payload={})
        self.assertEqual(self.db.execute(
            "SELECT event_code FROM job_history WHERE job_id=?", (job,)).fetchone()[0], "CREATED")
        with self.assertRaises(Exception):
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO job_history(job_id,occurred_at_ms,event_code) VALUES(9999,2,'UNKNOWN')")

    def test_version_two_failure_rolls_back_tables_and_version(self):
        sql_dir = Path(__file__).resolve().parents[1] / "storage" / "sql"
        v1 = (sql_dir / "001_foundation.sql").read_text(encoding="utf-8")
        v2 = (sql_dir / "002_independent_jobs.sql").read_text(encoding="utf-8")
        # Inject a failure after table creation but before user_version changes.
        bad = v2.replace("PRAGMA user_version=2;", "SELECT * FROM deliberately_missing_table;\\nPRAGMA user_version=2;")
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(os.path.join(tmp, "rollback.sqlite"))
            try:
                db.executescript(v1)
                with self.assertRaises(Exception):
                    db.executescript(bad)
                db.rollback()
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertIsNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE name='jobs'").fetchone())
                self.assertIsNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE name='job_history'").fetchone())
            finally:
                db.close()

    def test_initialize_refuses_unversioned_existing_schema(self):
        self.db.execute("PRAGMA user_version=0")
        with self.assertRaises(ValueError):
            initialize(self.db)


if __name__ == "__main__":
    unittest.main()

