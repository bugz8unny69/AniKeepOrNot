"""Offline v11→v12 preservation, constraints and rollback. Unexecuted."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class EpisodeReservationMigrationTests(unittest.TestCase):
    def test_upgrade_matches_fresh_and_preserves_existing_events(self):
        db=sqlite3.connect(':memory:')
        fresh=sqlite3.connect(':memory:')
        try:
            for script in SCRIPTS[:11]:db.executescript((SQL_DIR/script).read_text())
            db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('test',X'61',1)")
            db.commit()
            before=db.execute('SELECT * FROM incoming_events').fetchall()
            db.executescript((SQL_DIR/'012_episode_reservations.sql').read_text())
            db.executescript((SQL_DIR/'013_acquisition_plans.sql').read_text())
            db.executescript((SQL_DIR/'014_acquisition_budgets.sql').read_text())
            db.executescript((SQL_DIR/'015_torrent_submissions.sql').read_text())
            db.executescript((SQL_DIR/'016_torrent_transfers.sql').read_text())
            db.executescript((SQL_DIR/'017_cancellation.sql').read_text())
            db.executescript((SQL_DIR/'018_management_actions.sql').read_text())
            db.executescript((SQL_DIR/'019_media_verifications.sql').read_text())
            db.executescript((SQL_DIR/'020_staging_publications.sql').read_text())
            db.executescript((SQL_DIR/'021_reject_decisions.sql').read_text())
            db.executescript((SQL_DIR / "022_keep_imports.sql").read_text())
            db.executescript((SQL_DIR / "023_reservation_release.sql").read_text())
            from src.anime.storage.migrations import SCRIPTS as current_scripts
            for name in current_scripts[23:]:
                db.executescript((SQL_DIR/name).read_text())
            initialize(fresh)
            self.assertEqual(_structure(db),_structure(fresh))
            self.assertEqual(db.execute('SELECT * FROM incoming_events').fetchall(),before)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],len(current_scripts))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            indexes=db.execute("PRAGMA index_list('episode_reservations')").fetchall()
            unique=[tuple(r[2] for r in db.execute('PRAGMA index_info('+row[1]+')')) for row in indexes if row[2]]
            for columns in (('owner_job_id',),('anidb_episode_id',),('tvdb_episode_id',),
                            ('tvdb_series_id','tvdb_season','tvdb_episode','tvdb_order')):
                self.assertIn(columns,unique)
        finally:db.close();fresh.close()

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
