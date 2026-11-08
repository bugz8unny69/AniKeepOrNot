"""Offline v13→v14 preservation and nullable historical-size migration; unexecuted."""
import hashlib
import json
import sqlite3
import unittest
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class AcquisitionBudgetMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_prior_prepared_plan_without_inventing_size(self):
        db=sqlite3.connect(':memory:')
        fresh=sqlite3.connect(':memory:')
        try:
            for script in SCRIPTS[:13]:db.executescript((SQL_DIR/script).read_text())
            db.execute('PRAGMA foreign_keys=ON')
            body=json.dumps({'release_name':'Test - 10','torrent_infohash':'a'*40}).encode()
            db.execute("INSERT INTO incoming_events VALUES(1,'test',?,1,'IDENTIFIED',0,NULL,NULL,NULL,NULL)",(body,))
            db.execute("INSERT INTO identified_anime VALUES(123,456,'Test')")
            db.execute('INSERT INTO identified_episodes VALUES(987,123)')
            series=json.dumps({'schema_version':2,'anime':{'anidb_id':123,'tvdb_series_id':456,'title':'Test'}})
            db.execute('INSERT INTO identity_results VALUES(1,1,123,2,?,1)',(series,))
            for job,kind in enumerate(('VERIFY_RELEASE','EVALUATE_TRIAL','RESERVE_EPISODE','PREPARE_ACQUISITION'),1):
                db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,state,attempt_count,available_at_ms,created_at_ms,updated_at_ms) VALUES(?,1,?,'{}','DONE',1,1,1,1)",(job,kind))
            db.execute("INSERT INTO release_verifications VALUES(1,1,1,123,?,'DECLARED_SINGLE_EPISODE',10,987,456,1,10,100,'official',1)",(hashlib.sha256(body).hexdigest(),))
            db.execute("INSERT INTO trial_plans VALUES(1,123,456,1,'official',1,10,6,1,1,1)")
            db.execute("INSERT INTO trial_evaluations VALUES(1,2,1,1,1,'TRIAL_SELECTED',1)")
            db.execute("INSERT INTO episode_reservations VALUES(1,3,2,1,1,123,987,456,1,10,100,'official',1)")
            manifest=json.dumps({'files':[{'index':0,'path':'Test - 10.mkv','size':100}],'selected_index':0})
            db.execute("INSERT INTO acquisition_plans(job_id,reservation_id,info_hash,info_sha256,manifest_json,submission_marker,configuration_revision,prepared_at_ms) VALUES(4,1,?,?,?,?,1,1)",('a'*40,'b'*64,manifest,'c'*32))
            db.commit()
            before=db.execute('SELECT * FROM acquisition_plans').fetchone()
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
            row=db.execute('SELECT * FROM acquisition_plans').fetchone()
            self.assertEqual(row[:-1],before)
            self.assertIsNone(row[-1])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],len(current_scripts))
        finally:db.close();fresh.close()

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
