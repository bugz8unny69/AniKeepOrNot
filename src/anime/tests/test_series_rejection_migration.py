"""Explicit v49->v50 rejection-reason migration smoke test."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR


class RejectionReasonMigrationTests(unittest.TestCase):
    def test_reason_survives_and_is_bounded(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE anime_review_decisions(anidb_id INTEGER PRIMARY KEY,
                operation_id INTEGER,job_id INTEGER,decision TEXT,
                cleanup_state TEXT,decided_at_ms INTEGER);
            INSERT INTO anime_review_decisions VALUES(10,1,2,'REJECT','COMPLETE',1);
            PRAGMA user_version=49;
        """)
        db.executescript((SQL_DIR/'050_series_rejection_reasons.sql').read_text())
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],50)
        self.assertEqual(db.execute(
            'SELECT reason_text FROM anime_review_decisions WHERE anidb_id=10'
        ).fetchone(), (None,))
        db.execute("UPDATE anime_review_decisions SET reason_text=? WHERE anidb_id=10",
                   ('Not interested: Watched a preview',))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE anime_review_decisions SET reason_text=? WHERE anidb_id=10",
                       ('x'*501,))


if __name__ == '__main__':
    unittest.main()
