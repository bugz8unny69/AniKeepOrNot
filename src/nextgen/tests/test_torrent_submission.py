"""Offline durable send authorization/reconciliation and lease cases; unexecuted."""
import asyncio
from dataclasses import replace
import unittest

from src.nextgen.acquisition.settings import DownloadCredentials
from src.nextgen.acquisition.submission import ClientUnavailable, Observation
from src.nextgen.job_contracts import Policy, Registry
from src.nextgen.jobs.select_user_evaluation_job import registration as evaluate
from src.nextgen.jobs.reserve_episode_job import registration as reserve
from src.nextgen.jobs.prepare_acquisition_job import registration as prepare
from src.nextgen.jobs.submit_torrent_job import registration as submit
from src.nextgen.jobs.discover_episode_releases_job import registration as discover
from src.nextgen.storage.job_queue import LeaseLost
from src.nextgen.storage.sqlite import connect, transaction, enqueue_job
from src.nextgen.storage.submissions import SubmissionJournal, target_options
from src.nextgen.acquisition.settings import download_options
from src.nextgen.tests import test_acquisition_preparation as fixture
from src.nextgen.worker import Worker


class Secrets:
    def qbittorrent_credentials(self):return DownloadCredentials('private-user','private-pass')


class TorrentSubmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.AcquisitionPreparationTests.asyncSetUp
    rows=fixture.AcquisitionPreparationTests.rows
    source=fixture.AcquisitionPreparationTests.source

    def worker(self,*,mode='normal',journal_factory=SubmissionJournal,backfill=False,prowlarr='https://prowlarr.example'):
        test=self
        self.adds=0
        self.metainfo_reads=0
        self.observation=None
        self.targets=[]
        class FakeClient:
            major=5
            def __init__(self,options,credentials):
                self.options=options
                test.targets.append(options)
            def login(self):pass
            def inspect(self,expected):return test.observation
            def add_stopped(self,raw,intent,permit):
                permit.take(intent)
                test.adds+=1
                if mode!='timeout_absent':
                    test.observation=Observation(intent.plan.manifest.info_hash,'stoppedDL',intent.options.download_root,
                        intent.options.category,('ad-'+intent.plan.marker,),False,intent.plan.manifest.files)
                if mode.startswith('timeout'):raise ClientUnavailable()
        async def read(*args):
            test.metainfo_reads+=1
            return self.raw
        worker=Worker(self.store,self.startup,Registry((evaluate(reservation_kind='RESERVE_EPISODE'),
            reserve(preparation_kind='PREPARE_ACQUISITION'),prepare(read,submission_kind='SUBMIT_TORRENT'),
            submit(read,Secrets(),client_factory=FakeClient,journal_factory=journal_factory,
                   discovery_kind='DISCOVER_EPISODE_RELEASES' if backfill else None),
            *((discover(None,catalog_factory=lambda configuration:None),) if backfill else ()))),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        self.store.update({'qbittorrent':{'base_url':'https://qb.example'},'storage':{'download_root':'/srv/downloads'},
                           'prowlarr':{'base_url':prowlarr}},expected_revision=1)
        return worker

    async def queued(self,worker):
        event,evaluation=self.source()
        for _ in range(3):await worker.attempt()
        prep=self.rows("SELECT job_id FROM jobs WHERE kind='PREPARE_ACQUISITION'")[0][0]
        return event,prep

    def due(self):
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET available_at_ms=0 WHERE kind='SUBMIT_TORRENT' AND state='RETRY_WAIT'")
        finally:db.close()

    async def test_success_is_owned_stopped_without_starting_downloads(self):
        worker=self.worker()
        await self.queued(worker)
        await worker.attempt()
        self.assertEqual(self.adds,1)
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('OWNED_STOPPED',)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='SUBMIT_TORRENT'"),[('DONE',)])
        self.assertTrue(self.targets[0].download_root.startswith('/srv/downloads/ad-'))
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='TORRENT_SUBMISSION_RECORDED'"),[(1,)])

    async def test_backfill_starts_only_after_confirmed_submission(self):
        worker=self.worker(backfill=True)
        await self.queued(worker)
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='DISCOVER_EPISODE_RELEASES'"),[(0,)])
        await worker.attempt()
        self.assertEqual(self.adds,1)
        self.assertEqual(self.rows("SELECT d.state,p.kind,p.state FROM jobs d JOIN jobs p "
                                   "ON p.job_id=d.created_by_job_id WHERE d.kind='DISCOVER_EPISODE_RELEASES'"),
                         [('PENDING','SUBMIT_TORRENT','DONE')])
        # Backfill runs in its own operation, so the episode can be marked STAGED
        # while backfill is still searching (and backfill is not frozen by it).
        self.assertEqual(self.rows("SELECT count(DISTINCT m.operation_id) FROM jobs j JOIN job_operation_members m USING(job_id) "
                                   "WHERE j.kind IN ('DISCOVER_EPISODE_RELEASES','SUBMIT_TORRENT')"),[(2,)])

    async def test_no_prowlarr_address_means_automatic_search_is_off(self):
        # A blank Prowlarr address turns automatic search off: the torrent is
        # still submitted and downloaded, but no backfill search is scheduled.
        worker=self.worker(backfill=True,prowlarr='')
        await self.queued(worker)
        await worker.attempt()
        self.assertEqual(self.adds,1)
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='SUBMIT_TORRENT'"),[('DONE',)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='DISCOVER_EPISODE_RELEASES'"),[(0,)])

    async def test_review_submission_never_starts_backfill(self):
        worker=self.worker(backfill=True)
        await self.queued(worker)
        self.observation=Observation(self.digest,'downloading','/other','other',(),False,())
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='DISCOVER_EPISODE_RELEASES'"),[(0,)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='SUBMIT_TORRENT'"),[('NEEDS_REVIEW',)])

    async def test_uncertain_submission_never_starts_backfill(self):
        worker=self.worker(mode='timeout_absent',backfill=True)
        await self.queued(worker)
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='DISCOVER_EPISODE_RELEASES'"),[(0,)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='SUBMIT_TORRENT'"),[('RETRY_WAIT',)])

    async def test_add_timeout_then_reconcile_never_repeats_add_and_pins_target(self):
        worker=self.worker(mode='timeout_present')
        await self.queued(worker)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('UNCERTAIN',)])
        reads=self.metainfo_reads
        self.store.update({'qbittorrent':{'base_url':'https://different.example'},'storage':{'download_root':'/else/downloads'}},expected_revision=2)
        self.due()
        await worker.attempt()
        self.assertEqual(self.adds,1)
        self.assertEqual(self.metainfo_reads,reads)
        self.assertEqual(self.targets[0],self.targets[-1])
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('OWNED_STOPPED',)])

    async def test_absence_after_uncertain_submission_never_rearms_send(self):
        worker=self.worker(mode='timeout_absent')
        await self.queued(worker)
        await worker.attempt()
        for _ in range(2):
            self.due()
            await worker.attempt()
        self.assertEqual(self.adds,1)
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('UNCERTAIN',)])
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='SUBMIT_TORRENT'"),
                         [('RETRY_WAIT','TORRENT_SUBMISSION_UNCERTAIN')])

    async def test_crash_after_journal_commit_before_send_cannot_regain_permit(self):
        class LosePermit(SubmissionJournal):
            def authorize(self,*args):
                result=super().authorize(*args)
                if result[1] is not None:raise RuntimeError('simulated crash')
                return result
        worker=self.worker(journal_factory=LosePermit)
        await self.queued(worker)
        await worker.attempt()
        self.due()
        await worker.attempt()
        self.assertEqual(self.adds,0)
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('UNCERTAIN',)])

    async def test_preexisting_torrent_is_external_and_untouched(self):
        worker=self.worker()
        await self.queued(worker)
        self.observation=Observation(self.digest,'downloading','/someone/else','other',(),False,())
        await worker.attempt()
        self.assertEqual(self.adds,0)
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('EXTERNAL',)])
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='SUBMIT_TORRENT'"),
                         [('NEEDS_REVIEW','TORRENT_ALREADY_PRESENT')])

    async def test_changed_provenance_or_active_state_requires_review_without_mutation(self):
        worker=self.worker(mode='timeout_present')
        await self.queued(worker)
        await worker.attempt()
        self.observation=replace(self.observation,state='downloading')
        self.due()
        await worker.attempt()
        self.assertEqual(self.adds,1)
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('REVIEW',)])

    async def test_stale_claim_cannot_authorize_or_confirm_submission(self):
        worker=self.worker()
        _,prep=await self.queued(worker)
        claim,_=worker.queue.claim()
        journal=SubmissionJournal(self.database)
        plan,_=journal.load(claim,prep)
        options=target_options(download_options(self.store.read()),plan)
        expired=SubmissionJournal(self.database,clock=lambda:worker.queue.now()+30000)
        with self.assertRaises(LeaseLost):expired.authorize(claim,plan,options,5,False,2)
        self.assertEqual(self.rows('SELECT count(*) FROM torrent_submissions'),[(0,)])
        intent,permit,_=journal.authorize(claim,plan,options,5,False,2)
        with self.assertRaises(LeaseLost):expired.observe(claim,intent,None)
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('UNCERTAIN',)])

    async def test_duplicate_concurrent_jobs_receive_only_one_send_authorization(self):
        worker=self.worker()
        event,prep=await self.queued(worker)
        db=connect(self.database)
        try:
            with transaction(db):enqueue_job(db,kind='SUBMIT_TORRENT',payload={'preparation_job_id':prep},
                component='worker',event_id=event,available_at_ms=0)
        finally:db.close()
        await asyncio.gather(worker.attempt(),worker.attempt())
        for _ in range(2):
            self.due()
            await worker.attempt()
        self.assertEqual(self.adds,1)
        self.assertEqual(self.rows('SELECT count(*) FROM torrent_submissions'),[(1,)])
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('OWNED_STOPPED',)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='SUBMIT_TORRENT'"),[('DONE',),('DONE',)])

    async def test_stale_absence_cannot_replace_another_jobs_confirmed_ownership(self):
        worker=self.worker();_,prep=await self.queued(worker)
        claim,_=worker.queue.claim();journal=SubmissionJournal(self.database)
        plan,_=journal.load(claim,prep)
        options=target_options(download_options(self.store.read()),plan)
        original,_,_=journal.authorize(claim,plan,options,5,False,2)
        found=Observation(plan.manifest.info_hash,'stoppedDL',options.download_root,
            options.category,('ad-'+plan.marker,),False,plan.manifest.files)
        self.assertEqual(journal.observe(claim,original,found)[0],'OWNED_STOPPED')
        self.assertEqual(journal.observe(claim,original,None)[0],'OWNED_STOPPED')
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('OWNED_STOPPED',)])

    async def test_changed_evidence_before_authorization_cannot_create_an_intent(self):
        from src.nextgen.acquisition.metadata import AcquisitionConflict
        worker=self.worker()
        event,prep=await self.queued(worker)
        claim,_=worker.queue.claim()
        journal=SubmissionJournal(self.database)
        plan,_=journal.load(claim,prep)
        options=target_options(download_options(self.store.read()),plan)
        db=connect(self.database)
        try:
            with transaction(db):db.execute('UPDATE incoming_events SET raw_body=? WHERE event_id=?',(b'changed',event))
        finally:db.close()
        with self.assertRaises(AcquisitionConflict):journal.authorize(claim,plan,options,5,False,2)
        self.assertEqual(self.rows('SELECT count(*) FROM torrent_submissions'),[(0,)])
