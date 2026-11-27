"""Offline v17→v18 preservation, constraints and rollback; unexecuted."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class ManagementActionMigrationTests(unittest.TestCase):
    def prior(self):
        db=sqlite3.connect(':memory:')
        for script in SCRIPTS[:17]:db.executescript((SQL_DIR/script).read_text())
        return db


    def test_wrong_version_and_partial_failure_roll_back(self):
        for version in (16,17,18):
            db=self.prior()
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'018_management_actions.sql').read_text()
                if version==17:sql=sql.replace('PRAGMA user_version=18;','SELECT * FROM missing;\nPRAGMA user_version=18;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='management_actions'").fetchone())
            finally:db.close()

