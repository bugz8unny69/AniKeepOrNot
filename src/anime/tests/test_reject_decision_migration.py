"""Offline v20→v21 action preservation and explicit upgrade rollback; unrun."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class RejectDecisionMigrationTests(unittest.TestCase):
    def test_action_rows_survive_upgrade_and_match_fresh_schema(self):
        db=sqlite3.connect(':memory:');fresh=sqlite3.connect(':memory:')
        try:
            for script in SCRIPTS[:20]:db.executescript((SQL_DIR/script).read_text())
            for i in (1,2):db.execute("INSERT INTO jobs(job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(?,'TEST','{}',0,0,0)",(i,))
            db.execute('INSERT INTO job_operations(operation_id,created_at_ms,updated_at_ms) VALUES(1,0,0)')
            db.execute("INSERT INTO management_actions VALUES(1,'KEEP',1,2,0)");db.commit()
            before=db.execute('SELECT * FROM management_actions').fetchall()
            db.executescript((SQL_DIR/'021_reject_decisions.sql').read_text())
            db.executescript((SQL_DIR / "022_keep_imports.sql").read_text())
            db.executescript((SQL_DIR / "023_reservation_release.sql").read_text())
            from src.anime.storage.migrations import SCRIPTS as current_scripts
            for name in current_scripts[23:]:
                db.executescript((SQL_DIR/name).read_text())
            initialize(fresh)
            self.assertEqual(_structure(db),_structure(fresh))
            self.assertEqual(db.execute('SELECT * FROM management_actions').fetchall(),before)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],len(current_scripts))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:db.close();fresh.close()

    def test_wrong_version_and_partial_failure_roll_back(self):
        for version in (19,20,21):
            db=sqlite3.connect(':memory:')
            try:
                for script in SCRIPTS[:20]:db.executescript((SQL_DIR/script).read_text())
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'021_reject_decisions.sql').read_text()
                if version==20:sql=sql.replace('PRAGMA user_version=21;','SELECT * FROM missing;\nPRAGMA user_version=21;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='anime_review_decisions'").fetchone())
            finally:db.close()
