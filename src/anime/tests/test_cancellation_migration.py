"""Offline v16→v17 preservation and immutable standalone historical scopes; unexecuted."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class CancellationMigrationTests(unittest.TestCase):
    def test_upgrade_matches_fresh_and_preserves_history_without_inventing_edges(self):
        db=sqlite3.connect(':memory:');fresh=sqlite3.connect(':memory:')
        try:
            for script in SCRIPTS[:16]:db.executescript((SQL_DIR/script).read_text())
            db.execute("INSERT INTO jobs(job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(1,'TASK','{}',0,1,1)")
            db.execute("INSERT INTO jobs(job_id,created_by_job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(2,1,'TASK','{}',0,1,1)")
            db.commit();before=db.execute('SELECT * FROM jobs ORDER BY job_id').fetchall()
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
            self.assertEqual(db.execute('SELECT * FROM jobs ORDER BY job_id').fetchall(),before)
            self.assertEqual(db.execute('SELECT * FROM job_operation_members ORDER BY job_id').fetchall(),[(1,1),(2,2)])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],len(current_scripts))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:db.close();fresh.close()

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
