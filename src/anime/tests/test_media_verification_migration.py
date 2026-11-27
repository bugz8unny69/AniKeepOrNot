"""Offline v18→v19 preservation, schema equivalence and rollback; unrun."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class MediaVerificationMigrationTests(unittest.TestCase):

    def test_wrong_start_or_partial_failure_rolls_back(self):
        for version in (17,18,19):
            db=sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'019_media_verifications.sql').read_text()
                if version==18:sql=sql.replace('PRAGMA user_version=19;','SELECT * FROM missing;\nPRAGMA user_version=19;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='media_verifications'").fetchone())
            finally:db.close()
