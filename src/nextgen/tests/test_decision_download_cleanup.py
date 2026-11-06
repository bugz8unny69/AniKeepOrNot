"""Offline decision/download handoffs and one-shot recovery; deliberately unrun."""
from dataclasses import replace
from functools import partial
from pathlib import Path
import unittest

from src.nextgen.acquisition.submission import ClientUnavailable
from src.nextgen.decision_download_cleanup import KIND, Service, registration
from src.nextgen.job_contracts import Registry
from src.nextgen.storage.management_actions import ManagementActions
from src.nextgen.storage.sqlite import connect, enqueue_job, transaction
from src.nextgen.tests.test_acquisition_preparation import AcquisitionPreparationTests
from src.nextgen.tests.test_keep_sonarr import KeepSonarrTests
from src.nextgen.tests.test_post_keep_cleanup import PostKeepCleanupTests
from src.nextgen.tests.test_stage_media_job import StageMediaJobTests
from src.nextgen.tests.test_torrent_submission import Secrets
from src.nextgen.tests.test_torrent_transfer import TorrentTransferTests
from src.nextgen.tests.test_verify_media_job import VerifyMediaJobTests
from src.nextgen.worker import Worker


class DecisionDownloadCleanupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    queued=TorrentTransferTests.queued
    due=TorrentTransferTests.due
    ready=VerifyMediaJobTests.ready
    staged_ready=StageMediaJobTests.staged_ready
    prepared=KeepSonarrTests.prepared
    make_due=KeepSonarrTests.make_due
    cleanup_ready=PostKeepCleanupTests.cleanup_ready

    async def staged(self):
        worker=StageMediaJobTests.worker(self)
        target,source=await self.staged_ready(worker)
        await worker.attempt()
        job=self.rows("SELECT job_id FROM jobs WHERE kind='STAGE_TRIAL_MEDIA'")[0][0]
        return worker,target,job

    def attach(self,worker,mode='normal',*,preserve_controls=False):
        test=self
        if not preserve_controls:
            self.controls=[]
            self.delete_files_args=[]
        class Client:
            def __init__(self,*args):pass
            def login(self):pass
            def inspect(self,expected):return None if test.snapshot is None else test.snapshot.observation
            def cleanup_control(self,intent,fingerprint,permit,action,*,delete_files=True):
                permit.take(fingerprint,action);test.controls.append(action)
                if action=='remove':test.delete_files_args.append(delete_files)
                if action=='stop':
                    first_delayed=mode=='stop_delayed' and test.controls.count('stop')==1
                    if mode!='stop_present' and not first_delayed:
                        test.snapshot=replace(test.snapshot,observation=replace(test.snapshot.observation,state='stoppedDL'))
                    if mode in ('stop_lost','stop_present') or first_delayed:raise ClientUnavailable()
                else:
                    first_delayed=mode=='remove_delayed' and test.controls.count('remove')==1
                    if mode not in ('remove_present','remove_present_ok') and not first_delayed:
                        test.snapshot=None
                    if mode in ('remove_lost','remove_present','changed_after_remove') or first_delayed:
                        if mode=='changed_after_remove':test.remainder.write_bytes(b'changed')
                        raise ClientUnavailable()
        factory=partial(Service,client_factory=Client)
        # A recreated worker already has a cleanup registration; swap its
        # client-backed handler rather than registering the same kind twice.
        entries=tuple(item for item in worker.registry.entries.values() if item.kind!=KIND)
        worker.registry=Registry((*entries,registration(Secrets(),service_factory=factory)))

    def restart(self,previous,mode):
        # The normal fixture factory seeds configuration at revision 1. A true
        # restart reuses the persisted settings and handler registry instead
        # of trying to apply the initial configuration for a second time.
        restarted=Worker(self.store,self.startup,previous.registry,policy=previous.policy)
        self.addAsyncCleanup(restarted.close)
        self.attach(restarted,mode=mode,preserve_controls=True)
        return restarted

    async def download_ready(self,action='keep',mode='normal'):
        worker,fake,actions,target,decision,staging_cleanup=await self.cleanup_ready()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind=? AND json_extract(payload_json,'$.decision_job_id') IS NOT NULL",(KIND,)),[(0,)])
        await worker.attempt()
        job=self.rows("SELECT job_id FROM jobs WHERE kind=? AND json_extract(payload_json,'$.decision_job_id') IS NOT NULL",(KIND,))[0][0]
        self.payload_root=Path(self.snapshot.observation.save_path)
        self.remainder=self.payload_root/self.snapshot.observation.files[0].path
        self.remainder.parent.mkdir(parents=True,exist_ok=True)
        # A remaining declared download payload; staging now preserves the source.
        self.remainder.write_bytes(b'partial')
        self.attach(worker,mode)
        return worker,fake,actions,target,decision,job

    async def test_keep_cleans_download_once_without_reimport_or_releasing_kept_slot(self):
        worker,fake,actions,target,decision,job=await self.download_ready()
        permanent=fake.root/'Series'/'episode.mkv';before=permanent.read_bytes()
        await worker.attempt()
        self.assertEqual(self.controls,['stop','remove'])
        self.assertFalse(self.remainder.exists());self.assertFalse(self.payload_root.exists())  # empty ad-* folder removed
        self.assertEqual(permanent.read_bytes(),before);self.assertEqual(fake.posts.count('command'),1)
        self.assertEqual(self.rows('SELECT state FROM episode_reservations'),[('ACTIVE',)])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        _,receipt,_=actions.submit(target,'keep')
        self.assertTrue(receipt['download_cleanup']['completed'])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind=? AND json_extract(payload_json,'$.decision_job_id') IS NOT NULL",(KIND,)),[(1,)])

    async def test_staging_removes_the_torrent_and_its_empty_download_folder_at_once(self):
        worker,staging,stage_job=await self.staged()
        job=self.rows("SELECT job_id FROM jobs WHERE kind=? AND "
                      "json_extract(payload_json,'$.staging_job_id')=?",(KIND,stage_job))[0][0]
        # Not claimed the instant staging completes; due shortly after.
        self.assertFalse(await worker.attempt())
        self.payload_root=Path(self.snapshot.observation.save_path)
        self.remainder=self.payload_root/self.snapshot.observation.files[0].path
        self.remainder.parent.mkdir(parents=True,exist_ok=True)
        self.remainder.write_bytes(b'partial')
        self.attach(worker)
        await self.make_due(job);await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(self.controls,['stop','remove'])
        self.assertIsNone(self.snapshot)                       # Gone from qBittorrent.
        self.assertFalse(self.remainder.exists())              # Leftover files removed.
        self.assertEqual(len(list(staging.rglob('*.mkv'))),1)  # The staged video stays.
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('COMPLETE',)])
        self.assertFalse(self.payload_root.exists())           # Empty ad-* folder removed.

    async def finish_staging_cleanup_after_decision(self,action):
        worker,_,_,_,_,job=await self.download_ready(action)
        await worker.attempt()                      # Keep/Reject cleanup does the work first.
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(self.controls,['stop','remove'])
        staging=self.rows("SELECT job_id FROM jobs WHERE kind=? AND "
                          "json_extract(payload_json,'$.staging_job_id') IS NOT NULL",(KIND,))[0][0]
        await self.make_due(staging);await worker.attempt()
        # The staging cleanup that was still waiting finds nothing left to do.
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(staging,)),[('DONE',)])
        self.assertEqual(self.controls,['stop','remove'])

    async def test_keep_before_staging_cleanup_leaves_it_nothing_to_do(self):
        await self.finish_staging_cleanup_after_decision('keep')

    async def test_lost_remove_response_reconciles_without_another_delete(self):
        worker,_,_,_,_,job=await self.download_ready(mode='remove_lost')
        await worker.attempt();self.assertTrue(self.remainder.exists())
        await self.make_due(job);await worker.attempt()
        self.assertEqual(self.controls,['stop','remove']);self.assertFalse(self.remainder.exists())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])

    async def test_lost_stop_response_does_not_repeat_stop(self):
        worker,_,_,_,_,job=await self.download_ready(mode='stop_lost')
        await worker.attempt();await self.make_due(job);await worker.attempt()
        self.assertEqual(self.controls,['stop','remove'])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])

    async def test_unconfirmed_stop_recovers_after_worker_restart(self):
        worker,_,_,_,_,job=await self.download_ready(mode='stop_delayed')
        await worker.attempt()
        self.assertEqual(self.controls,['stop'])
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('STOP_UNCERTAIN',)])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('RETRY_WAIT',)])
        # A new Worker and Client resume against the same durable journal.
        restarted=self.restart(worker,mode='stop_delayed')
        await self.make_due(job)
        await restarted.attempt()
        self.assertEqual(self.controls,['stop','stop','remove'])
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('COMPLETE',)])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])

    async def test_unconfirmed_stop_rechecks_active_torrent_without_repeating_delete(self):
        worker,_,_,_,_,job=await self.download_ready(mode='stop_present')
        await worker.attempt()
        await self.make_due(job)
        await worker.attempt()
        self.assertEqual(self.controls,['stop','stop'])
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('STOP_UNCERTAIN',)])

    async def test_uncertain_remove_still_present_is_rechecked_and_retried(self):
        worker,_,_,_,_,job=await self.download_ready(mode='remove_present_ok')
        for _ in range(3):
            await self.make_due(job)
            await worker.attempt()
        self.assertEqual(self.controls,['stop','remove','remove','remove'])
        self.assertTrue(self.remainder.exists())
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('REMOVE_UNCERTAIN',)])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('RETRY_WAIT',)])

    async def test_remove_not_delivered_can_recover_after_worker_restart(self):
        worker,_,_,_,_,job=await self.download_ready(mode='remove_delayed')
        await worker.attempt()
        self.assertEqual(self.controls,['stop','remove'])
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('REMOVE_UNCERTAIN',)])
        restarted=self.restart(worker,mode='remove_delayed')
        await self.make_due(job)
        await restarted.attempt()
        self.assertEqual(self.controls,['stop','remove','remove'])
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('COMPLETE',)])
        self.assertFalse(self.remainder.exists())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])

    async def test_remove_retries_require_matching_owned_torrent(self):
        worker,_,_,_,_,job=await self.download_ready(mode='remove_present_ok')
        await worker.attempt()
        self.assertEqual(self.controls,['stop','remove'])
        self.snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,tags=()))
        await self.make_due(job)
        await worker.attempt()
        self.assertEqual(self.controls,['stop','remove'])
        self.assertTrue(self.remainder.exists())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW',)])

    async def test_remove_restarted_active_torrent_is_stopped_first(self):
        worker,_,_,_,_,job=await self.download_ready(mode='remove_present_ok')
        await worker.attempt()
        self.snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,state='downloading'))
        await self.make_due(job)
        await worker.attempt()
        self.assertEqual(self.controls,['stop','remove','stop','remove'])
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('REMOVE_UNCERTAIN',)])

    async def test_changed_file_after_lost_remove_is_purged_with_the_folder(self):
        # Staging confirmed + torrent gone: the owned ad-* folder goes whatever is in it.
        worker,_,_,_,_,job=await self.download_ready(mode='changed_after_remove')
        await worker.attempt();await self.make_due(job);await worker.attempt()
        self.assertFalse(self.payload_root.exists())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])

    async def test_unknown_file_is_purged_once_torrent_is_gone(self):
        worker,fake,actions,target,decision,job=await self.download_ready()
        unknown=self.payload_root/'unowned.txt';unknown.write_text('leftover')
        (self.payload_root/'sub').mkdir();(self.payload_root/'sub'/'x.part').write_text('x')
        await worker.attempt()
        self.assertEqual(self.controls,['stop','remove'])
        self.assertFalse(self.payload_root.exists())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(fake.posts.count('command'),1)

    async def test_purge_never_follows_a_swapped_folder(self):
        worker,_,_,_,_,job=await self.download_ready()
        import shutil
        moved=self.payload_root.with_name(self.payload_root.name+'.other')
        self.payload_root.rename(moved);self.payload_root.mkdir()
        await worker.attempt()
        self.assertTrue(moved.exists())                  # Not ours any more: untouched.
        self.assertNotEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        shutil.rmtree(moved)

    async def test_other_consumer_blocks_all_external_controls(self):
        worker,_,_,_,_,job=await self.download_ready()
        prep,event=self.rows("SELECT job_id,event_id FROM jobs WHERE kind='PREPARE_ACQUISITION'")[0]
        db=connect(self.database)
        try:
            with transaction(db):
                # Simulate a separately scoped historical consumer. New references
                # to STAGED work are correctly rejected by normal admission.
                historical=enqueue_job(db,kind='DOWNLOAD_TORRENT',payload={},component='api',event_id=event,available_at_ms=9999999999999)
                db.execute('UPDATE jobs SET payload_json=? WHERE job_id=?',('{"preparation_job_id":'+str(prep)+'}',historical))
        finally:db.close()
        await worker.attempt()
        self.assertEqual(self.controls,[]);self.assertTrue(self.remainder.exists())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW',)])

    async def test_changed_ownership_marker_preserves_download(self):
        worker,_,_,_,_,job=await self.download_ready()
        self.snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,tags=()))
        await worker.attempt()
        self.assertEqual(self.controls,[]);self.assertTrue(self.remainder.exists())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW',)])

    async def test_replaced_download_root_is_preserved_for_review(self):
        worker,_,_,_,_,job=await self.download_ready()
        old=self.payload_root.with_name(self.payload_root.name+'-original')
        self.payload_root.rename(old);self.payload_root.mkdir()
        replacement=self.payload_root/self.remainder.relative_to(self.payload_root)
        replacement.parent.mkdir(parents=True,exist_ok=True);replacement.write_bytes(b'new')
        await worker.attempt()
        self.assertNotIn('remove',self.controls);self.assertEqual(replacement.read_bytes(),b'new')
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW',)])

    async def test_legacy_move_missing_files_recovery_from_prepared(self):
        """WI-19 / Job #6030: Legacy MOVE plan reporting missingFiles from PREPARED recovers using deleteFiles=false."""
        worker,staging,stage_job=await self.staged()
        job=self.rows("SELECT job_id FROM jobs WHERE kind=? AND json_extract(payload_json,'$.staging_job_id')=?",(KIND,stage_job))[0][0]
        db=connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE staging_publications SET manifest_json = json_set(manifest_json, '$.transfer', 'MOVE') WHERE job_id=?",(stage_job,))
        finally:db.close()
        self.payload_root=Path(self.snapshot.observation.save_path)
        self.remainder=self.payload_root/self.snapshot.observation.files[0].path
        self.payload_root.mkdir(parents=True,exist_ok=True)
        # In legacy MOVE, the video file was moved to staging and is missing from download dir.
        if self.remainder.exists():self.remainder.unlink()
        self.snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,state='missingFiles'))
        self.attach(worker)
        await self.make_due(job);await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(self.controls,['remove'])
        self.assertEqual(self.delete_files_args,[False])
        self.assertIsNone(self.snapshot)
        self.assertEqual(len(list(staging.rglob('*.mkv'))),1)
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('COMPLETE',)])
        # Repeat execution does not issue another delete
        self.controls.clear();self.delete_files_args.clear()
        await worker.attempt()
        self.assertEqual(self.controls,[])

    async def test_legacy_move_missing_files_recovery_from_stop_uncertain(self):
        """WI-19 / Job #3089: Legacy MOVE plan in STOP_UNCERTAIN recovers without resetting journal."""
        worker,staging,stage_job=await self.staged()
        job=self.rows("SELECT job_id FROM jobs WHERE kind=? AND json_extract(payload_json,'$.staging_job_id')=?",(KIND,stage_job))[0][0]
        db=connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE staging_publications SET manifest_json = json_set(manifest_json, '$.transfer', 'MOVE') WHERE job_id=?",(stage_job,))
                plan_id=self.rows('SELECT plan_id FROM staging_publications WHERE job_id=?',(stage_job,))[0][0]
                op_id=self.rows('SELECT operation_id FROM job_operation_members WHERE job_id=?',(stage_job,))[0][0]
                fp=self.rows('SELECT fingerprint FROM torrent_submissions WHERE plan_id=?',(plan_id,))[0][0]
                db.execute("INSERT INTO cancellation_cleanup VALUES(?,?,?,'{}','STOP_UNCERTAIN',1000,1000)",
                           (plan_id,op_id,fp))
        finally:db.close()
        self.payload_root=Path(self.snapshot.observation.save_path)
        self.remainder=self.payload_root/self.snapshot.observation.files[0].path
        self.payload_root.mkdir(parents=True,exist_ok=True)
        if self.remainder.exists():self.remainder.unlink()
        self.snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,state='missingFiles'))
        self.attach(worker)
        await self.make_due(job);await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(self.controls,['remove'])
        self.assertEqual(self.delete_files_args,[False])
        self.assertIsNone(self.snapshot)
        self.assertEqual(self.rows('SELECT phase FROM cancellation_cleanup'),[('COMPLETE',)])

    async def test_copy_missing_files_is_rejected_without_deletion(self):
        """WI-19: COPY plans reporting missingFiles must NOT be deleted."""
        worker,staging,stage_job=await self.staged()
        job=self.rows("SELECT job_id FROM jobs WHERE kind=? AND json_extract(payload_json,'$.staging_job_id')=?",(KIND,stage_job))[0][0]
        self.payload_root=Path(self.snapshot.observation.save_path)
        self.remainder=self.payload_root/self.snapshot.observation.files[0].path
        self.payload_root.mkdir(parents=True,exist_ok=True)
        if self.remainder.exists():self.remainder.unlink()
        self.snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,state='missingFiles'))
        self.attach(worker)
        await self.make_due(job);await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW',)])
        self.assertEqual(self.controls,[])
        self.assertIsNotNone(self.snapshot)

    async def test_retry_cleanup_capability_and_execution(self):
        """WI-20: Retry Cleanup for CLEANUP_DECIDED_DOWNLOAD jobs in NEEDS_REVIEW."""
        from src.nextgen.storage.review_retry import capability, request_review_retry, RetryStale
        from src.nextgen.storage.cancellation import CancellationStore, CancellationError
        worker,staging,stage_job=await self.staged()
        job=self.rows("SELECT job_id FROM jobs WHERE kind=? AND json_extract(payload_json,'$.staging_job_id')=?",(KIND,stage_job))[0][0]
        self.payload_root=Path(self.snapshot.observation.save_path)
        self.remainder=self.payload_root/self.snapshot.observation.files[0].path
        self.payload_root.mkdir(parents=True,exist_ok=True)
        if self.remainder.exists():self.remainder.unlink()
        self.snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,state='missingFiles'))
        self.attach(worker)
        await self.make_due(job);await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW',)])

        db=connect(self.database)
        try:
            self.assertTrue(capability(db,job)['enabled'])
            self.assertFalse(CancellationStore(self.database).status(job)['can_cancel'])
            with self.assertRaises(CancellationError):
                CancellationStore(self.database).request(job)
        finally:db.close()

        with self.assertRaises(RetryStale):
            request_review_retry(self.database,job,expected_attempt=999,expected_reason='DECISION_DOWNLOAD_CLEANUP_REVIEW')

        # Fix the underlying issue (simulate upgrading to MOVE or resolving state)
        db=connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE staging_publications SET manifest_json = json_set(manifest_json, '$.transfer', 'MOVE') WHERE job_id=?",(stage_job,))
        finally:db.close()

        receipt=request_review_retry(self.database,job,expected_attempt=1,expected_reason='DECISION_DOWNLOAD_CLEANUP_REVIEW')
        self.assertEqual(receipt.outcome,'QUEUED')
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('PENDING',)])

        # Duplicate retry is idempotent
        dup=request_review_retry(self.database,job,expected_attempt=1,expected_reason='DECISION_DOWNLOAD_CLEANUP_REVIEW')
        self.assertEqual(dup.outcome,'ALREADY_QUEUED')

        # Now run worker and prove completion
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(self.controls,['remove'])
        self.assertEqual(self.delete_files_args,[False])

    async def test_bounded_waiting_escalates_to_review(self):
        """WI-20: Unchanging torrent state escalates to NEEDS_REVIEW when phase deadline is exceeded."""
        worker,staging,stage_job=await self.staged()
        job=self.rows("SELECT job_id FROM jobs WHERE kind=? AND json_extract(payload_json,'$.staging_job_id')=?",(KIND,stage_job))[0][0]
        self.payload_root=Path(self.snapshot.observation.save_path)
        self.remainder=self.payload_root/self.snapshot.observation.files[0].path
        self.remainder.parent.mkdir(parents=True,exist_ok=True)
        self.remainder.write_bytes(b'partial')
        self.snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,state='checkingDL'))
        self.attach(worker)
        await self.make_due(job);await worker.attempt()
        # First attempt defers
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(job,)),
                         [('RETRY_WAIT','DECISION_DOWNLOAD_CLEANUP_WAITING')])
        # Age the phase in cancellation_cleanup past 10 minutes
        db=connect(self.database)
        try:
            with transaction(db):
                db.execute('UPDATE cancellation_cleanup SET created_at_ms=created_at_ms-700000, updated_at_ms=updated_at_ms-700000')
        finally:db.close()
        await self.make_due(job);await worker.attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(job,)),
                         [('NEEDS_REVIEW','DECISION_DOWNLOAD_CLEANUP_REVIEW')])
        facts=self.rows("SELECT details_json FROM job_history WHERE job_id=? AND event_code='NEEDS_REVIEW'",(job,))
        self.assertTrue(len(facts)>0)
        self.assertIn('STOP_TIMEOUT',facts[0][0])

    async def test_job_report_follows_staging_job(self):
        """WI-20: adm report job follows staging_job_id and displays originating operation and journal."""
        from src.nextgen.job_report import report
        worker,staging,stage_job=await self.staged()
        job=self.rows("SELECT job_id FROM jobs WHERE kind=? AND json_extract(payload_json,'$.staging_job_id')=?",(KIND,stage_job))[0][0]
        db=connect(self.database)
        try:
            lines=[]
            report(db,job,out=lines.append)
            text='\n'.join(lines)
            self.assertIn(f'download cleanup for staging Job #{stage_job}',text)
            self.assertIn('Staging publication',text)
            self.assertIn('Torrent submission',text)
        finally:db.close()
