"""Offline checks for Search again cache safety (no qBittorrent or Prowlarr)."""
import json
import sqlite3
import time
import unittest

from src.anime.storage.cached_release_retry import (
    _cached, clear_cached, expire_cached, RETENTION_MS,
)

A='a'*40
B='b'*40


class CachedRetryTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE episode_release_candidates (
                plan_id INTEGER,anidb_episode_number INTEGER,rank INTEGER,
                info_hash TEXT,state TEXT,reason_code TEXT
            );
            CREATE TABLE magnet_intents (
                event_id INTEGER,job_id INTEGER,plan_id INTEGER,anidb_episode_number INTEGER,
                info_hash TEXT,state TEXT,created_at_ms INTEGER
            );
            CREATE TABLE incoming_events (
                event_id INTEGER PRIMARY KEY,source TEXT,raw_body BLOB
            );
            CREATE TABLE job_operation_members (job_id INTEGER,operation_id INTEGER);
            CREATE TABLE job_operations (
                operation_id INTEGER,state TEXT,cleanup_state TEXT
            );
        """)
        self.now=time.time_ns()//1000000
        self.add(A,1,1,'TORRENT_METADATA_TIMEOUT','[Group] Show - 01 [Dual Audio]')

    def add(self,digest,rank,event,reason,title,*,created=None,operation_state='CANCELED'):
        body=json.dumps({
            'torrent_infohash':digest,
            'source_release_name':title,
            'release_name':'Show - 01',
            'magnet_uri':'magnet:?xt=urn:btih:'+digest
        }).encode()
        self.db.execute("INSERT INTO episode_release_candidates VALUES(1,1,?,?,'FAILED',?)",
                        (rank,digest,reason))
        self.db.execute("INSERT INTO incoming_events VALUES(?,'release-discovery',?)",(event,body))
        self.db.execute("INSERT INTO magnet_intents VALUES(?,?,?,?,?,'RELEASED',?)",
                        (event,event,1,1,digest,self.now if created is None else created))
        self.db.execute("INSERT INTO job_operation_members VALUES(?,?)",(event,event))
        self.db.execute("INSERT INTO job_operations VALUES(? ,?,'COMPLETE')",
                        (event,operation_state))

    def candidates(self):
        return list(_cached(self.db,1,1,True,self.now))

    def test_replay_from_cleaned_event_and_no_duplicate_magnet_storage(self):
        self.assertEqual([c.info_hash for c in self.candidates()],[A])

    def test_already_replayed_hash_is_not_offered_twice(self):
        self.db.execute("INSERT INTO magnet_intents VALUES(7,7,1,1,?,'RELEASED',?)",
                        (A,self.now))
        self.assertEqual(self.candidates(),[])

    def test_audio_failed_and_unowned_candidates_not_replayed(self):
        self.add(B,2,2,'MEDIA_AUDIO_REQUIRED','[Group] Show - 01 [Dual Audio]')
        self.assertEqual([c.info_hash for c in self.candidates()],[A])
        self.db.execute("UPDATE job_operations SET state='ACTIVE' WHERE operation_id=1")
        self.assertEqual(self.candidates(),[])

    def test_english_dub_alone_never_replayed(self):
        self.db.execute("UPDATE incoming_events SET raw_body=? WHERE event_id=1",
            (json.dumps({'torrent_infohash':A,'source_release_name':'Show - 01 [English Dub]',
                         'release_name':'Show - 01',
                         'magnet_uri':'magnet:?xt=urn:btih:'+A}).encode(),))
        self.assertEqual(self.candidates(),[])

    def test_delete_cache_preserves_history_fields(self):
        self.assertEqual(clear_cached(self.db,1,1),1)
        saved=json.loads(self.db.execute('SELECT raw_body FROM incoming_events').fetchone()[0])
        self.assertNotIn('magnet_uri',saved)
        self.assertEqual(saved['torrent_infohash'],A)
        self.assertEqual(self.candidates(),[])
        self.assertEqual(clear_cached(self.db,1,1),0)

    def test_expiry_scrubs_only_cleaned_old_magnets(self):
        self.db.execute("UPDATE magnet_intents SET created_at_ms=?",
                        (self.now-RETENTION_MS-1,))
        self.assertEqual(expire_cached(self.db,self.now),1)
        self.assertEqual(self.candidates(),[])


if __name__=='__main__':
    unittest.main()
