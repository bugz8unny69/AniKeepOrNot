"""Offline season recognition tests; not executed until CI approval."""
import json
from types import SimpleNamespace
import sqlite3
import unittest

from src.anime.storage.sqlite import initialize
from src.anime.storage.recognition import (
    RecognitionConflict, recognized_for_release, remember_verified_release,
    recognized_episode, remember_verified_episode)


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO identified_anime VALUES(1001,2001,'Example Anime')")
        for id_,season,episode in ((11,1,3),(12,2,1)):
            event=id_
            self.db.execute(
                "INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(?, 'autobrr',?,1)",
                (event,json.dumps({"release_name":f"Example Anime S{season:02d}E{episode:02d}"}).encode()))
            self.db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(?,?,'VERIFY_RELEASE','{}',1,1,1)",(id_,event))
            self.db.execute("INSERT INTO identity_results VALUES(?,?,1001,2,'{}',1)",(id_,event))
            self.db.execute("INSERT INTO identified_episodes VALUES(?,1001)",(10000+id_,))
            self.db.execute("INSERT INTO release_verifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id_,id_,id_,1001,'a'*64,'DECLARED_SINGLE_EPISODE',episode,10000+id_,
                 2001,season,episode,20000+id_,'official',100000))
        self.db.commit()

    def remember(self,id_,season):
        source=SimpleNamespace(anidb_id=1001,tvdb_series_id=2001,
            raw_body=json.dumps({"release_name":f"Example Anime S{season:02d}E01"}).encode())
        self.db.execute("BEGIN")
        try:
            remember_verified_release(self.db,verification_id=id_,source=source,season=season)
            remember_verified_episode(self.db,verification_id=id_)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def second_work(self, *, tid=2001, title='Second Cour', release='Second Cour S01E04'):
        self.db.execute("INSERT INTO identified_anime VALUES(1002,?,?)", (tid,title))
        self.db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(13,'autobrr',?,1)",
                        (json.dumps({'release_name':release}).encode(),))
        self.db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(13,13,'VERIFY_RELEASE','{}',1,1,1)")
        self.db.execute("INSERT INTO identity_results VALUES(13,13,1002,2,'{}',1)")
        self.db.execute("INSERT INTO identified_episodes VALUES(10013,1002)")
        self.db.execute("INSERT INTO release_verifications VALUES(13,13,13,1002,?,'DECLARED_SINGLE_EPISODE',1,10013,?,1,4,20013,'official',100000)", ('b'*64,tid))
        remember_verified_release(self.db, verification_id=13,
            source=SimpleNamespace(anidb_id=1002,tvdb_series_id=tid,
                raw_body=json.dumps({'release_name':release}).encode()), season=1)
        remember_verified_episode(self.db, verification_id=13)
        self.db.commit()

    def test_two_verified_works_can_share_tvdb_season(self):
        self.remember(11,1)
        self.second_work()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recognized_seasons WHERE tvdb_season=1").fetchone()[0],2)
        self.assertEqual(recognized_for_release(self.db,'Second Cour S01E04',season=1)['anidb_id'],1002)
        self.assertEqual(recognized_for_release(self.db,'Example Anime S01E03',season=1)['anidb_id'],1001)
        self.assertEqual(recognized_episode(self.db,aid=1002,tid=2001,origin='tvdb',season=1,number=4)['anidb_episode_id'],10013)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_shared_alias_is_ambiguous_instead_of_first_owner_winning(self):
        self.remember(11,1)
        self.second_work(tid=2002,title='Example Anime')
        self.assertIsNone(recognized_for_release(self.db,'Example Anime S01E09',season=1))
        self.assertEqual(recognized_for_release(self.db,'Second Cour S01E04',season=1)['anidb_id'],1002)
        self.assertEqual(self.db.execute('SELECT count(*) FROM recognized_episodes').fetchone()[0],2)

    def test_v25_upgrade_preserves_evidence_and_matches_fresh_schema(self):
        from src.anime.storage import migrations
        old=sqlite3.connect(':memory:')
        self.addCleanup(old.close)
        old.execute('PRAGMA foreign_keys=ON')
        for name in migrations.SCRIPTS[:25]:
            old.executescript((migrations.SQL_DIR/name).read_text())
        # Copy a complete verified graph into the historical schema.
        self.remember(11,1)
        # v70 keys aliases by AniDB anime; v25 keyed them by TVDB season.
        shapes={'recognized_aliases':'SELECT a.alias_key,s.tvdb_season,s.tvdb_series_id,a.anidb_id '
                                     'FROM recognized_aliases a JOIN recognized_seasons s USING(anidb_id)'}
        for table in ('identified_anime','incoming_events','jobs','identity_results',
                      'identified_episodes','release_verifications','recognized_seasons',
                      'recognized_aliases','recognized_episodes'):
            rows=self.db.execute(shapes.get(table,'SELECT * FROM '+table)).fetchall()
            if rows:
                old.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
        old.commit()
        for name in migrations.SCRIPTS[25:]:
            old.executescript((migrations.SQL_DIR/name).read_text())
        self.assertEqual(migrations._structure(old),migrations._structure(self.db))
        self.assertEqual(old.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(recognized_for_release(old,'Example Anime S01E03',season=1)['anidb_id'],1001)
        self.assertEqual(old.execute('SELECT count(*) FROM recognized_episodes').fetchone()[0],1)

    def test_automatic_series_season_recognition_is_not_per_episode(self):
        self.remember(11,1)
        self.remember(12,2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM recognized_seasons").fetchone()[0],2)
        first=recognized_for_release(self.db,"Example Anime S01E09",season=1)
        second=recognized_for_release(self.db,"Example Anime S02E03",season=2)
        self.assertEqual(first["anidb_id"],1001)
        self.assertEqual(second["tvdb_series_id"],2001)
        self.assertEqual(second["tvdb_season"],2)
        # P2: one anime per alias is found without a season marker too.
        self.assertEqual(recognized_for_release(self.db,"Example Anime - 01")["anidb_id"],1001)
        self.assertIsNone(recognized_for_release(self.db,"Unknown Anime S02E01",season=2))
        self.assertEqual(self.db.execute("SELECT count(*) FROM release_verifications").fetchone()[0],2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM recognized_episodes").fetchone()[0],2)
        self.assertEqual(recognized_episode(self.db,aid=1001,tid=2001,origin='tvdb',season=2,number=1)['anidb_episode_id'],10012)
        self.assertEqual(recognized_episode(self.db,aid=1001,tid=2001,origin='anidb',season=None,number=3)['tvdb_season'],1)
        self.assertIsNone(recognized_episode(self.db,aid=1001,tid=2001,origin='tvdb',season=2,number=5))

    def test_idempotence_and_verified_provenance_enforced(self):
        self.remember(11,1)
        self.remember(11,1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM recognized_aliases").fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM recognized_episodes").fetchone()[0],1)
        source=SimpleNamespace(anidb_id=999,tvdb_series_id=2001,
            raw_body=b'{"release_name":"Untrusted S01E01"}')
        self.db.execute("BEGIN")
        with self.assertRaises(RecognitionConflict):
            remember_verified_release(self.db,verification_id=11,source=source,season=1)
        self.db.rollback()
        self.assertIsNone(recognized_for_release(self.db,"Untrusted S01E01",season=1))


if __name__=="__main__":
    unittest.main()
