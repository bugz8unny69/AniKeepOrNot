"""Offline v22→v23 preservation, schema equality and rollback; authored/unrun."""
import hashlib
import json
import sqlite3
import unittest
from src.anime.storage import migrations
from src.anime.storage.sqlite import initialize


class ReservationReleaseMigrationTests(unittest.TestCase):
    def prior(self):
        db=sqlite3.connect(':memory:')
        for name in migrations.SCRIPTS[:22]:db.executescript((migrations.SQL_DIR/name).read_text())
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
        db.execute("INSERT INTO acquisition_plans(job_id,reservation_id,info_hash,info_sha256,manifest_json,submission_marker,configuration_revision,prepared_at_ms,selected_size_bytes) VALUES(4,1,?,?,'{}',?,1,1,100)",('a'*40,'b'*64,'c'*32))
        db.commit();return db

    def test_migration_preserves_rows_foreign_keys_and_fresh_structure(self):
        db=self.prior();fresh=sqlite3.connect(':memory:')
        try:
            old=db.execute('SELECT * FROM episode_reservations').fetchall()
            plans=db.execute('SELECT * FROM acquisition_plans').fetchall()
            sql=(migrations.SQL_DIR/'023_reservation_release.sql').read_text()
            migrations._apply_step(db,sql,23,migrations._scripts())
            for name in migrations.SCRIPTS[23:]:db.executescript((migrations.SQL_DIR/name).read_text())
            initialize(fresh)
            self.assertEqual(migrations._structure(db),migrations._structure(fresh))
            self.assertEqual([r[:13] for r in db.execute('SELECT * FROM episode_reservations')],old)
            self.assertEqual(db.execute('SELECT * FROM acquisition_plans').fetchall(),plans)
            self.assertEqual(db.execute('SELECT state FROM episode_reservations').fetchall(),[('ACTIVE',)])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
        finally:db.close();fresh.close()

    def test_failed_rebuild_rolls_back_rows_and_restores_fk_enforcement(self):
        db=self.prior()
        try:
            before=db.execute('SELECT * FROM episode_reservations').fetchall()
            sql=(migrations.SQL_DIR/'023_reservation_release.sql').read_text().replace('PRAGMA user_version=23;','SELECT * FROM missing_table;\nPRAGMA user_version=23;')
            with self.assertRaises(sqlite3.Error):migrations._apply_step(db,sql,23,migrations._scripts())
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],22)
            self.assertEqual(db.execute('SELECT * FROM episode_reservations').fetchall(),before)
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
        finally:db.close()
