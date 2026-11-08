"""Job state/activity schema and migration regression cases; intentionally unrun."""
import io
import logging
from pathlib import Path
import sqlite3
import unittest

from src.anime.diagnostics import configure_logging, log_history
from src.anime.history import append_job_history
from src.anime.messages import DEFAULT_CATALOG, JOB_ACTIVITIES, STATES, validate_job_activity
from src.anime.settings import Settings
from src.anime.storage.sqlite import accept_event_and_job, initialize, transaction

SQL_DIR = Path(__file__).resolve().parents[1] / "storage" / "sql"


def upgrade_v5(db, text=None):
    # Mirrors the documented script caller contract, not a production runner.
    try:
        db.executescript(text if text is not None else (SQL_DIR / "005_job_statuses.sql").read_text())
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


class JobStatusTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)
        self.event, self.job = accept_event_and_job(
            self.db, "test", b"private input", kind="identity", payload={})

    def tearDown(self):
        self.db.close()

    def test_all_lifecycle_states_are_persistable_with_readable_labels(self):
        self.assertEqual(set(STATES["job"]), {
            "PENDING", "RUNNING", "RETRY_WAIT", "ON_HOLD", "NEEDS_REVIEW",
            "DONE", "BLOCKED", "CANCELED", "FAILED"})
        for state in STATES["job"]:
            with self.subTest(state=state), transaction(self.db):
                self.db.execute(
                    "UPDATE jobs SET state=?,lease_token=?,lease_expires_at_ms=? WHERE job_id=?",
                    (state, "test-token" if state == "RUNNING" else None,
                     100 if state == "RUNNING" else None, self.job))
                self.assertNotEqual(DEFAULT_CATALOG.job_status(state)["message"], "Unknown status")
        self.assertEqual(DEFAULT_CATALOG.state("incoming_event", "PENDING")["message"], "Received")
        self.assertEqual(DEFAULT_CATALOG.state("incoming_event", "RUNNING")["message"], "Identifying")

    def test_valid_activity_pairs_and_invalid_pairs_agree_between_sql_and_python(self):
        for state in STATES["job"]:
            for activity in (None, *JOB_ACTIVITIES):
                valid = (
                    activity is None
                    or (activity == "DOWNLOADING" and state in {"RUNNING", "RETRY_WAIT"})
                    or (activity in {"WAITING_FOR_DOWNLOAD", "WAITING_FOR_SEARCH"} and state == "RETRY_WAIT")
                    or (activity == "SKIPPED_DUPLICATE" and state == "DONE")
                )
                with self.subTest(state=state, activity=activity):
                    if valid:
                        validate_job_activity(state, activity)
                        with transaction(self.db):
                            self.db.execute(
                                "UPDATE jobs SET state=?,activity_code=?,lease_token=?,"
                                "lease_expires_at_ms=? WHERE job_id=?",
                                (state, activity, "token" if state == "RUNNING" else None,
                                 100 if state == "RUNNING" else None, self.job))
                    else:
                        with self.assertRaises(ValueError):
                            validate_job_activity(state, activity)
                        with self.assertRaises(sqlite3.IntegrityError), transaction(self.db):
                            self.db.execute("UPDATE jobs SET state=?,activity_code=? WHERE job_id=?",
                                            (state, activity, self.job))
        with self.assertRaises(ValueError):
            validate_job_activity("DONE", "raw-secret")
        with self.assertRaises(sqlite3.IntegrityError), transaction(self.db):
            self.db.execute("UPDATE jobs SET activity_code='UNKNOWN' WHERE job_id=?", (self.job,))

    def test_display_selection_never_masks_a_terminal_or_held_state(self):
        self.assertEqual(DEFAULT_CATALOG.job_status("DONE", "SKIPPED_DUPLICATE")["message"],
                         "Skipped duplicate")
        self.assertEqual(DEFAULT_CATALOG.job_status("RETRY_WAIT", "DOWNLOADING")["message"], "Downloading")
        for state in ("ON_HOLD", "BLOCKED", "CANCELED", "NEEDS_REVIEW"):
            self.assertEqual(DEFAULT_CATALOG.job_status(state, "DOWNLOADING"),
                             DEFAULT_CATALOG.state("job", state))
        self.assertEqual(DEFAULT_CATALOG.job_status("unknown", "DOWNLOADING")["message"], "Unknown status")

    def test_activity_is_a_durable_snapshot_with_readable_journal_output(self):
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET state='RETRY_WAIT',attempt_count=1,"
                            "activity_code='WAITING_FOR_DOWNLOAD' WHERE job_id=?", (self.job,))
            receipt = append_job_history(
                self.db, job_id=self.job, event_code="DEFERRED", reason_code="TORRENT_DOWNLOAD_WAITING",
                component="acquisition", occurred_at_ms=10, details={"attempt": 1})
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET state='DONE',activity_code='SKIPPED_DUPLICATE' WHERE job_id=?",
                            (self.job,))
            append_job_history(self.db, job_id=self.job, event_code="DONE",
                               component="acquisition", occurred_at_ms=11, details={"attempt": 1})
        self.assertEqual(self.db.execute(
            "SELECT activity_code FROM job_history WHERE job_id=? ORDER BY history_id",
            (self.job,)).fetchall(), [(None,), ("WAITING_FOR_DOWNLOAD",), ("SKIPPED_DUPLICATE",)])
        stream = io.StringIO()
        logger = logging.Logger("test.activity")
        logger.addHandler(logging.StreamHandler(stream))
        configure_logging(Settings(), "acquisition", logger=logger)
        log_history(logger, receipt)
        self.assertIn("Waiting for download", stream.getvalue())
        self.assertIn("Waiting for download", stream.getvalue())

    def test_initial_job_can_be_held_blocked_or_canceled_without_an_attempt(self):
        for state in ("ON_HOLD", "BLOCKED", "CANCELED"):
            with self.subTest(state=state), transaction(self.db):
                self.db.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, self.job))
                append_job_history(
                    self.db, job_id=self.job, event_code=state, component="worker",
                    occurred_at_ms=2, reason_code="UNSUPPORTED_JOB_KIND", details={"attempt": 0})
        with self.assertRaises(ValueError), transaction(self.db):
            append_job_history(self.db, job_id=self.job, event_code="CANCELED",
                               component="worker", occurred_at_ms=3, details={"attempt": 0})


class JobStatusMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        for filename in ("001_foundation.sql", "002_independent_jobs.sql",
                         "003_incoming_events.sql", "004_diagnostic_components.sql"):
            self.db.executescript((SQL_DIR / filename).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events VALUES(1,'test',X'61',1,'PENDING',0,NULL,NULL,NULL,NULL);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,attempt_count,
                             lease_token,lease_expires_at_ms,last_reason_code,created_at_ms,updated_at_ms)
              VALUES(7,1,'identity','{}','RUNNING',1,2,'preserve-token',100,'OLD_REASON',1,2);
            INSERT INTO jobs(job_id,event_id,created_by_job_id,kind,payload_json,available_at_ms,
                             created_at_ms,updated_at_ms) VALUES(8,1,7,'next','{}',1,1,1);
            INSERT INTO job_history VALUES(9,7,1,'OLD_EVENT','OLD_REASON','{}',NULL);
        """)

    def tearDown(self):
        self.db.close()

    def test_upgrade_preserves_rows_references_indexes_and_null_activity(self):
        before = self.db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
        old_history = self.db.execute("SELECT * FROM job_history").fetchall()
        upgrade_v5(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall(),
                         [row + (None,) for row in before])
        self.assertEqual(self.db.execute("SELECT * FROM job_history").fetchall(),
                         [row + (None,) for row in old_history])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        targets = {row[2] for row in self.db.execute("PRAGMA foreign_key_list(jobs)")}
        self.assertEqual(targets, {"jobs", "incoming_events"})
        self.assertIn("jobs", {row[2] for row in self.db.execute("PRAGMA foreign_key_list(job_history)")})
        self.assertTrue({"idx_jobs_ready", "idx_jobs_expired", "idx_jobs_event"} <=
                        {row[1] for row in self.db.execute("PRAGMA index_list(jobs)")})
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM jobs WHERE job_id=7")
        self.db.rollback()

    def test_failure_during_rebuild_restores_v4_data_and_foreign_key_enforcement(self):
        before = self.db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
        sql = (SQL_DIR / "005_job_statuses.sql").read_text().replace(
            "PRAGMA user_version=5;", "SELECT * FROM deliberately_missing_table;\nPRAGMA user_version=5;")
        with self.assertRaises(sqlite3.Error):
            upgrade_v5(self.db, sql)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall(), before)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertNotIn("activity_code", {row[1] for row in self.db.execute("PRAGMA table_info(job_history)")})

    def test_wrong_version_and_broken_existing_foreign_keys_are_rejected(self):
        self.db.execute("PRAGMA user_version=3")
        with self.assertRaises(sqlite3.Error):
            upgrade_v5(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.db.execute("PRAGMA user_version=4")
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.execute("UPDATE jobs SET created_by_job_id=999 WHERE job_id=8")
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.assertRaises(sqlite3.Error):
            upgrade_v5(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
