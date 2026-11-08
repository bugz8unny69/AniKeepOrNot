"""Offline v28 -> v29 parity and rollback guarantees for batch claims."""
import sqlite3
import unittest

from src.anime.storage import migrations
from src.anime.storage.sqlite import initialize


class BatchReservationMigrationTests(unittest.TestCase):
    def migration_prefix(self,stop):
        db=sqlite3.connect(':memory:')
        db.execute('PRAGMA foreign_keys=ON')
        for path in migrations.SCRIPTS[:stop]:
            db.executescript((migrations.SQL_DIR/path).read_text())
        return db

    def test_upgrade_28_to_29_matches_fresh_schema(self):
        self.assertGreaterEqual(migrations.LATEST_VERSION,29)
        upgraded=self.migration_prefix(28)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone(),(28,))
        upgraded.executescript((migrations.SQL_DIR/'029_batch_episode_reservations.sql').read_text())
        fresh=sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        fresh.execute('PRAGMA foreign_keys=ON')
        initialize(fresh)
        self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone(),(29,))
        self.assertEqual(fresh.execute('PRAGMA user_version').fetchone(),(migrations.LATEST_VERSION,))
        self.assertEqual(migrations._structure(upgraded),
                         migrations._reference(29,migrations._scripts()))
        upgraded.executescript(
            (migrations.SQL_DIR/'030_batch_torrent_submissions.sql').read_text())
        for number in range(31,migrations.LATEST_VERSION+1):
            name=migrations.SCRIPTS[number-1]
            upgraded.executescript((migrations.SQL_DIR/name).read_text())
        self.assertEqual(migrations._structure(upgraded),migrations._structure(fresh))
        self.assertEqual(upgraded.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_failed_v29_rolls_back_schema_and_version(self):
        db=self.migration_prefix(28)
        self.addCleanup(db.close)
        faulty=(migrations.SQL_DIR/'029_batch_episode_reservations.sql').read_text()
        faulty=faulty.replace('PRAGMA user_version=29;','SELECT * FROM no_such_table;\nPRAGMA user_version=29;')
        with self.assertRaises(sqlite3.Error):
            db.executescript(faulty)
        # Mirror the migration runner's explicit rollback on SQL failures.
        if db.in_transaction:
            db.rollback()
        self.assertEqual(db.execute('PRAGMA user_version').fetchone(),(28,))
        self.assertIsNone(db.execute(
            "SELECT name FROM sqlite_master WHERE name='batch_episode_reservations'").fetchone())
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])


if __name__ == '__main__':
    unittest.main()
