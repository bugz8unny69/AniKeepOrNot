"""Offline schema checks for one-shot batch submission journaling (v30)."""
import sqlite3
import unittest

from src.anime.storage import migrations
from src.anime.storage.sqlite import initialize


class BatchSubmissionMigrationTests(unittest.TestCase):
    def test_29_to_30_schema_matches_fresh_and_rejects_bad_journal_states(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        for path in migrations.SCRIPTS[:29]:
            db.executescript((migrations.SQL_DIR/path).read_text())
        self.assertEqual(db.execute('PRAGMA user_version').fetchone(),(29,))
        db.executescript((migrations.SQL_DIR/'030_batch_torrent_submissions.sql').read_text())
        fresh=sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        initialize(fresh)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone(),(30,))
        self.assertEqual(migrations._structure(db),
                         migrations._reference(30,migrations._scripts()))
        for number in range(31,migrations.LATEST_VERSION+1):
            name=migrations.SCRIPTS[number-1]
            db.executescript((migrations.SQL_DIR/name).read_text())
        self.assertEqual(migrations._structure(db),migrations._structure(fresh))
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(),[('ok',)])

    def test_failed_v30_upgrade_rolls_back_completely(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        for path in migrations.SCRIPTS[:29]:
            db.executescript((migrations.SQL_DIR/path).read_text())
        faulty=(migrations.SQL_DIR/'030_batch_torrent_submissions.sql').read_text()
        faulty=faulty.replace('PRAGMA user_version=30;',
                              'SELECT * FROM nonexistent_table;\nPRAGMA user_version=30;')
        with self.assertRaises(sqlite3.Error):
            db.executescript(faulty)
        if db.in_transaction:db.rollback()
        self.assertEqual(db.execute('PRAGMA user_version').fetchone(),(29,))
        self.assertIsNone(db.execute(
            "SELECT name FROM sqlite_master WHERE name='batch_torrent_submissions'"
        ).fetchone())


if __name__=='__main__':
    unittest.main()
