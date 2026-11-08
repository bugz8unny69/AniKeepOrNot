"""Offline verification-to-staging handoff and protected completion; unrun."""
from functools import partial
from pathlib import Path
import unittest

from src.nextgen.acquisition.media import verify_file
from src.nextgen.jobs.verify_media_job import registration as verify_registration
from src.nextgen.jobs.stage_user_evaluation_media_job import registration as stage_registration
from src.nextgen.staging import Publisher
from src.nextgen.storage.cancellation import CancellationStore
from src.nextgen.jobs.cleanup_canceled_operation_job import registration as cleanup_registration
from src.nextgen.tests.test_torrent_submission import Secrets
from src.nextgen.storage.sqlite import connect, transaction
from src.nextgen.tests.test_torrent_transfer import TorrentTransferTests
from src.nextgen.tests.test_verify_media_job import VerifyMediaJobTests
from src.nextgen.tests.test_acquisition_preparation import AcquisitionPreparationTests
from src.nextgen.tests.test_media_verification import probe_bytes


class StageMediaJobTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    queued=TorrentTransferTests.queued
    due=TorrentTransferTests.due
    ready=VerifyMediaJobTests.ready

    def worker(self,publisher=None,additional=()):
        verification=verify_registration(verify=partial(verify_file,read_probe=lambda *_:probe_bytes()),staging_kind='STAGE_TRIAL_MEDIA')
        staging=stage_registration(publisher_factory=publisher or partial(Publisher,allow_root=True))
        return TorrentTransferTests.worker(self,verification=verification,additional=(staging,*additional))

    async def staged_ready(self,worker):
        event,preparation,path=await self.ready(worker)
        target=self.root/'staging';target.mkdir()
        self.store.update({'storage':{'trial_root':str(target)}},expected_revision=3)
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='STAGE_TRIAL_MEDIA'"),[('PENDING',)])
        return target,path

    async def test_verified_copy_metadata_and_done_commit_with_staged_milestone(self):
        worker=self.worker();target,source=await self.staged_ready(worker)
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='STAGE_TRIAL_MEDIA'"),[('DONE',)])
        self.assertEqual(self.rows('SELECT state FROM staging_publications'),[('PUBLISHED',)])
        self.assertEqual(self.rows("SELECT json_extract(manifest_json,'$.transfer') FROM staging_publications"),[('COPY',)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_operations WHERE state='STAGED'"),[(1,)])
        self.assertEqual(len(list(target.rglob('*.mkv'))),1)
        self.assertEqual(len(list(target.rglob('*.nfo'))),2)
        self.assertTrue(source.exists())
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='REFRESH_JELLYFIN'"),[(0,)])
        job=self.rows("SELECT job_id FROM jobs WHERE kind='STAGE_TRIAL_MEDIA'")[0][0]
        self.assertFalse(CancellationStore(self.database).status(job)['can_cancel'])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='KEEP_MEDIA'"),[(0,)])

    async def held_contender(self,worker):
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET available_at_ms=9999999999999 WHERE kind='STAGE_TRIAL_MEDIA'")
        finally:db.close()
        self.source()
        await worker.attempt();await worker.attempt()
        self.assertTrue(self.rows("SELECT 1 FROM jobs WHERE state='ON_HOLD' AND last_reason_code='EPISODE_RESERVED'"))

    def stage_due(self):
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET available_at_ms=0 WHERE kind='STAGE_TRIAL_MEDIA'")
        finally:db.close()

    async def test_staging_cancels_held_groups_and_late_releases_skip_without_download(self):
        from unittest.mock import patch
        worker=self.worker(additional=(cleanup_registration(Secrets()),));target,source=await self.staged_ready(worker)
        await self.held_contender(worker)
        await self.held_contender(worker)
        self.stage_due()
        records=[]
        with patch('src.nextgen.worker.log_history',side_effect=lambda logger,record:records.append(record)):
            await worker.attempt()
        self.assertEqual(self.rows("SELECT state,activity_code FROM jobs WHERE kind='RESERVE_EPISODE' ORDER BY job_id"),
            [('DONE',None),('CANCELED',None),('CANCELED',None)])
        self.assertEqual(sum(r.event_code=='CANCELED' and r.reason_code=='EPISODE_ALREADY_STAGED' for r in records),2)
        self.assertEqual(self.rows("SELECT count(*) FROM management_actions WHERE action='CLEANUP'"),[(2,)])
        await worker.attempt();await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM job_operations WHERE state='CANCELED' AND cleanup_state='COMPLETE'"),[(2,)])
        self.source();await worker.attempt();await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE activity_code='SKIPPED_DUPLICATE'"),[(1,)])
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(1,)])
        self.assertEqual(len(list(target.rglob('*.mkv'))),1)
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='PREPARE_ACQUISITION'"),[(1,)])

    async def test_stage_does_not_cancel_held_contender_for_another_episode(self):
        worker=self.worker();await self.staged_ready(worker)
        await self.held_contender(worker)
        # The unrelated held reservation must remain untouched. The regular
        # contender (same episode) will still be canceled on stage completion.
        db=connect(self.database)
        try:
            with transaction(db):
                other=db.execute("SELECT job_id FROM jobs WHERE state='ON_HOLD' LIMIT 1").fetchone()[0]
                db.execute("UPDATE jobs SET last_reason_code='EPISODE_RESERVATION_CONFLICT' WHERE job_id=?",(other,))
        finally:db.close()
        self.stage_due();await worker.attempt()
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(other,)),
                         [('ON_HOLD','EPISODE_RESERVATION_CONFLICT')])
        self.assertEqual(self.rows("SELECT count(*) FROM management_actions WHERE action='CLEANUP'"),[(0,)])

    async def test_partial_staging_does_not_skip_waiters(self):
        from unittest.mock import patch
        class FailMetadata(Publisher):
            def _copy(self,plan,entry,fd):
                if entry['role']=='episode':raise OSError('metadata unavailable')
                return super()._copy(plan,entry,fd)
        worker=self.worker(partial(FailMetadata,allow_root=True))
        await self.staged_ready(worker);await self.held_contender(worker);self.stage_due()
        with patch('src.nextgen.jobs.stage_user_evaluation_media_job.exception') as diagnostic:
            await worker.attempt()
        self.assertEqual(diagnostic.call_args.kwargs['reason_code'],'STAGING_UNAVAILABLE')
        self.assertEqual(diagnostic.call_args.args[1],'OPERATION_FAILED')
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE state='ON_HOLD' AND last_reason_code='EPISODE_RESERVED'"),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE activity_code='SKIPPED_DUPLICATE'"),[(0,)])

    async def test_staging_transaction_rolls_back_duplicate_closure_on_failure(self):
        from unittest.mock import patch
        from src.nextgen.storage.job_queue import QueueUnavailable
        from src.nextgen.storage.episode_reservations import cancel_staged_waiters
        worker=self.worker();await self.staged_ready(worker)
        await self.held_contender(worker);self.stage_due()
        def fail_after_closure(*args):
            cancel_staged_waiters(*args)
            raise OSError('interrupted transaction')
        with patch('src.nextgen.storage.episode_reservations.cancel_staged_waiters',side_effect=fail_after_closure):
            with self.assertRaises(QueueUnavailable):await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM staging_publications'),[('PLANNED',)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE state='ON_HOLD' AND last_reason_code='EPISODE_RESERVED'"),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='CANCELED'"),[(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM management_actions"),[(0,)])

    async def test_reservation_conflict_at_commit_stops_for_review_instead_of_crash_looping(self):
        from unittest.mock import patch
        from src.nextgen.episode_reservations import ReservationConflict
        worker=self.worker();await self.staged_ready(worker)
        with patch('src.nextgen.storage.episode_reservations.cancel_staged_waiters',side_effect=ReservationConflict()):
            self.assertTrue(await worker.attempt())
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='STAGE_TRIAL_MEDIA'"),
                         [('NEEDS_REVIEW','EPISODE_RESERVATION_CONFLICT')])
        # The refused save rolled back; nothing was marked published.
        self.assertEqual(self.rows('SELECT state FROM staging_publications'),[('PLANNED',)])

    async def test_staging_does_not_complete_canceled_competitor(self):
        worker=self.worker();await self.staged_ready(worker)
        await self.held_contender(worker)
        job=self.rows("SELECT job_id FROM jobs WHERE state='ON_HOLD'")[0][0]
        CancellationStore(self.database).request(job)
        before=self.rows('SELECT state,activity_code FROM jobs WHERE job_id=?',(job,))
        self.stage_due();await worker.attempt()
        self.assertEqual(self.rows('SELECT state,activity_code FROM jobs WHERE job_id=?',(job,)),before)

    async def test_running_reservation_rechecks_publication_at_commit(self):
        from src.nextgen.storage.episode_reservations import load_reservation_input
        from src.nextgen.job_contracts import CompletionRejected, Outcome
        worker=self.worker();await self.staged_ready(worker)
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET available_at_ms=9999999999999 WHERE kind='STAGE_TRIAL_MEDIA'")
        finally:db.close()
        event,evaluation=self.source();await worker.attempt()
        claim,_=worker.queue.claim();spec=worker.registry.entries['RESERVE_EPISODE']
        worker.queue.start(claim,spec,'a'*32,worker.registry)
        value=load_reservation_input(self.database,evaluation,event)
        self.stage_due();await worker.attempt()
        with self.assertRaises(CompletionRejected) as caught:
            worker.queue.finish(claim,Outcome(value=value),spec=spec,invocation='a'*32,registry=worker.registry)
        self.assertEqual(caught.exception.outcome.kind,'skip')
        worker.queue.finish(claim,caught.exception.outcome,spec=spec,invocation='a'*32,registry=worker.registry)
        self.assertEqual(self.rows('SELECT state,activity_code FROM jobs WHERE job_id=?',(claim.job_id,)),[('DONE','SKIPPED_DUPLICATE')])
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(1,)])

    async def test_restart_reconciles_missed_staged_cancellation_idempotently(self):
        from unittest.mock import patch
        worker=self.worker();await self.staged_ready(worker)
        await self.held_contender(worker);self.stage_due()
        # Simulate a pre-upgrade staged commit that left a held contender behind.
        with patch('src.nextgen.storage.episode_reservations.cancel_staged_waiters',return_value=()):
            await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE state='ON_HOLD' AND last_reason_code='EPISODE_RESERVED'"),[(1,)])
        await worker._recover()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='RESERVE_EPISODE' AND state='CANCELED'"),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM management_actions WHERE action='CLEANUP'"),[(1,)])
        await worker._recover()
        self.assertEqual(self.rows("SELECT count(*) FROM management_actions WHERE action='CLEANUP'"),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM episode_reservations WHERE state='ACTIVE'"),[(1,)])

    async def test_metadata_failure_retains_video_and_does_not_mark_staged(self):
        class FailMetadata(Publisher):
            def _copy(self,plan,entry,fd):
                if entry['role']=='episode':raise OSError('metadata unavailable')
                return super()._copy(plan,entry,fd)
        worker=self.worker(partial(FailMetadata,allow_root=True));target,source=await self.staged_ready(worker)
        await worker.attempt()
        self.assertEqual(len(list(target.rglob('*.mkv'))),1)
        self.assertTrue(source.exists())
        self.assertEqual(self.rows("SELECT count(*) FROM job_operations WHERE state='STAGED'"),[(0,)])
        self.assertEqual(self.rows('SELECT state FROM staging_publications'),[('PLANNED',)])

    async def test_cancel_after_copy_fences_database_completion(self):
        test=self
        class CancelAfter(Publisher):
            def publish(self,plan):
                receipts=super().publish(plan)
                CancellationStore(test.database).request(self.claim.job_id)
                return receipts
        worker=self.worker(partial(CancelAfter,allow_root=True));target,source=await self.staged_ready(worker)
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM job_operations WHERE state='STAGED'"),[(0,)])
        self.assertEqual(self.rows('SELECT state FROM staging_publications'),[('PLANNED',)])
        self.assertEqual(len(list(target.rglob('*.mkv'))),1)
        self.assertTrue(source.exists())

    async def test_retry_uses_pinned_root_after_configuration_change(self):
        class LoseConfirmation(Publisher):
            once=True
            def publish(self,plan):
                receipts=super().publish(plan)
                if type(self).once:type(self).once=False;raise OSError('lost response')
                return receipts
        worker=self.worker(partial(LoseConfirmation,allow_root=True));first,source=await self.staged_ready(worker)
        await worker.attempt()
        second=self.root/'second';second.mkdir()
        self.store.update({'storage':{'trial_root':str(second)}},expected_revision=4)
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET available_at_ms=0 WHERE kind='STAGE_TRIAL_MEDIA' AND state='RETRY_WAIT'")
        finally:db.close()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM staging_publications'),[('PUBLISHED',)])
        self.assertEqual(len(list(first.rglob('*.mkv'))),1)
        self.assertEqual(list(second.iterdir()),[])


class CompletedStepLabelsTests(unittest.TestCase):
    def test_completed_identity_is_not_presented_as_staged(self):
        from src.nextgen.messages import DEFAULT_CATALOG as catalog
        self.assertEqual(catalog.job_status('DONE',kind='IDENTIFY_ANIME')['message'],'Series identified')
        self.assertEqual(catalog.job_status('DONE',kind='VERIFY_RELEASE')['message'],'Episode verified')
        self.assertEqual(catalog.job_status('DONE',kind='STAGE_TRIAL_MEDIA')['message'],'Staged')
        self.assertEqual(catalog.job_status('DONE')['message'],'Step completed')
        self.assertEqual(catalog.job_status('DONE','SKIPPED_DUPLICATE',kind='RESERVE_EPISODE')['message'],'Skipped duplicate')
        self.assertEqual(catalog.job_status('NEEDS_REVIEW',kind='STAGE_TRIAL_MEDIA')['message'],'Needs review')
