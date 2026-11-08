"""Offline future staging receipt/cancellation boundary; unexecuted."""
import unittest
from src.anime.cancellation_service import CancellationService
from src.anime.staging_artifacts import record_temporary, publication
from src.anime.storage.cancellation import mark_staged
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import connect, transaction, enqueue_job
from src.anime.tests import test_acquisition_preparation as fixture
from src.anime.tests.test_torrent_submission import Secrets


class StagingCancellationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.AcquisitionPreparationTests.asyncSetUp
    rows=fixture.AcquisitionPreparationTests.rows

    def stage(self):
        self.name='.ad-'+'b'*32+'.partial'
        self.path=self.root/self.name;self.path.write_bytes(b'partial')
        db=connect(self.database)
        try:
            with transaction(db):job=enqueue_job(db,kind='STAGE_USER_EVALUATION_MEDIA',payload={},component='worker',available_at_ms=0)
        finally:db.close()
        claim,_=JobQueue(self.database).claim()
        db=connect(self.database)
        try:
            with transaction(db):aid=record_temporary(db,claim,str(self.root),self.name)
        finally:db.close()
        return job,claim,aid

    def service(self):return CancellationService(self.store.read(),Secrets())

    async def test_failed_stage_cancel_removes_only_registered_temporary(self):
        job,claim,aid=self.stage()
        other=self.root/'published.mkv';other.write_bytes(b'preserved')
        self.assertEqual(self.service().cancel(job)['cleanup_state'],'COMPLETE')
        self.assertFalse(self.path.exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.rows('SELECT phase FROM staging_cleanup_artifacts'),[('REMOVED',)])

    async def test_uncertain_publication_temporary_is_deleted_without_review(self):
        job,claim,aid=self.stage();db=connect(self.database)
        try:
            with transaction(db):publication(db,claim,aid)
        finally:db.close()
        self.assertEqual(self.service().cancel(job)['cleanup_state'],'COMPLETE')
        self.assertFalse(self.path.exists())

    async def test_completed_staging_hides_cancel_and_never_deletes_published_media(self):
        job,claim,aid=self.stage();db=connect(self.database)
        try:
            with transaction(db):
                publication(db,claim,aid);publication(db,claim,aid,confirmed=True)
                mark_staged(db,claim)
                db.execute("UPDATE jobs SET state='DONE',lease_token=NULL,lease_expires_at_ms=NULL WHERE job_id=?",(job,))
        finally:db.close()
        status=self.service().status(job)
        self.assertFalse(status['can_cancel']);self.assertFalse(status['can_cleanup'])
        self.assertEqual(status['state'],'STAGED')
        self.assertTrue(self.path.exists())
