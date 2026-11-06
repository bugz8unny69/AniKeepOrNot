"""Offline v59 -> v60: batch_candidate_operations schema, FKs, rollback, fresh parity."""
import sqlite3
import unittest

from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.nextgen.storage.sqlite import initialize

SCRIPT_60 = (SQL_DIR / '060_batch_candidate_operations.sql').read_text()


class BatchCandidateOperationsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:59]:
            self.db.executescript((SQL_DIR / name).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms)
                VALUES(1, 'autobrr', X'61', 1000);
            INSERT INTO identified_anime(anidb_id, tvdb_series_id, title)
                VALUES(100, 200, 'Test Series');
        """)
        self.db.commit()

    def migrate(self):
        self.db.commit()
        self.db.executescript(SCRIPT_60)
        return self.db

    def test_upgrade_applies_v60_and_creates_tables_and_indexes(self):
        db = self.migrate()
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 60)
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

        # Verify batch_candidate_scopes
        db.execute("""
            INSERT INTO batch_candidate_scopes (
                scope_id, anidb_id, tvdb_series_id, tvdb_season, state,
                created_at_ms, updated_at_ms
            ) VALUES (1, 100, 200, 1, 'ACTIVE', 1000, 1000)
        """)
        # Verify batch_candidates
        db.execute("""
            INSERT INTO batch_candidates (
                candidate_id, scope_id, info_hash, title, rank, state,
                has_metadata, created_at_ms, updated_at_ms
            ) VALUES (1, 1, '0123456789abcdef0123456789abcdef01234567',
                      'Test Series S01 Batch', 1, 'READY', 0, 1000, 1000)
        """)
        # Verify batch_candidate_events
        db.execute("""
            INSERT INTO batch_candidate_events (
                scope_id, info_hash, event_id, received_at_ms
            ) VALUES (1, '0123456789abcdef0123456789abcdef01234567', 1, 1000)
        """)
        db.commit()

        row = db.execute("SELECT anidb_id, tvdb_season FROM batch_candidate_scopes WHERE scope_id=1").fetchone()
        self.assertEqual(row, (100, 1))
        c_row = db.execute("SELECT rank, state FROM batch_candidates WHERE candidate_id=1").fetchone()
        self.assertEqual(c_row, (1, 'READY'))

    def test_guard_rolls_back_if_version_is_not_59(self):
        self.db.execute("PRAGMA user_version=58")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.executescript(SCRIPT_60)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 58)

    def test_fresh_schema_matches_sequential_upgrade(self):
        fresh = sqlite3.connect(':memory:')
        try:
            initialize(fresh)
            migrated = self.migrate()
            for name in SCRIPTS[60:]:  # Later versions build on v60.
                migrated.executescript((SQL_DIR / name).read_text())
            self.assertEqual(_structure(fresh), _structure(migrated))
        finally:
            fresh.close()
