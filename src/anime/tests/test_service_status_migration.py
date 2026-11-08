"""Offline v61 -> v62: service_status table, guard, rollback and fresh parity."""
import sqlite3
import unittest

from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize

SCRIPT_62 = (SQL_DIR / '062_service_status.sql').read_text()


class ServiceStatusMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:61]:
            self.db.executescript((SQL_DIR / name).read_text())

    def test_upgrade_creates_the_table_and_checks_values(self):
        self.db.executescript(SCRIPT_62)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 62)
        self.db.execute("INSERT INTO service_status VALUES('sonarr','Sonarr','DOWN','HTTP 503',1,2,3)")
        for bad in (("x", "X", "MAYBE", "", 0, 0, 0), ("x", "X", "UP", "d" * 501, 0, 0, 0),
                    ("x", "X", "UP", "", -1, 0, 0)):
            with self.subTest(bad=bad), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('INSERT INTO service_status VALUES(?,?,?,?,?,?,?)', bad)

    def test_guard_rolls_back_if_version_is_not_61(self):
        self.db.execute('PRAGMA user_version=60')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.executescript(SCRIPT_62)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 60)
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='service_status'").fetchone())

    def test_fresh_schema_matches_sequential_upgrade(self):
        self.db.executescript(SCRIPT_62)
        for name in SCRIPTS[62:]:  # Later versions build on v62.
            self.db.executescript((SQL_DIR / name).read_text())
        fresh = sqlite3.connect(':memory:')
        try:
            initialize(fresh)
            self.assertEqual(_structure(self.db), _structure(fresh))
        finally:
            fresh.close()


if __name__ == '__main__':
    unittest.main()
