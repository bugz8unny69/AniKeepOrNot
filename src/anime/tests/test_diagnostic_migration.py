"""Historical v4 and latest sequential upgrade/rollback cases; no migration runner or live DB."""
from pathlib import Path
import sqlite3
import unittest

from src.anime.storage.sqlite import initialize

SQL_DIR = Path(__file__).resolve().parents[1] / "storage" / "sql"


def script(name):
    return (SQL_DIR / name).read_text(encoding="utf-8")


def v3_database(db):
    db.executescript(script("001_foundation.sql"))
    db.executescript(script("002_independent_jobs.sql"))
    db.executescript(script("003_incoming_events.sql"))


def structure(db):
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: (
        db.execute('PRAGMA table_info("' + name + '")').fetchall(),
        db.execute('PRAGMA foreign_key_list("' + name + '")').fetchall(),
        db.execute('PRAGMA index_list("' + name + '")').fetchall(),
    ) for name in tables}


class DiagnosticMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")

    def tearDown(self):
        self.db.close()

    def test_upgrade_preserves_payload_identity_provenance_and_unknown_history(self):
        v3_database(self.db)
        self.db.executescript("""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms)
              VALUES(7,'source',X'6162',1);
            INSERT INTO identified_anime VALUES(1,2,'Title');
            INSERT INTO identified_episodes VALUES(3,1);
            INSERT INTO identity_results VALUES(4,7,1,2,'{}',1);
            INSERT INTO result_episode_links VALUES(4,1,3);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(8,7,'identity','{}',1,1,1);
            INSERT INTO jobs(job_id,event_id,created_by_job_id,kind,payload_json,
                             available_at_ms,created_at_ms,updated_at_ms)
              VALUES(9,7,8,'next','{}',1,1,1);
            INSERT INTO event_history VALUES(10,7,1,'FUTURE_EVENT','OLD_REASON','old untyped detail');
            INSERT INTO job_history VALUES(11,8,1,'FUTURE_JOB','OLD_REASON','{}');
        """)
        tables = [row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        before = {table: self.db.execute("SELECT * FROM " + table).fetchall() for table in tables}
        self.db.executescript(script("004_diagnostic_components.sql"))
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 4)
        for table, rows in before.items():
            after = self.db.execute("SELECT * FROM " + table).fetchall()
            if table in ("job_history", "event_history"):
                self.assertEqual(after, [row + (None,) for row in rows])
            else:
                self.assertEqual(after, rows)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_latest_fresh_and_sequential_schema_metadata_match(self):
        initialize(self.db)
        other = sqlite3.connect(":memory:")
        other.execute("PRAGMA foreign_keys=ON")
        try:
            v3_database(other)
            other.executescript(script("004_diagnostic_components.sql"))
            try:
                other.executescript(script("005_job_statuses.sql"))
            except BaseException:
                other.rollback()
                raise
            finally:
                other.execute("PRAGMA foreign_keys=ON")
            other.executescript(script("006_api_diagnostics.sql"))
            other.executescript(script("007_worker_component.sql"))
            other.executescript(script("008_handler_history.sql"))
            other.executescript(script("009_provider_cache.sql"))
            other.executescript(script("010_release_verification.sql"))
            other.executescript(script("011_trial_windows.sql"))
            other.executescript(script("012_episode_reservations.sql"))
            other.executescript(script("013_acquisition_plans.sql"))
            other.executescript(script("014_acquisition_budgets.sql"))
            other.executescript(script("015_torrent_submissions.sql"))
            other.executescript(script("016_torrent_transfers.sql"))
            other.executescript(script("017_cancellation.sql"))
            other.executescript(script("018_management_actions.sql"))
            other.executescript(script("019_media_verifications.sql"))
            other.executescript(script("020_staging_publications.sql"))
            other.executescript(script("021_reject_decisions.sql"))
            other.executescript(script("022_keep_imports.sql"))
            other.executescript(script("023_reservation_release.sql"))
            from src.anime.storage.migrations import SCRIPTS as current_scripts
            for name in current_scripts[23:]:
                other.executescript((SQL_DIR/name).read_text())
            self.assertEqual(structure(self.db), structure(other))
            self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0],len(current_scripts))
        finally:
            other.close()

    def test_failed_upgrade_rolls_back_columns_and_version(self):
        v3_database(self.db)
        broken = script("004_diagnostic_components.sql").replace(
            "PRAGMA user_version=4;", "SELECT * FROM deliberately_missing_table;\nPRAGMA user_version=4;")
        with self.assertRaises(sqlite3.Error):
            self.db.executescript(broken)
        self.db.rollback()
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)
        for table in ("event_history", "job_history"):
            self.assertNotIn("component", [row[1] for row in self.db.execute(
                'PRAGMA table_info("' + table + '")')])

    def test_wrong_starting_version_rejected_and_v3_not_silently_stamped(self):
        for version in (0, 1, 2, 4, 99):
            with self.subTest(version=version):
                self.db.execute("PRAGMA user_version=" + str(version))
                with self.assertRaises(sqlite3.Error):
                    self.db.executescript(script("004_diagnostic_components.sql"))
                self.db.rollback()
                self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], version)
        self.db.execute("PRAGMA user_version=0")
        v3_database(self.db)
        with self.assertRaises(ValueError):
            initialize(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_component_constraint_and_historical_null(self):
        initialize(self.db)
        self.db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('test',X'61',1)")
        self.db.commit()
        for component in (None, "intake", "worker", "identity", "acquisition", "supervisor", "api"):
            with self.db:
                self.db.execute("INSERT INTO event_history(event_id,occurred_at_ms,event_code,component)"
                                " VALUES(1,1,'HISTORICAL',?)", (component,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO event_history(event_id,occurred_at_ms,event_code,component)"
                            " VALUES(1,1,'HISTORICAL','unknown')")
        self.db.rollback()
