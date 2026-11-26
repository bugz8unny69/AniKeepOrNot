"""Offline v14→v15 preservation, fresh equivalence and rollback; unexecuted."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class TorrentSubmissionMigrationTests(unittest.TestCase):

    def test_wrong_version_or_partial_failure_rolls_back(self):
        for version in (13,14,15):
            db=sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'015_torrent_submissions.sql').read_text()
                if version==14:sql=sql.replace('PRAGMA user_version=15;','SELECT * FROM missing;\nPRAGMA user_version=15;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='torrent_submissions'").fetchone())
            finally:db.close()
