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

    def test_preserves_jobs_history_and_cancellation_and_matches_fresh(self):
        db=self.prior();fresh=sqlite3.connect(':memory:')
        try:
            db.execute("INSERT INTO jobs(kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES('TASK','{}',0,1,1)")
            db.execute("INSERT INTO job_history(job_id,occurred_at_ms,event_code) VALUES(1,1,'HISTORICAL')")
            db.execute("INSERT INTO job_operations VALUES(1,'CANCELED','COMPLETE','CANCELLATION_COMPLETE',1,1)")
            db.execute('INSERT INTO job_operation_members VALUES(1,1)');db.commit()
            before={table:db.execute('SELECT * FROM '+table).fetchall() for table in ('jobs','job_history','job_operations','job_operation_members')}
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
            for table,rows in before.items():self.assertEqual(db.execute('SELECT * FROM '+table).fetchall(),rows)
            self.assertEqual(db.execute('SELECT * FROM management_actions').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],len(current_scripts))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:db.close();fresh.close()

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

    def test_uniqueness_foreign_keys_and_action_vocabulary(self):
        db=sqlite3.connect(':memory:');db.execute('PRAGMA foreign_keys=ON');initialize(db)
        try:
            for i in (1,2,3):
                db.execute("INSERT INTO jobs(job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(?,'TASK','{}',0,1,1)",(i,))
                db.execute('INSERT INTO job_operations(operation_id,created_at_ms,updated_at_ms) VALUES(?,1,1)',(i,))
            db.execute("INSERT INTO management_actions VALUES(1,'KEEP',1,2,1)");db.commit()
            for values in ((1,'KEEP',1,3,1),(3,'CLEANUP',3,2,1),(1,'ARBITRARY',1,3,1),(1,'CLEANUP',99,3,1),(1,'CLEANUP',3,3,1)):
                with self.assertRaises(sqlite3.IntegrityError):db.execute('INSERT INTO management_actions VALUES(?,?,?,?,?)',values)
                db.rollback()
        finally:db.close()
