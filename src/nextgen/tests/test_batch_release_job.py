"""Offline worker transaction: verified batch facts are durable, not acquired."""
import tempfile
from pathlib import Path
import unittest

from src.nextgen.acquisition.metadata import BatchInventory, TorrentFile
from src.nextgen.batch.provider_worker import BatchVerificationCompletion
from src.nextgen.batch.verification import verify_batch_files
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.job_contracts import Registry, Policy
from src.nextgen.jobs.verify_release_job import registration
from src.nextgen.providers.http import Credentials
from src.nextgen.storage.releases import load_release
from src.nextgen.storage.sqlite import (
    connect, initialize, accept_event_and_job, save_identity,
)
from src.nextgen.worker import Worker


class FakeProvider:
    def episodes(self,aid): return {1:1001,2:1002}
    def mapping_xml(self):
        return b'<anime-list><anime anidbid="123" tvdbid="456" defaulttvdbseason="1"/></anime-list>'
    def episode_page(self,tid,page,secret):return ((2001,1,1),(2002,1,2)),False


class BatchReleaseJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.database=root/'nextgen.sqlite'
        self.store=ConfigurationStore(root)
        self.startup=self.store.initialize({'storage':{'database_path':str(self.database)}}).snapshot
        db=connect(self.database)
        initialize(db)
        self.event,self.job=accept_event_and_job(
            db,'test',b'{"release_name":"Shiki (S01 + OVA)","torrent_infohash":"'
            +b'a'*40+b'"}',kind='VERIFY_RELEASE',payload={'anidb_id':123})
        save_identity(db,self.event,{'schema_version':2,'anime':{
            'anidb_id':123,'tvdb_series_id':456,'title':'Shiki'}})
        db.close()
        self.inventory=BatchInventory('a'*40,'b'*64,(
            TorrentFile(0,'Shiki/Season 01/Shiki S01E01.mkv',100),
            TorrentFile(1,'Shiki/Season 01/Shiki S01E02.mkv',100),
            TorrentFile(2,'Shiki/OVA/Shiki OVA 01.mkv',100)))
        self.verified=verify_batch_files(
            self.inventory,client=FakeProvider(),credentials=Credentials('private'),
            anidb_id=123,tvdb_series_id=456,season=1)

    def rows(self,sql):
        db=connect(self.database)
        try:return db.execute(sql).fetchall()
        finally:db.close()

    async def test_worker_persists_fenced_batch_but_creates_no_single_episode_user_evaluation(self):
        async def verify(source,*args):
            return BatchVerificationCompletion(source,self.inventory,self.verified)
        worker=Worker(self.store,self.startup,Registry((registration(verify),)),
                      policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT kind,state FROM jobs'),
                         [('VERIFY_RELEASE','DONE')])
        self.assertEqual(self.rows('SELECT count(*) FROM release_verifications'),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM batch_verifications'),[(1,)])
        self.assertEqual(self.rows(
            'SELECT file_index,tvdb_episode FROM batch_verified_files ORDER BY file_index'),
            [(0,1),(1,2)])
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM torrent_submissions'),[(0,)])
