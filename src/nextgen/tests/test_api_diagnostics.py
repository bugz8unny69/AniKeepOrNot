"""Separate API attribution and immutable history migration cases; unexecuted."""
import io
import logging
from pathlib import Path
import sqlite3
import unittest

from src.nextgen.diagnostics import configure_logging, log_history
from src.nextgen.history import append_job_history
from src.nextgen.settings import parse_settings
from src.nextgen.storage.sqlite import (
    accept_event, enqueue_job, initialize, transaction,
)

SQL_DIR = Path(__file__).resolve().parents[1] / "storage" / "sql"


class ApiDiagnosticsTests(unittest.TestCase):
    def test_separate_api_logger_and_history_preserve_actual_emitter(self):
        db = sqlite3.connect(":memory:")
        db.execute("PRAGMA foreign_keys=ON")
        logger = logging.Logger("nextgen.api.test")
        stream = io.StringIO()
        logger.addHandler(logging.StreamHandler(stream))
        settings = parse_settings({"logging": {"components": {"api": "DEBUG", "worker": "WARNING"}}})
        configure_logging(settings, "api", logger=logger)
        try:
            initialize(db)
            event_id = accept_event(db, "test", b"private")
            with transaction(db):
                job_id = enqueue_job(db, component="api", kind="requested-work", payload={}, event_id=event_id)
                db.execute("UPDATE jobs SET state='CANCELED' WHERE job_id=?", (job_id,))
                receipt = append_job_history(
                    db, job_id=job_id, event_code="CANCELED", component="api",
                    occurred_at_ms=2, reason_code="UNSUPPORTED_JOB_KIND", details={"attempt": 0})
            log_history(logger, receipt)
            self.assertEqual(db.execute("SELECT component FROM job_history").fetchall(),
                             [("api",), ("api",)])
            self.assertIn("from api", stream.getvalue())
            self.assertNotIn("from worker", stream.getvalue())
            self.assertEqual(logger.level, logging.DEBUG)
            self.assertEqual(settings.logging.level_for("worker"), "WARNING")
        finally:
            db.close()


class ApiDiagnosticMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        for name in ("001_foundation.sql", "002_independent_jobs.sql", "003_incoming_events.sql",
                     "004_diagnostic_components.sql", "005_job_statuses.sql"):
            try:
                self.db.executescript((SQL_DIR / name).read_text())
            except BaseException:
                self.db.rollback()
                raise
            finally:
                self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(7,'test',X'61',1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,
                             created_at_ms,updated_at_ms,activity_code)
              VALUES(8,7,'work','{}','DONE',1,1,1,'SKIPPED_DUPLICATE');
            INSERT INTO event_history VALUES(9,7,1,'OLD_EVENT','OLD_REASON','old untyped detail',NULL);
            INSERT INTO job_history VALUES(10,8,1,'OLD_JOB','OLD_REASON','{}','identity','SKIPPED_DUPLICATE');
        """)

    def tearDown(self):
        self.db.close()

    def test_v6_preserves_every_history_column_and_enforces_api_vocabulary(self):
        before = {table: self.db.execute("SELECT * FROM " + table).fetchall()
                  for table in ("incoming_events", "jobs", "event_history", "job_history")}
        self.db.executescript((SQL_DIR / "006_api_diagnostics.sql").read_text())
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        for table, rows in before.items():
            self.assertEqual(self.db.execute("SELECT * FROM " + table).fetchall(), rows)
        with transaction(self.db):
            self.db.execute("INSERT INTO event_history(event_id,occurred_at_ms,event_code,component)"
                            " VALUES(7,3,'API_OBSERVATION','api')")
        with self.assertRaises(sqlite3.IntegrityError), transaction(self.db):
            self.db.execute("INSERT INTO job_history(job_id,occurred_at_ms,event_code,component)"
                            " VALUES(8,3,'OLD_JOB','not-a-component')")
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        for table, name in (("event_history", "idx_history_event"), ("job_history", "idx_job_history_job")):
            self.assertIn(name, {row[1] for row in self.db.execute("PRAGMA index_list(" + table + ")")})

    def test_v6_failure_rolls_back_history_rebuild_and_version(self):
        before = self.db.execute("SELECT * FROM job_history").fetchall()
        sql = (SQL_DIR / "006_api_diagnostics.sql").read_text().replace(
            "PRAGMA user_version=6;", "SELECT * FROM deliberately_missing_table;\nPRAGMA user_version=6;")
        with self.assertRaises(sqlite3.Error):
            try:
                self.db.executescript(sql)
            except BaseException:
                self.db.rollback()
                raise
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.db.execute("SELECT * FROM job_history").fetchall(), before)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError), transaction(self.db):
            self.db.execute("UPDATE job_history SET component='api'")

    def test_v6_refuses_a_wrong_starting_version(self):
        self.db.execute("PRAGMA user_version=4")
        with self.assertRaises(sqlite3.Error):
            try:
                self.db.executescript((SQL_DIR / "006_api_diagnostics.sql").read_text())
            except BaseException:
                self.db.rollback()
                raise
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
