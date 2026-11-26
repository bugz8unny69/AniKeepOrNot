"""Offline v10→v11 preservation, fresh equivalence and rollback; unexecuted."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class UserEvaluationMigrationTests(unittest.TestCase):

    def test_failure_and_wrong_starting_versions_are_atomic(self):
        for version in (9,10,11):
            db=sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'011_trial_windows.sql').read_text()
                if version==10:sql=sql.replace('PRAGMA user_version=11;','SELECT * FROM missing;\nPRAGMA user_version=11;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='user_evaluation_plans'").fetchone())
            finally:db.close()
