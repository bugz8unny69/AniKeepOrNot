"""Offline v16→v17 preservation and immutable standalone historical scopes; unexecuted."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class CancellationMigrationTests(unittest.TestCase):

    def test_wrong_version_and_partial_failure_roll_back(self):
        for version in (15,16,17):
            db=sqlite3.connect(':memory:')
            try:
                for script in SCRIPTS[:16]:db.executescript((SQL_DIR/script).read_text())
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'017_cancellation.sql').read_text()
                if version==16:sql=sql.replace('PRAGMA user_version=17;','SELECT * FROM missing;\nPRAGMA user_version=17;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='job_operations'").fetchone())
            finally:db.close()
