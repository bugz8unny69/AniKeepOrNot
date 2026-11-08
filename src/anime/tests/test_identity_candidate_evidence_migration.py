"""Offline v58 -> v59: identity_candidate_evidence schema, FKs, rollback, fresh parity."""
import sqlite3
import unittest

from src.anime.storage.candidate_evidence import load_candidate_evidence, save_candidate_evidence
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize

SCRIPT_59 = (SQL_DIR / '059_identity_candidate_evidence.sql').read_text()


class IdentityCandidateEvidenceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:58]:
            self.db.executescript((SQL_DIR / name).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms)
                VALUES(1, 'autobrr', X'61', 1000);
            INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms)
                VALUES(10, 1, 'IDENTIFY_ANIME', '{}', 'DONE', 0, 1000, 1000);
        """)
        self.db.commit()

    def migrate(self):
        self.db.commit()
        self.db.executescript(SCRIPT_59)
        return self.db

    def test_upgrade_applies_v59_and_creates_table_and_indexes(self):
        db = self.migrate()
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 59)
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

        # Verify table exists and can hold rows
        db.execute("""
            INSERT INTO identity_candidate_evidence (
                job_id, attempt, captured_at_ms, anidb_id, display_title,
                matched_title, matched_variant, similarity_pct, rank,
                eligible, selected, evidence_json
            ) VALUES (10, 1, 1000, 19433, 'Black Clover 2nd Season',
                      'Black Clover 2nd Season', 'Black Clover 2nd Season',
                      100, 1, 1, 1, '{"exact": true}')
        """)
        db.commit()
        rows = db.execute("SELECT anidb_id, rank, eligible, selected FROM identity_candidate_evidence").fetchall()
        self.assertEqual(rows, [(19433, 1, 1, 1)])

    def test_guard_rolls_back_if_version_is_not_58(self):
        self.db.execute("PRAGMA user_version=57")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.executescript(SCRIPT_59)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 57)

    def test_fresh_schema_matches_sequential_upgrade(self):
        fresh = sqlite3.connect(':memory:')
        try:
            initialize(fresh)
            db = self.migrate()
            for name in SCRIPTS[59:]:  # Later versions build on v59.
                db.executescript((SQL_DIR / name).read_text())
            self.assertEqual(_structure(db), _structure(fresh))
        finally:
            fresh.close()

    def test_save_and_load_candidate_evidence_helpers(self):
        db = self.migrate()
        candidates = [
            {
                'anidb_id': 19433,
                'display_title': 'Black Clover 2nd Season',
                'matched_anidb_title': 'Black Clover 2nd Season',
                'matched_variant': 'Black Clover 2nd Season',
                'similarity_pct': 100,
                'rank': 1,
                'eligible': True,
                'selected': True,
                'evidence': {'score': 123.0},
            },
            {
                'anidb_id': 12665,
                'display_title': 'Black Clover',
                'matched_anidb_title': 'Black Clover',
                'matched_variant': 'Black Clover',
                'similarity_pct': 85,
                'rank': 2,
                'eligible': False,
                'selected': False,
                'evidence': {'score': 95.0},
            },
        ]
        save_candidate_evidence(db, 10, 1, candidates, now_ms=5000)
        db.commit()

        loaded = load_candidate_evidence(db, 10, 1)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]['anidb_id'], 19433)
        self.assertEqual(loaded[0]['rank'], 1)
        self.assertTrue(loaded[0]['eligible'])
        self.assertTrue(loaded[0]['selected'])
        self.assertEqual(loaded[0]['evidence'], {'score': 123.0})

        self.assertEqual(loaded[1]['anidb_id'], 12665)
        self.assertEqual(loaded[1]['rank'], 2)
        self.assertFalse(loaded[1]['eligible'])
        self.assertFalse(loaded[1]['selected'])


if __name__ == '__main__':
    unittest.main()
