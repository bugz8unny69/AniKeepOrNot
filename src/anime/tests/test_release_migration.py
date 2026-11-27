"""Offline v9→v10 evidence-schema preservation/rollback cases; unexecuted."""
from pathlib import Path
import sqlite3
import unittest

from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class ReleaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        for name in SCRIPTS[:9]: self.db.executescript((SQL_DIR/name).read_text())
        self.db.executescript('''
            INSERT INTO incoming_events(source,raw_body,received_at_ms,state) VALUES('test',X'61',1,'IDENTIFIED');
            INSERT INTO identified_anime VALUES(123,456,'Test');
            INSERT INTO identified_episodes VALUES(987,123);
            INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,validated_at_ms)
              VALUES(1,123,2,'{}',1);
            INSERT INTO jobs(event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(1,'VERIFY_RELEASE','{}',0,1,1);
        ''')

    def sql(self):return (SQL_DIR/'010_release_verification.sql').read_text()


    def insert(self,scope='DECLARED_SINGLE_EPISODE',eid=987):
        self.db.execute("INSERT INTO release_verifications(job_id,identity_result_id,anidb_id,release_sha256,verification_scope,"
                        "anidb_episode_number,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms) "
                        "VALUES(1,1,123,?,?,1,?,456,0,1,100,1)",('a'*64,scope,eid))

    def test_scope_episode_ownership_and_one_verification_per_job_are_enforced(self):
        self.db.executescript(self.sql())
        for scope,eid in (('ACQUISITION_APPROVED',987),('DECLARED_SINGLE_EPISODE',999)):
            with self.assertRaises(sqlite3.IntegrityError):self.insert(scope,eid)
            self.db.rollback()
        self.insert()
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):self.insert()
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT tvdb_order,tvdb_season FROM release_verifications').fetchall(),[('official',0)])

    def test_failed_and_wrong_version_upgrades_preserve_original_schema(self):
        broken=self.sql().replace('PRAGMA user_version=10;','SELECT * FROM missing;\nPRAGMA user_version=10;')
        with self.assertRaises(sqlite3.Error):self.db.executescript(broken)
        self.db.rollback()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],9)
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE name='release_verifications'").fetchone())
        self.db.execute('PRAGMA user_version=8')
        with self.assertRaises(sqlite3.Error):self.db.executescript(self.sql())
        self.db.rollback()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],8)
