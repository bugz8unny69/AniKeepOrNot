"""Offline v12→v13 upgrade/constraints/fresh equivalence/rollback; unexecuted."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class AcquisitionPlanMigrationTests(unittest.TestCase):

    def test_wrong_version_and_partial_failure_roll_back(self):
        for version in (11,12,13):
            db=sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'013_acquisition_plans.sql').read_text()
                if version==12:sql=sql.replace('PRAGMA user_version=13;','SELECT * FROM missing;\nPRAGMA user_version=13;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='acquisition_plans'").fetchone())
            finally:db.close()
