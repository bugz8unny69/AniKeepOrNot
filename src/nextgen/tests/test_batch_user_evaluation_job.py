"""Offline v28 batch trial integration: no external acquisition."""
import tempfile
from pathlib import Path
import unittest

from dataclasses import replace
from src.nextgen.acquisition.metadata import BatchInventory, TorrentFile, AcquisitionConflict
from src.nextgen.storage.batch_acquisition_candidate import load_batch_acquisition_candidate
from src.nextgen.batch.provider_worker import BatchVerificationCompletion
from src.nextgen.batch.verification import verify_batch_files
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.job_contracts import Registry, Policy
from src.nextgen.jobs.verify_release_job import registration as release_registration
from src.nextgen.jobs.evaluate_batch_job import registration as batch_registration
from src.nextgen.jobs.reserve_batch_job import registration as reserve_batch_registration
from src.nextgen.providers.http import Credentials
from src.nextgen.storage.migrations import LATEST_VERSION
from src.nextgen.storage.sqlite import (connect, initialize, accept_event_and_job,
    save_identity, transaction, enqueue_job)
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.batch_submission_journal import BatchSubmissionJournal
from src.nextgen.storage.reservation_release import validate_releasable
from src.nextgen.cancellation import CancellationError
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.acquisition.submission import Observation, ClientInvalid
from src.nextgen.worker import Worker


class FakeProvider:
    def episodes(self,aid):return {i:1000+i for i in range(1,13)}
    def mapping_xml(self):
        return b'<anime-list><anime anidbid="123" tvdbid="456" defaulttvdbseason="1"/></anime-list>'
    def episode_page(self,tid,page,secret):
        return tuple((2000+i,1,i) for i in range(1,13)),False


class BatchUserEvaluationWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        self.database=root/'db.sqlite'
        self.store=ConfigurationStore(root)
        self.startup=self.store.initialize({'storage':{'database_path':str(self.database)}}).snapshot
        db=connect(self.database)
        initialize(db)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone(),(LATEST_VERSION,))
        self.event,self.verification_job=accept_event_and_job(
            db,'test',b'{"release_name":"Shiki (S01 + OVA)","torrent_infohash":"'+b'a'*40+b'"}',
            kind='VERIFY_RELEASE',payload={'anidb_id':123})
        save_identity(db,self.event,{'schema_version':2,'anime':{
            'anidb_id':123,'tvdb_series_id':456,'title':'Shiki'}})
        db.close()
        files=tuple(TorrentFile(i,f'Shiki/Season 01/Shiki S01E{i+1:02d}.mkv',
                                100000000) for i in range(12))
        self.inventory=BatchInventory('a'*40,'b'*64,files+(
            TorrentFile(12,'Shiki/OVA/Shiki OVA 01.mkv',100000000),))
        self.verified=verify_batch_files(
            self.inventory,client=FakeProvider(),credentials=Credentials('secret'),
            anidb_id=123,tvdb_series_id=456,season=1)

    def rows(self,sql):
        db=connect(self.database)
        try:return db.execute(sql).fetchall()
        finally:db.close()

    def worker(self, *, reserve=False):
        async def verify(source,*args):
            return BatchVerificationCompletion(source,self.inventory,self.verified)
        worker=Worker(self.store,self.startup,Registry((
            release_registration(verify,batch_kind='EVALUATE_BATCH'),
            batch_registration(reservation_kind='RESERVE_BATCH' if reserve else None),
            *( (reserve_batch_registration(),) if reserve else () ))),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        return worker

    async def test_batch_user_evaluation_is_immutable_and_does_not_acquire(self):
        worker=self.worker()
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT kind,state FROM jobs ORDER BY job_id'),[
            ('VERIFY_RELEASE','DONE'),('EVALUATE_BATCH','DONE')])
        self.assertEqual(self.rows('SELECT anchor_episode,backfill_count,'
                                   'following_count,planned_size_bytes FROM batch_user_evaluation_plans'),
                         [(1,6,1,600000000)])
        self.assertEqual(self.rows('SELECT tvdb_episode,file_index FROM batch_user_evaluation_files '
                                   'ORDER BY tvdb_episode'),
                         [(i,i-1) for i in range(1,7)])
        for table in ('episode_reservations','acquisition_plans','torrent_submissions',
                      'torrent_transfers','staging_publications'):
            self.assertEqual(self.rows('SELECT count(*) FROM '+table),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM batch_verified_files'),[(12,)])
        self.assertEqual(self.rows('PRAGMA foreign_key_check'),[])

    async def test_opt_in_reserves_regular_episodes_atomically_without_torrent(self):
        worker=self.worker(reserve=True)
        for _ in range(3):
            await worker.attempt()
        self.assertEqual(self.rows('SELECT kind,state FROM jobs ORDER BY job_id'),[
            ('VERIFY_RELEASE','DONE'),('EVALUATE_BATCH','DONE'),
            ('RESERVE_BATCH','DONE')])
        self.assertEqual(self.rows('SELECT tvdb_episode,file_index FROM '
                                   'batch_episode_reservations ORDER BY tvdb_episode'),
                         [(i,i-1) for i in range(1,7)])
        self.assertEqual(self.rows("SELECT state FROM batch_episode_reservations"),[
            ('ACTIVE',)]*6)
        for table in ('episode_reservations','acquisition_plans','torrent_submissions',
                      'torrent_transfers','staging_publications'):
            self.assertEqual(self.rows('SELECT count(*) FROM '+table),[(0,)])
        self.assertEqual(self.rows('PRAGMA foreign_key_check'),[])
        reserve_job=self.rows("SELECT job_id FROM jobs WHERE kind='RESERVE_BATCH'")[0][0]
        candidate=load_batch_acquisition_candidate(self.database,reserve_job,self.inventory)
        self.assertEqual(candidate.probe_index,0)
        self.assertEqual(candidate.planned_episodes,tuple((i,i-1) for i in range(1,7)))
        self.assertEqual(candidate.skipped_indices,tuple(range(6,13)))
        with self.assertRaises(AcquisitionConflict):
            load_batch_acquisition_candidate(self.database,reserve_job,
                replace(self.inventory,info_sha256='c'*64))

    async def test_stopped_submission_is_single_use_and_restart_only_reconciles(self):
        worker=self.worker(reserve=True)
        for _ in range(3):
            await worker.attempt()
        reservation=self.rows("SELECT job_id FROM jobs WHERE kind='RESERVE_BATCH'")[0][0]
        db=connect(self.database)
        try:
            with transaction(db):
                submission=enqueue_job(
                    db,kind='SUBMIT_BATCH',payload={'reservation_job_id':reservation},
                    component='worker',event_id=self.event,created_by_job_id=reservation,
                    operation_id=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',
                                            (reservation,)).fetchone()[0])
        finally:
            db.close()
        claim,_=JobQueue(self.database).claim()
        self.assertEqual(claim.job_id,submission)
        journal=BatchSubmissionJournal(self.database)
        candidate,previous=journal.load(claim,reservation,self.inventory)
        self.assertIsNone(previous)
        options=DownloadOptions('http://localhost:8080','/tmp/nextgen','trial',10)
        intent,permit,_=journal.authorize(
            claim,candidate,self.inventory,options,5,False,1)
        self.assertIsNotNone(permit)
        self.assertEqual(intent.state,'UNCERTAIN')
        self.assertEqual(journal.load(claim,reservation,self.inventory)[1],intent)
        old,again,_=journal.authorize(
            claim,candidate,self.inventory,options,5,False,1)
        self.assertEqual(old,intent)
        self.assertIsNone(again)
        journal.guard_send(claim,intent)
        permit.take(intent)
        with self.assertRaises(ClientInvalid):
            permit.take(intent)
        # A restart has only the durable intent, never another send permit.
        reopened=BatchSubmissionJournal(self.database)
        observed,after,_=reopened.authorize(
            claim,candidate,self.inventory,options,5,False,1)
        self.assertEqual(observed,intent)
        self.assertIsNone(after)
        bad=Observation(intent.inventory.info_hash,'stoppedDL',
                        intent.options.download_root,intent.options.category,
                        ('ad-'+('f'*32),),False,intent.inventory.files)
        state,reason,_=reopened.observe(claim,intent,bad)
        self.assertEqual((state,reason),('REVIEW','TORRENT_OWNERSHIP_CONFLICT'))
        self.assertEqual(self.rows(
            "SELECT state FROM batch_torrent_submissions"),[('REVIEW',)])
        self.assertEqual(self.rows("SELECT count(*) FROM torrent_submissions"),[(0,)])
        db=connect(self.database)
        try:
            operation=db.execute(
                "SELECT operation_id FROM job_operation_members WHERE job_id=?",
                (reservation,)).fetchone()[0]
            with self.assertRaises(CancellationError):
                validate_releasable(db,operation)
        finally:
            db.close()
        self.assertEqual(self.rows(
            "SELECT count(*) FROM batch_episode_reservations WHERE state='ACTIVE'"),[(6,)])

    async def test_submission_uncertain_can_confirm_exact_stopped_inventory(self):
        worker=self.worker(reserve=True)
        for _ in range(3):
            await worker.attempt()
        reservation=self.rows("SELECT job_id FROM jobs WHERE kind='RESERVE_BATCH'")[0][0]
        db=connect(self.database)
        try:
            with transaction(db):
                enqueue_job(db,kind='SUBMIT_BATCH',
                    payload={'reservation_job_id':reservation},
                    component='worker',event_id=self.event,created_by_job_id=reservation,
                    operation_id=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',
                                            (reservation,)).fetchone()[0])
        finally:
            db.close()
        claim,_=JobQueue(self.database).claim()
        journal=BatchSubmissionJournal(self.database)
        candidate,_=journal.load(claim,reservation,self.inventory)
        options=DownloadOptions('http://localhost:8080','/tmp/nextgen','trial',10)
        intent,permit,_=journal.authorize(claim,candidate,self.inventory,
                                           options,5,False,1)
        self.assertIsNotNone(permit)
        exact=Observation(intent.inventory.info_hash,'stoppedDL',
                         intent.options.download_root,intent.options.category,
                         ('ad-'+intent.marker,),False,intent.inventory.files)
        state,reason,_=journal.observe(claim,intent,exact)
        self.assertEqual((state,reason),('OWNED_STOPPED','TORRENT_STOPPED_CONFIRMED'))
        self.assertEqual(self.rows(
            "SELECT state FROM batch_torrent_submissions"),[('OWNED_STOPPED',)])
        prior,never,_=journal.authorize(claim,candidate,self.inventory,
                                        options,5,False,1)
        self.assertEqual(prior.state,'OWNED_STOPPED')
        self.assertIsNone(never)

    async def test_two_competing_packs_cannot_claim_same_episodes(self):
        db=connect(self.database)
        try:
            event,job=accept_event_and_job(
                db,'second-delivery',
                b'{"release_name":"Shiki S01 + OVA","torrent_infohash":"'+b'a'*40+b'"}',
                kind='VERIFY_RELEASE',payload={'anidb_id':123})
            save_identity(db,event,{'schema_version':2,'anime':{
                'anidb_id':123,'tvdb_series_id':456,'title':'Shiki'}})
        finally:db.close()
        worker=self.worker(reserve=True)
        for _ in range(6):
            await worker.attempt()
        self.assertEqual(self.rows(
            "SELECT state FROM jobs WHERE kind='RESERVE_BATCH' ORDER BY job_id"),
            [('DONE',),('ON_HOLD',)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM batch_episode_reservations WHERE state='ACTIVE'"),[(6,)])
        self.assertEqual(self.rows(
            "SELECT distinct owner_job_id FROM batch_episode_reservations"),[
                (self.rows("SELECT job_id FROM jobs WHERE kind='RESERVE_BATCH' "
                           "ORDER BY job_id LIMIT 1")[0][0],)])
        self.assertEqual(self.rows('PRAGMA foreign_key_check'),[])

    async def test_missing_episode_one_requires_review_without_reservation(self):
        # An otherwise provider-confirmed pack cannot guess the trial anchor.
        # Reindex a pack whose first regular episode is E02.
        self.inventory=BatchInventory('a'*40,'b'*64,tuple(
            TorrentFile(i,raw.path,raw.size) for i,raw in enumerate(self.inventory.files[1:])))
        self.verified=verify_batch_files(
            self.inventory,client=FakeProvider(),credentials=Credentials('secret'),
            anidb_id=123,tvdb_series_id=456,season=1)
        worker=self.worker()
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT kind,state,last_reason_code FROM jobs ORDER BY job_id'),[
            ('VERIFY_RELEASE','DONE',None),
            ('EVALUATE_BATCH','NEEDS_REVIEW','USER_EVALUATION_PLAN_CONFLICT')])
        self.assertEqual(self.rows('SELECT count(*) FROM batch_user_evaluation_plans'),[(0,)])
