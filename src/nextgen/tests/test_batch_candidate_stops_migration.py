"""Offline v60 -> v61: batch_candidate_stops, guard, rollback and fresh parity."""
import sqlite3
import unittest

from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.nextgen.storage.sqlite import initialize

SCRIPT_61 = (SQL_DIR / '061_batch_candidate_stops.sql').read_text()


class BatchCandidateStopsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        for name in SCRIPTS[:60]:
            self.db.executescript((SQL_DIR / name).read_text())

    def test_upgrade_creates_the_table_and_checks_values(self):
        self.db.executescript(SCRIPT_61)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 61)
        columns = [row[1] for row in self.db.execute('PRAGMA table_info(batch_candidate_stops)')]
        self.assertEqual(columns, ['candidate_id', 'operation_id', 'disposition', 'reason_code', 'recorded_at_ms'])
        sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name='batch_candidate_stops'").fetchone()[0]
        self.assertIn("disposition IN ('FAILED','CANCELED')", sql)

    def test_guard_rolls_back_if_version_is_not_60(self):
        self.db.execute('PRAGMA user_version=59')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.executescript(SCRIPT_61)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 59)
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='batch_candidate_stops'").fetchone())

    def test_fresh_schema_matches_sequential_upgrade(self):
        self.db.executescript(SCRIPT_61)
        fresh = sqlite3.connect(':memory:')
        try:
            initialize(fresh)
            self.assertEqual(_structure(self.db), _structure(fresh))
        finally:
            fresh.close()


if __name__ == '__main__':
    unittest.main()
