"""Offline v18→v19 preservation, schema equivalence and rollback; unrun."""
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class MediaVerificationMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_prior_rows_and_matches_fresh(self):
        db=sqlite3.connect(':memory:');fresh=sqlite3.connect(':memory:')
        try:
            for script in SCRIPTS[:18]:db.executescript((SQL_DIR/script).read_text())
            db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('test',X'61',1)");db.commit()
            before=db.execute('SELECT * FROM incoming_events').fetchall()
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
        finally:db.close();fresh.close()

    def test_wrong_start_or_partial_failure_rolls_back(self):
        for version in (17,18,19):
            db=sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'019_media_verifications.sql').read_text()
                if version==18:sql=sql.replace('PRAGMA user_version=19;','SELECT * FROM missing;\nPRAGMA user_version=19;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='media_verifications'").fetchone())
            finally:db.close()
