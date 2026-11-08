"""Offline additive v27 batch-proof persistence and fenced ownership tests."""
from pathlib import Path
import time
import tempfile
import unittest

from src.anime.acquisition.metadata import BatchInventory, TorrentFile
from src.anime.batch.verification import verify_batch_files
from src.anime.providers.http import Credentials
from src.anime.release_contracts import ReleaseConflict
from src.anime.storage.batch_verifications import persist_batch_verification
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.releases import load_release
from src.anime.storage.migrations import LATEST_VERSION
from src.anime.storage.sqlite import (
    connect, initialize, accept_event_and_job, save_identity, transaction,
)


class BatchPersistenceTests(unittest.TestCase):
    def setUp(self):
        root=tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.database=Path(root.name)/'anime.sqlite'
        self.info_hash='a'*40
        self.inventory=BatchInventory(self.info_hash,'b'*64,(
            TorrentFile(0,'Shiki/Season 01/Shiki S01E01.mkv',100),
            TorrentFile(1,'Shiki/Season 01/Shiki S01E02.mkv',100),
            TorrentFile(2,'Shiki/OVA/Shiki OVA 01.mkv',50)))
        db=connect(self.database)
        initialize(db)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone(),(LATEST_VERSION,))
        self.event,self.job=accept_event_and_job(
            db,'test',('{"release_name":"Shiki S01 + OVA","torrent_infohash":"%s"}' %
                       self.info_hash).encode(),kind='VERIFY_RELEASE',payload={'anidb_id':123})
        save_identity(db,self.event,{'schema_version':2,'anime':{
            'anidb_id':123,'tvdb_series_id':456,'title':'Shiki'}})
        db.close()
        class Client:
            def episodes(self,aid): return {1:1001,2:1002}
            def mapping_xml(self):
                return b'<anime-list><anime anidbid="123" tvdbid="456" defaulttvdbseason="1"/></anime-list>'
            def episode_page(self,tid,page,cred): return ((2001,1,1),(2002,1,2)),False
        self.verified=verify_batch_files(self.inventory,client=Client(),
            credentials=Credentials('secret'),anidb_id=123,tvdb_series_id=456,season=1)
        self.now=int(time.time()*1000)+1000
        self.queue=JobQueue(self.database,clock=lambda:self.now)

    def count(self,table):
        db=connect(self.database)
        try:return db.execute('SELECT count(*) FROM '+table).fetchone()[0]
        finally:db.close()

    def test_persist_all_regular_files_as_immutable_claim_owned_facts(self):
        claim,_=self.queue.claim()
        source=load_release(self.database,self.event,123)
        with self.queue.connection() as db,transaction(db):
            batch=persist_batch_verification(db,claim,source,self.inventory,self.verified,now_ms=self.now+1)
            self.assertGreater(batch,0)
        self.assertEqual(self.count('batch_verifications'),1)
        self.assertEqual(self.count('batch_verified_files'),2)
        db=connect(self.database)
        try:
            self.assertEqual(db.execute('SELECT file_index,tvdb_episode FROM batch_verified_files '
                                        'ORDER BY file_index').fetchall(),[(0,1),(1,2)])
        finally:db.close()

    def test_stale_claim_and_mismatched_metadata_never_save_facts(self):
        from dataclasses import replace
        claim,_=self.queue.claim()
        source=load_release(self.database,self.event,123)
        with self.queue.connection() as db,transaction(db):
            with self.assertRaises(ReleaseConflict):
                persist_batch_verification(db,claim,source,self.inventory,self.verified,now_ms=self.now+999999)
        self.assertEqual(self.count('batch_verifications'),0)
        changed=replace(self.verified,info_sha256='c'*64)
        with self.queue.connection() as db,transaction(db):
            with self.assertRaises(ReleaseConflict):
                persist_batch_verification(db,claim,source,self.inventory,changed,now_ms=self.now+1)
        self.assertEqual(self.count('batch_verifications'),0)
