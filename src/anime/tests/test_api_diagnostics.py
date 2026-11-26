"""Separate API attribution and immutable history migration cases; unexecuted."""
import io
import logging
from pathlib import Path
import sqlite3
import unittest

from src.anime.diagnostics import configure_logging, log_history
from src.anime.settings import parse_settings
from src.anime.storage.sqlite import transaction

SQL_DIR = Path(__file__).resolve().parents[1] / "storage" / "sql"




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
