"""Offline v8 preservation, constraints and rollback cases; intentionally unrun."""
from pathlib import Path
import sqlite3
import unittest

SQL_DIR = Path(__file__).resolve().parents[1] / "storage" / "sql"


class HandlerHistoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        for name in ("001_foundation.sql", "002_independent_jobs.sql", "003_incoming_events.sql",
                     "004_diagnostic_components.sql", "005_job_statuses.sql", "006_api_diagnostics.sql",
                     "007_worker_component.sql"):
            self.db.executescript((SQL_DIR / name).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events VALUES(7,'test',X'61',1,'PENDING',0,NULL,NULL,NULL,NULL);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms)
                VALUES(8,7,'test','{}',1,1,1);
            INSERT INTO jobs(job_id,created_by_job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms)
                VALUES(9,8,'test','{}',1,1,1);
            INSERT INTO event_history VALUES(9,7,1,'UNKNOWN','OLD_REASON','old detail',NULL);
            INSERT INTO job_history VALUES(10,8,1,'UNKNOWN','OLD_REASON','{}','coordinator',NULL);
            INSERT INTO job_history VALUES(11,8,1,'HANDLER_STARTED',NULL,'{}',NULL,NULL);
        """)

    def tearDown(self):
        self.db.close()

    def sql(self):
        return (SQL_DIR / "008_handler_history.sql").read_text()

    def test_v8_preserves_rows_ids_and_jobs_shape_with_unknown_handler_history(self):
        tables = [row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        before = {name: self.db.execute("SELECT * FROM " + name).fetchall() for name in tables}
        jobs_shape = self.db.execute("PRAGMA table_info(jobs)").fetchall()
        self.db.executescript(self.sql())
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 8)
        self.assertEqual(self.db.execute("PRAGMA table_info(jobs)").fetchall(), jobs_shape)
        for table, rows in before.items():
            expected = [row + (None,None,None) for row in rows] if table == "job_history" else rows
            self.assertEqual(self.db.execute("SELECT * FROM " + table).fetchall(), expected)
        indexes = {row[1] for row in self.db.execute("PRAGMA index_list(job_history)")}
        self.assertTrue({"idx_job_history_job", "idx_job_history_invocation", "idx_handler_invocation_start", "idx_handler_invocation_end"} <= indexes)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def insert(self, code, *, handler="TEST", label="Test handler", identifier="a"*32):
        self.db.execute("INSERT INTO job_history(job_id,occurred_at_ms,event_code,handler_code,handler_label,invocation_id) "
                        "VALUES(8,2,?,?,?,?)", (code,handler,label,identifier))

    def test_database_checks_metadata_shapes_and_unique_invocation_markers(self):
        self.db.executescript(self.sql())
        for overrides in ({"handler": None}, {"label": None}, {"identifier": None}, {"handler": "bad-code"},
                          {"identifier": "bad"}, {"label": ""}, {"label": "x"*129}):
            with self.subTest(overrides=overrides), self.assertRaises(sqlite3.IntegrityError):
                self.insert("HANDLER_STARTED", **overrides)
            self.db.rollback()
        self.insert("HANDLER_STARTED")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("HANDLER_STARTED")
        self.db.rollback()
        self.insert("HANDLER_COMPLETED")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("HANDLER_FAILED")
        self.db.rollback()

    def test_failure_rolls_back_version_columns_and_rows(self):
        before = self.db.execute("SELECT * FROM job_history").fetchall()
        broken = self.sql().replace("PRAGMA user_version=8;", "SELECT * FROM missing_table;\nPRAGMA user_version=8;")
        with self.assertRaises(sqlite3.Error):
            try:
                self.db.executescript(broken)
            except BaseException:
                self.db.rollback()
                raise
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertEqual(self.db.execute("SELECT * FROM job_history").fetchall(), before)
        self.assertNotIn("handler_code", {row[1] for row in self.db.execute("PRAGMA table_info(job_history)")})
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_wrong_version_refused(self):
        self.db.execute("PRAGMA user_version=6")
        with self.assertRaises(sqlite3.Error):
            try:
                self.db.executescript(self.sql())
            except BaseException:
                self.db.rollback()
                raise
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 6)
