"""Offline durable selection/start recovery and worker handoff; unexecuted."""
import asyncio
from dataclasses import replace
import unittest

from src.nextgen.acquisition.submission import Observation, ClientUnavailable
from src.nextgen.acquisition.transfers import TransferSnapshot, verify_snapshot, exact_selection
from src.nextgen.job_contracts import Policy, Registry
from src.nextgen.jobs.select_user_evaluation_job import registration as evaluate
from src.nextgen.jobs.reserve_episode_job import registration as reserve
from src.nextgen.jobs.prepare_acquisition_job import registration as prepare
from src.nextgen.jobs.submit_torrent_job import registration as submit
from src.nextgen.jobs.download_torrent_job import registration as download
from src.nextgen.storage.job_queue import LeaseLost
from src.nextgen.storage.sqlite import connect, transaction, enqueue_job
from src.nextgen.storage.transfers import TransferJournal
from src.nextgen.tests import test_acquisition_preparation as fixture
from src.nextgen.tests.test_acquisition_metadata import metadata
from src.nextgen.tests.test_torrent_submission import Secrets
from src.nextgen.worker import Worker


class TorrentTransferTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.AcquisitionPreparationTests.asyncSetUp
    rows=fixture.AcquisitionPreparationTests.rows
    source=fixture.AcquisitionPreparationTests.source

    def worker(self,*,mode='normal',journal_factory=TransferJournal,verification=None,additional=()):
        test=self
        self.adds=self.starts=0
        self.selection_calls=[];self.targets=[];self.snapshot=None
        class FakeClient:
            major=5
            def __init__(self,options,credentials):test.targets.append(options)
            def login(self):pass
            def inspect(self,expected):return None if test.snapshot is None else test.snapshot.observation
            def add_stopped(self,raw,intent,permit):
                permit.take(intent);test.adds+=1
                obs=Observation(intent.plan.manifest.info_hash,'stoppedDL',intent.options.download_root,
                                intent.options.category,('ad-'+intent.plan.marker,),False,intent.plan.manifest.files)
                offset=0;ranges=[]
                for f in obs.files:
                    ranges.append((offset//262144,(offset+f.size-1)//262144));offset+=f.size
                test.snapshot=TransferSnapshot(obs,(1,)*len(obs.files),(0,)*len(obs.files),262144,tuple(ranges))
            def inspect_transfer(self,expected):return test.snapshot
            def apply_selection(self,intent,fingerprint,permit,action):
                verify_snapshot(intent,test.snapshot)
                permit.take(fingerprint,action)
                test.selection_calls.append(action)
                if mode=='selection_timeout':raise ClientUnavailable()
                selected=intent.plan.manifest.selected_index
                priorities=tuple((1 if i==selected else p) if action=='wanted' else (p if i==selected else 0)
                                 for i,p in enumerate(test.snapshot.priorities))
                test.snapshot=replace(test.snapshot,priorities=priorities)
            def start_selected(self,intent,fingerprint,permit):
                verify_snapshot(intent,test.snapshot)
                if not exact_selection(intent,test.snapshot):raise ValueError('wrong selection')
                permit.take(fingerprint,'start');test.starts+=1
                if mode!='start_timeout_absent':
                    test.snapshot=replace(test.snapshot,observation=replace(test.snapshot.observation,state='downloading'))
                if mode.startswith('start_timeout'):raise ClientUnavailable()
        async def read(*args):return test.raw
        worker=Worker(self.store,self.startup,Registry((evaluate(reservation_kind='RESERVE_EPISODE'),
            reserve(preparation_kind='PREPARE_ACQUISITION'),prepare(read,submission_kind='SUBMIT_TORRENT'),
            submit(read,Secrets(),client_factory=FakeClient,transfer_kind='DOWNLOAD_TORRENT'),
            download(Secrets(),client_factory=FakeClient,journal_factory=journal_factory,
                verification_kind=None if verification is None else verification.kind),
            *((verification,) if verification is not None else ()),*additional)),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        self.store.update({'qbittorrent':{'base_url':'https://qb.example'},'storage':{'download_root':'/srv/downloads'}},expected_revision=1)
        return worker

    async def queued(self,worker):
        event,_=self.source()
        for _ in range(4):await worker.attempt()
        prep=self.rows("SELECT job_id FROM jobs WHERE kind='PREPARE_ACQUISITION'")[0][0]
        return event,prep

    def due(self):
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET available_at_ms=0 WHERE kind='DOWNLOAD_TORRENT' AND state='RETRY_WAIT'")
        finally:db.close()

    def enqueue(self,event,prep,kind='DOWNLOAD_TORRENT'):
        db=connect(self.database)
        try:
            with transaction(db):return enqueue_job(db,kind=kind,payload={'preparation_job_id':prep},
                component='worker',event_id=event,available_at_ms=0)
        finally:db.close()

    async def test_selected_progress_finishes_without_sidecar_completion_and_pins_options(self):
        self.raw,self.digest=metadata(name=b'Folder',files=[{b'length':262144,b'path':[b'info.nfo']},
            {b'length':100,b'path':[b'Test Anime - 10.mkv']}],extra={b'pieces':b'p'*40})
        worker=self.worker()
        await self.queued(worker)
        self.store.update({'qbittorrent':{'base_url':'https://changed.example'}},expected_revision=2)
        await worker.attempt()
        self.assertEqual(self.selection_calls,['skip'])
        self.assertEqual((self.adds,self.starts),(1,1))
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('ACTIVE',)])
        self.assertEqual(self.rows("SELECT state,activity_code FROM jobs WHERE kind='DOWNLOAD_TORRENT'"),[('RETRY_WAIT','DOWNLOADING')])
        self.snapshot=replace(self.snapshot,progress=(0,1))
        self.due();await worker.attempt()
        self.assertEqual(self.rows('SELECT phase,selected_progress FROM torrent_transfers'),[('COMPLETE',1.0)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='DOWNLOAD_TORRENT'"),[('DONE',)])
        self.assertTrue(all(t.base_url=='https://qb.example' for t in self.targets))
        self.assertEqual(self.starts,1)

    async def test_shared_boundary_bytes_permitted_under_w19(self):
        self.raw,self.digest=metadata(name=b'Folder',files=[{b'length':10,b'path':[b'info.nfo']},
                                                          {b'length':100,b'path':[b'Test Anime - 10.mkv']}])
        worker=self.worker();await self.queued(worker);await worker.attempt()
        self.assertEqual(self.starts,1)
        self.assertEqual(self.rows("SELECT state,activity_code FROM jobs WHERE kind='DOWNLOAD_TORRENT'"),
                         [('RETRY_WAIT','DOWNLOADING')])

    async def test_start_timeout_never_rearms_even_when_torrent_stays_stopped(self):
        worker=self.worker(mode='start_timeout_absent');await self.queued(worker);await worker.attempt()
        for _ in range(2):self.due();await worker.attempt()
        self.assertEqual(self.starts,1)
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('START_UNCERTAIN',)])

    async def test_start_timeout_after_acceptance_reconciles_without_another_start(self):
        worker=self.worker(mode='start_timeout_present');await self.queued(worker);await worker.attempt()
        self.due();await worker.attempt()
        self.assertEqual(self.starts,1)
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('ACTIVE',)])

    async def test_selection_timeout_does_not_replay_or_start(self):
        self.raw,self.digest=metadata(name=b'Folder',files=[{b'length':262144,b'path':[b'info.nfo']},
            {b'length':100,b'path':[b'Test Anime - 10.mkv']}],extra={b'pieces':b'p'*40})
        worker=self.worker(mode='selection_timeout');await self.queued(worker);await worker.attempt()
        self.due();await worker.attempt()
        self.assertEqual(self.selection_calls,['skip'])
        self.assertEqual(self.starts,0)
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('SELECT_UNCERTAIN',)])

    async def test_lost_start_permit_is_not_replaced(self):
        class LoseStart(TransferJournal):
            def start(self,*args):
                result=super().start(*args)
                if result[1] is not None:raise RuntimeError('simulated crash after commit')
                return result
        worker=self.worker(journal_factory=LoseStart);await self.queued(worker);await worker.attempt()
        self.due();await worker.attempt()
        self.assertEqual(self.starts,0)
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('START_UNCERTAIN',)])

    async def test_lost_selection_permit_only_confirms_existing_selection(self):
        class LoseSelection(TransferJournal):
            def select(self,*args):
                result=super().select(*args)
                if result[1] is not None:raise RuntimeError('simulated crash after commit')
                return result
        worker=self.worker(journal_factory=LoseSelection);await self.queued(worker);await worker.attempt()
        self.due();await worker.attempt()
        self.assertEqual(self.selection_calls,[])
        self.assertEqual(self.starts,1)  # Single-file source was already exactly selected.

    async def test_competing_jobs_and_duplicate_submission_preserve_active_receipt(self):
        worker=self.worker();event,prep=await self.queued(worker)
        self.enqueue(event,prep)
        await asyncio.gather(worker.attempt(),worker.attempt())
        self.enqueue(event,prep,'SUBMIT_TORRENT')
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('OWNED_STOPPED',)])
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('ACTIVE',)])
        self.assertEqual((self.adds,self.starts),(1,1))
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='SUBMIT_TORRENT'"),[('DONE',),('DONE',)])

    async def test_stale_stopped_snapshot_cannot_replace_a_newer_active_record(self):
        worker=self.worker();_,prep=await self.queued(worker);await worker.attempt()
        self.due();claim,_=worker.queue.claim();journal=TransferJournal(self.database)
        intent,current=journal.load(claim,prep)
        self.assertEqual(current.phase,'ACTIVE')
        stale=replace(current,phase='START_UNCERTAIN',reason_code='TORRENT_START_UNCERTAIN')
        old_snapshot=replace(self.snapshot,observation=replace(self.snapshot.observation,state='stoppedDL'))
        result,receipts=journal.observe(claim,intent,old_snapshot,expected_record=stale)
        self.assertEqual(result,current);self.assertEqual(receipts,())
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('ACTIVE',)])

    async def test_changed_priorities_stop_monitoring_for_review_without_restarting(self):
        worker=self.worker();await self.queued(worker);await worker.attempt()
        self.snapshot=replace(self.snapshot,priorities=(0,))
        self.due();await worker.attempt()
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('REVIEW',)])
        self.assertEqual(self.starts,1)

    async def test_absent_owned_torrent_is_waited_for_without_readding(self):
        worker=self.worker();await self.queued(worker);self.snapshot=None
        await worker.attempt()
        self.assertEqual((self.adds,self.starts),(1,0))
        self.assertEqual(self.rows("SELECT last_reason_code FROM jobs WHERE kind='DOWNLOAD_TORRENT'"),[('TORRENT_DOWNLOAD_WAITING',)])

    async def test_stale_lease_cannot_select_guard_or_confirm(self):
        worker=self.worker();_,prep=await self.queued(worker)
        claim,_=worker.queue.claim()
        journal=TransferJournal(self.database)
        intent,_=journal.load(claim,prep)
        expired=TransferJournal(self.database,clock=lambda:worker.queue.now()+30000)
        with self.assertRaises(LeaseLost):expired.select(claim,intent,self.snapshot)
        record,permit,_=journal.select(claim,intent,self.snapshot)
        with self.assertRaises(LeaseLost):expired.guard(claim,intent,record,'wanted')
        with self.assertRaises(LeaseLost):expired.selected(claim,intent,self.snapshot)
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'),[('SELECT_UNCERTAIN',)])
