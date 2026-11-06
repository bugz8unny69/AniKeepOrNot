"""Worker naming and upgrade compatibility cases; intentionally unexecuted."""
import copy
import json
from pathlib import Path
import sqlite3
import unittest

from src.nextgen.configuration_schema import (
    ConfigurationError, ConfigurationMigrationRequired, MAX_REVISION,
    convert_document, load_current,
)
from src.nextgen.history import component_name
from src.nextgen.settings import parse_settings
from src.nextgen.storage.sqlite import initialize, transaction, enqueue_job

SQL_DIR = Path(__file__).resolve().parents[1] / "storage" / "sql"


class WorkerConfigurationTests(unittest.TestCase):
    def test_v1_renames_preserve_values_and_advance_revision_without_mutating_source(self):
        source = {"schema_version": 1, "revision": 12, "settings": {
            "coordinator": {"concurrency": 9},
            "logging": {"level": "DEBUG", "components": {"coordinator": "TRACE", "api": "WARNING"}},
            "api": {"port": 9000}}}
        original = copy.deepcopy(source)
        result = convert_document(source)
        self.assertEqual(source, original)
        self.assertEqual((result.schema_version, result.revision), (15, 26))
        self.assertEqual(result.value("worker.concurrency"), 9)
        self.assertEqual(result.value("logging.components"), {"worker": "TRACE", "api": "WARNING"})
        self.assertEqual(result.value("api.port"), 9000)
        self.assertNotIn("coordinator", result.to_document()["settings"])
        with self.assertRaises(ConfigurationMigrationRequired):
            load_current(json.dumps(source).encode())
        self.assertEqual(load_current(json.dumps(result.to_document()).encode()), result)

    def test_mixed_names_invalid_historical_values_or_revision_overflow_are_rejected(self):
        cases = (
            {"coordinator": {"concurrency": 2}, "worker": {"concurrency": 3}},
            {"worker": {"concurrency": 3}},
            {"logging": {"components": {"coordinator": "TRACE", "worker": "DEBUG"}}},
            {"coordinator": {"concurrency": 0}},
            {"logging": {"components": {"coordinator": "not-a-level"}}},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                convert_document({"schema_version": 1, "revision": 1, "settings": values})
        with self.assertRaises(ConfigurationError):
            convert_document({"schema_version": 1, "revision": MAX_REVISION, "settings": {}})

    def test_current_diagnostics_use_worker_and_reject_retired_emitter(self):
        settings = parse_settings({"logging": {"components": {"worker": "TRACE"}}})
        self.assertEqual(settings.logging.level_for("worker"), "TRACE")
        self.assertEqual(component_name("worker"), "worker")
        with self.assertRaises(ValueError):
            component_name("coordinator")
        with self.assertRaises(ValueError):
            parse_settings({"logging": {"components": {"coordinator": "TRACE"}}})


class WorkerSqlMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        for name in ("001_foundation.sql", "002_independent_jobs.sql", "003_incoming_events.sql",
                     "004_diagnostic_components.sql", "005_job_statuses.sql", "006_api_diagnostics.sql"):
            self.db.executescript((SQL_DIR / name).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events VALUES(7,'test',X'61',1,'PENDING',0,NULL,NULL,NULL,NULL);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms)
                VALUES(8,7,'test','{}',1,1,1);
            INSERT INTO event_history VALUES(9,7,1,'OLD_EVENT','OLD_REASON','old detail','coordinator');
            INSERT INTO job_history VALUES(10,8,1,'OLD_JOB','OLD_REASON','{}','coordinator',NULL);
        """)

    def tearDown(self):
        self.db.close()

    def test_v7_preserves_history_and_accepts_new_worker_audits(self):
        before = {table: self.db.execute("SELECT * FROM " + table).fetchall()
                  for table in ("incoming_events", "jobs", "event_history", "job_history")}
        self.db.executescript((SQL_DIR / "007_worker_component.sql").read_text())
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 7)
        for table, rows in before.items():
            self.assertEqual(self.db.execute("SELECT * FROM " + table).fetchall(), rows)
        with self.assertRaises(ValueError):
            initialize(self.db)  # Current storage requires explicit upgrade from v7.
        self.db.executescript((SQL_DIR / "008_handler_history.sql").read_text())
        # The current enqueue adapter also writes operation scopes (v17+).
        # At v8, exercise only the historical worker-attributed audit shape.
        with transaction(self.db):
            self.db.execute("INSERT INTO jobs(job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(11,'new-work','{}',1,1,1)")
            self.db.execute("INSERT INTO job_history(job_id,occurred_at_ms,event_code,details_json,component) VALUES(11,1,'CREATED','{}','worker')")
        self.assertEqual(self.db.execute("SELECT component FROM job_history ORDER BY history_id").fetchall(),
                         [("coordinator",), ("worker",)])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        for table, index in (("event_history", "idx_history_event"), ("job_history", "idx_job_history_job")):
            self.assertIn(index, {row[1] for row in self.db.execute("PRAGMA index_list(" + table + ")")})
        self.db.executescript((SQL_DIR / "009_provider_cache.sql").read_text())
        self.db.executescript((SQL_DIR / "010_release_verification.sql").read_text())
        self.db.executescript((SQL_DIR / "011_trial_windows.sql").read_text())
        self.db.executescript((SQL_DIR / "012_episode_reservations.sql").read_text())
        self.db.executescript((SQL_DIR / "013_acquisition_plans.sql").read_text())
        self.db.executescript((SQL_DIR / "014_acquisition_budgets.sql").read_text())
        self.db.executescript((SQL_DIR / "015_torrent_submissions.sql").read_text())
        self.db.executescript((SQL_DIR / "016_torrent_transfers.sql").read_text())
        self.db.executescript((SQL_DIR / "017_cancellation.sql").read_text())
        self.db.executescript((SQL_DIR / "018_management_actions.sql").read_text())
        self.db.executescript((SQL_DIR / "019_media_verifications.sql").read_text())
        self.db.executescript((SQL_DIR / "020_staging_publications.sql").read_text())
        self.db.executescript((SQL_DIR / "021_reject_decisions.sql").read_text())
        self.db.executescript((SQL_DIR / "022_keep_imports.sql").read_text())
        self.db.executescript((SQL_DIR / "023_reservation_release.sql").read_text())
        from src.nextgen.storage.migrations import SCRIPTS as current_scripts
        for name in current_scripts[23:]:self.db.executescript((SQL_DIR/name).read_text())
        initialize(self.db)  # Latest version accepted without implicit upgrade.

    def test_failure_before_version_bump_rolls_back_rows_and_vocabulary(self):
        before = self.db.execute("SELECT * FROM job_history").fetchall()
        sql = (SQL_DIR / "007_worker_component.sql").read_text().replace(
            "PRAGMA user_version=7;", "SELECT * FROM missing_table;\nPRAGMA user_version=7;")
        with self.assertRaises(sqlite3.Error):
            try:
                self.db.executescript(sql)
            except BaseException:
                self.db.rollback()
                raise
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(self.db.execute("SELECT * FROM job_history").fetchall(), before)
        with self.assertRaises(sqlite3.IntegrityError), transaction(self.db):
            self.db.execute("UPDATE job_history SET component='worker'")

    def test_wrong_starting_version_refused(self):
        self.db.execute("PRAGMA user_version=5")
        with self.assertRaises(sqlite3.Error):
            try:
                self.db.executescript((SQL_DIR / "007_worker_component.sql").read_text())
            except BaseException:
                self.db.rollback()
                raise
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
