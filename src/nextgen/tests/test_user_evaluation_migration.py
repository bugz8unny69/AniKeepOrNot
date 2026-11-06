"""Offline v10→v11 preservation, fresh equivalence and rollback; unexecuted."""
import sqlite3
import unittest
from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.nextgen.storage.sqlite import initialize


class UserEvaluationMigrationTests(unittest.TestCase):
    def test_upgrade_matches_fresh_and_preserves_old_rows(self):
        db=sqlite3.connect(':memory:')
        fresh=sqlite3.connect(':memory:')
        try:
            for script in SCRIPTS[:10]:db.executescript((SQL_DIR/script).read_text())
            db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('test',X'61',1)")
            db.commit()
            before=db.execute('SELECT * FROM incoming_events').fetchall()
            db.executescript((SQL_DIR/'011_trial_windows.sql').read_text())
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
            from src.nextgen.storage.migrations import SCRIPTS as current_scripts
            for name in current_scripts[23:]:
                db.executescript((SQL_DIR/name).read_text())
            initialize(fresh)
            self.assertEqual(_structure(db),_structure(fresh))
            self.assertEqual(db.execute('SELECT * FROM incoming_events').fetchall(),before)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],len(current_scripts))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:
            db.close()
            fresh.close()

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
