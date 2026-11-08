"""Offline download handoff, immutable completion and cancellation; unrun."""
from dataclasses import replace
from functools import partial
from pathlib import Path
import unittest

from src.anime.acquisition.media import verify_file
from src.anime.jobs.verify_media_job import registration
from src.anime.storage.cancellation import CancellationStore
from src.anime.storage.sqlite import connect, transaction, enqueue_job
from src.anime.tests.test_torrent_transfer import TorrentTransferTests
from src.anime.tests.test_acquisition_preparation import AcquisitionPreparationTests
from src.anime.tests.test_media_verification import probe_bytes


class VerifyMediaJobTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    queued=TorrentTransferTests.queued
    due=TorrentTransferTests.due

    def worker(self,verify=None):
        return TorrentTransferTests.worker(self,verification=registration(verify=verify or partial(verify_file,
            read_probe=lambda *_:probe_bytes())))

    async def ready(self,worker):
        self.store.update({'storage':{'download_root':str(self.root/'downloads')}},expected_revision=2)
        event,preparation=await self.queued(worker)
        await worker.attempt()
        root=Path(self.targets[-1].download_root);root.mkdir(parents=True)
        selected=self.snapshot.observation.files[self.snapshot.priorities.index(1)]
        path=root/selected.path;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'x'*selected.size)
        self.snapshot=replace(self.snapshot,progress=(1,))
        self.due();await worker.attempt()
        return event,preparation,path

    async def test_download_hands_off_once_and_persists_digest_without_marking_staged(self):
        worker=self.worker();await self.ready(worker)
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='VERIFY_MEDIA'"),[('PENDING',)])
        self.assertEqual(self.rows('SELECT * FROM media_verifications'),[])
        await worker.attempt();await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='VERIFY_MEDIA'"),[('DONE',)])
        self.assertEqual(self.rows('SELECT count(*) FROM media_verifications'),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_operations WHERE state='STAGED'"),[(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='KEEP_MEDIA'"),[(0,)])
        self.assertEqual(self.starts,1)

    async def test_missing_audio_enters_review_without_publishing_evidence(self):
        verify=partial(verify_file,read_probe=lambda *_:probe_bytes(languages=('jpn',)))
        worker=self.worker(verify);_,_,path=await self.ready(worker);await worker.attempt()
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='VERIFY_MEDIA'"),
                         [('FAILED','MEDIA_AUDIO_REQUIRED')])
        self.assertTrue(path.exists())
        self.assertEqual(self.rows('SELECT * FROM media_verifications'),[])

    async def test_cancellation_during_verification_fences_evidence_commit(self):
        def verify(intent,record,download,flag,**kwargs):
            value=verify_file(intent,record,download,flag,read_probe=lambda *_:probe_bytes(),**kwargs)
            CancellationStore(self.database).request(download)
            return value
        worker=self.worker(verify);await self.ready(worker);await worker.attempt()
        self.assertEqual(self.rows('SELECT * FROM media_verifications'),[])
        self.assertNotEqual(self.rows("SELECT state FROM jobs WHERE kind='VERIFY_MEDIA'"),[('DONE',)])

    async def test_lease_loss_during_verification_rejects_result(self):
        def verify(intent,record,download,flag,**kwargs):
            value=verify_file(intent,record,download,flag,read_probe=lambda *_:probe_bytes(),**kwargs)
            db=connect(self.database)
            try:
                with transaction(db):db.execute("UPDATE jobs SET lease_token='replacement' WHERE kind='VERIFY_MEDIA'")
            finally:db.close()
            return value
        worker=self.worker(verify);await self.ready(worker);await worker.attempt()
        self.assertEqual(self.rows('SELECT * FROM media_verifications'),[])
        self.assertNotEqual(self.rows("SELECT state FROM jobs WHERE kind='VERIFY_MEDIA'"),[('DONE',)])

    async def test_another_operation_cannot_reuse_download_payload(self):
        worker=self.worker();event,preparation,_=await self.ready(worker)
        db=connect(self.database)
        try:
            download=db.execute("SELECT job_id FROM jobs WHERE kind='DOWNLOAD_TORRENT'").fetchone()[0]
            with transaction(db):
                db.execute("UPDATE jobs SET available_at_ms=9999999999999 WHERE kind='VERIFY_MEDIA'")
                job=enqueue_job(db,kind='VERIFY_MEDIA',payload={'preparation_job_id':preparation,'download_job_id':download},
                                component='worker',event_id=event,available_at_ms=0)
        finally:db.close()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(job,)),
                         [('NEEDS_REVIEW','TORRENT_TRANSFER_CONFLICT')])
        self.assertEqual(self.rows('SELECT * FROM media_verifications'),[])
