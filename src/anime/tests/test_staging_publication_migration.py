"""Offline explicit v19→v20 schema preservation and rollback; unrun."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class StagingPublicationMigrationTests(unittest.TestCase):

    def test_wrong_version_or_partial_failure_rolls_back(self):
        for version in (18,19,20):
            db=sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'020_staging_publications.sql').read_text()
                if version==19:sql=sql.replace('PRAGMA user_version=20;','SELECT * FROM missing;\nPRAGMA user_version=20;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='staging_publications'").fetchone())
            finally:db.close()
