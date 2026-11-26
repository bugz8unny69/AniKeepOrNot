"""Offline v13→v14 preservation and nullable historical-size migration; unexecuted."""
import hashlib
import json
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class AcquisitionBudgetMigrationTests(unittest.TestCase):

    def test_wrong_start_and_partial_failure_roll_back_column_and_table(self):
        for version in (12,13,14):
            db=sqlite3.connect(':memory:')
            try:
                for script in SCRIPTS[:13]:db.executescript((SQL_DIR/script).read_text())
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'014_acquisition_budgets.sql').read_text()
                if version==13:sql=sql.replace('PRAGMA user_version=14;','SELECT * FROM missing;\nPRAGMA user_version=14;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertNotIn('selected_size_bytes',[r[1] for r in db.execute('PRAGMA table_info(acquisition_plans)')])
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='acquisition_budgets'").fetchone())
            finally:db.close()
