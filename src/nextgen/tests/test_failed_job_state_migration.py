"""Migration tests for v57 -> v58: Adding FAILED state to jobs table."""
import sqlite3
import unittest

from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, _structure, LATEST_VERSION
from src.nextgen.storage.sqlite import initialize

SCRIPT = (SQL_DIR / '058_failed_job_state.sql').read_text()


class FailedJobStateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:57]:
            self.db.executescript((SQL_DIR / name).read_text())
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 57)
        self.db.executescript("""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms)
              VALUES(1,'manual-release',X'61',1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(1,1,'PREPARE_ACQUISITION','{}','PENDING',0,0,0);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms,lease_token,lease_expires_at_ms)
              VALUES(2,1,'VERIFY_RELEASE','{}','RUNNING',0,0,0,'tok',9999);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(3,1,'CLEANUP_CANCELED_OPERATION','{}','DONE',0,0,0);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms,last_reason_code)
              VALUES(4,1,'PREPARE_ACQUISITION','{}','NEEDS_REVIEW',0,0,0,'SOME_ERROR');
        """)
        self.db.commit()

    def migrate(self):
        self.db.executescript(SCRIPT)
        return self.db

    def test_upgrade_matches_fresh_schema_and_version(self):
        fresh = sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        initialize(fresh)
        upgraded = self.migrate()
        self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], 58)
        self.assertEqual(_structure(fresh), _structure(upgraded))

    def test_upgrade_preserves_existing_jobs_and_allows_failed_state(self):
        upgraded = self.migrate()
        rows = upgraded.execute(
            "SELECT job_id,state,last_reason_code FROM jobs ORDER BY job_id"
        ).fetchall()
        self.assertEqual(rows, [
            (1, 'PENDING', None),
            (2, 'RUNNING', None),
            (3, 'DONE', None),
            (4, 'NEEDS_REVIEW', 'SOME_ERROR'),
        ])
        # Inserting FAILED state is allowed
        upgraded.execute("""
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms,last_reason_code)
              VALUES(5,1,'PREPARE_ACQUISITION','{}','FAILED',0,0,0,'ACQUISITION_METADATA_TIMEOUT')
        """)
        self.assertEqual(
            upgraded.execute("SELECT state,last_reason_code FROM jobs WHERE job_id=5").fetchone(),
            ('FAILED', 'ACQUISITION_METADATA_TIMEOUT')
        )
        # Invalid state is rejected
        with self.assertRaises(sqlite3.IntegrityError):
            upgraded.execute("""
                INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
                  VALUES(6,1,'PREPARE_ACQUISITION','{}','INVALID_STATE',0,0,0)
            """)

    def test_migration_error_rolls_back(self):
        before = _structure(self.db)
        faulty = SCRIPT.replace(
            "PRAGMA user_version=58;",
            "SELECT * FROM no_such_table;\nPRAGMA user_version=58;"
        )
        with self.assertRaises(sqlite3.OperationalError):
            self.db.executescript(faulty)
        if self.db.in_transaction:
            self.db.rollback()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 57)
        self.assertEqual(_structure(self.db), before)


if __name__ == '__main__':
    unittest.main()
