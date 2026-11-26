"""Offline v11→v12 preservation, constraints and rollback. Unexecuted."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class EpisodeReservationMigrationTests(unittest.TestCase):

    def test_wrong_version_and_mid_migration_failure_roll_back(self):
        for version in (10,11,12):
            db=sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'012_episode_reservations.sql').read_text()
                if version==11:sql=sql.replace('PRAGMA user_version=12;','SELECT * FROM missing;\nPRAGMA user_version=12;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='episode_reservations'").fetchone())
            finally:db.close()
