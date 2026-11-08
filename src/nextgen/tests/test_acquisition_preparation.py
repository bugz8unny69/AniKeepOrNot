"""Offline inventory intent, correlation and fenced completion; unexecuted."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.nextgen.acquisition.contracts import AcquisitionProposal
from src.nextgen.acquisition.metadata import MetadataInvalid, ObservedInventory, parse_metainfo
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.job_contracts import CompletionRejected, Outcome, Policy, Registry, Successor
from src.nextgen.jobs.select_user_evaluation_job import registration as evaluate
from src.nextgen.jobs.reserve_episode_job import registration as reserve
from src.nextgen.jobs.prepare_acquisition_job import registration as prepare
from src.nextgen.storage.acquisition import load_acquisition_input, validate_declaration
from src.nextgen.storage.job_queue import LeaseLost
from src.nextgen.storage.sqlite import connect, initialize, accept_event_and_job, save_identity, enqueue_job, transaction
from src.nextgen.tests.test_acquisition_metadata import metadata
from src.nextgen.worker import Worker


class AcquisitionPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.database=self.root/'queue.sqlite'
        self.store=ConfigurationStore(self.root)
        self.startup=self.store.initialize({'storage':{'database_path':str(self.database)}}).snapshot
        self.raw,self.digest=metadata()
        self.reads=[]
        db=connect(self.database)
        initialize(db)
        db.close()

    def rows(self,sql,args=()):
        db=connect(self.database)
        try:return db.execute(sql,args).fetchall()
        finally:db.close()

    def source(self,*,torrent=True,episode=10,title='Test Anime',release_name=None):
        body={'release_name':release_name or f'Test Anime - {episode}'}
        if torrent:body['torrent_infohash']=self.digest
        raw=json.dumps(body).encode()
        db=connect(self.database)
        try:
            event,release=accept_event_and_job(db,'test',raw,kind='VERIFY_RELEASE',payload={'anidb_id':123})
            identity=save_identity(db,event,{'schema_version':2,'anime':{'anidb_id':123,'tvdb_series_id':456,'title':title}})
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE',attempt_count=1 WHERE job_id=?",(release,))
                db.execute('INSERT OR IGNORE INTO identified_episodes VALUES(?,123)',(900+episode,))
                db.execute("INSERT INTO release_verifications(job_id,identity_result_id,anidb_id,release_sha256,verification_scope,"
                    "anidb_episode_number,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms) "
                    "VALUES(?,?,123,?,'DECLARED_SINGLE_EPISODE',?,?,456,1,?,?,1)",
                    (release,identity,hashlib.sha256(raw).hexdigest(),episode,900+episode,episode,100+episode))
                evaluation=enqueue_job(db,kind='EVALUATE_TRIAL',payload={'verification_job_id':release},
                    component='worker',event_id=event,created_by_job_id=release,available_at_ms=0)
            return event,evaluation
        finally:db.close()

    def worker(self,spec=None):
        async def read(source,configuration,cancelled):
            self.reads.append(source.info_hash)
            return self.raw
        worker=Worker(self.store,self.startup,Registry((evaluate(reservation_kind='RESERVE_EPISODE'),
            reserve(preparation_kind='PREPARE_ACQUISITION'),spec or prepare(read))),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        return worker

    async def prepare_queue(self,worker):
        event,evaluation=self.source()
        await worker.attempt()
        await worker.attempt()
        return event,evaluation

    async def test_prepared_intent_has_stable_marker_and_safe_inventory_without_submission(self):
        worker=self.worker()
        await self.prepare_queue(worker)
        receipts=[]
        with patch('src.nextgen.worker.log_history',side_effect=lambda logger,record:receipts.append(record)):
            await worker.attempt()
        row=self.rows('SELECT state,info_hash,manifest_json,submission_marker FROM acquisition_plans')[0]
        self.assertEqual(row[:2],('PREPARED',self.digest))
        self.assertEqual(len(row[3]),32)
        self.assertNotIn('private-secret',row[2])
        self.assertEqual(json.loads(row[2])['files'][0]['size'],100)
        self.assertEqual(self.reads,[self.digest])
        self.assertIn(('ACQUISITION_PREPARED','ACQUISITION_PLAN_READY'),[(r.event_code,r.reason_code) for r in receipts])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE state!='DONE'"),[(0,)])

    async def test_missing_hash_is_reviewed_before_reader_is_called(self):
        self.source(torrent=False)
        worker=self.worker()
        for _ in range(3):await worker.attempt()
        self.assertEqual(self.reads,[])
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='PREPARE_ACQUISITION'"),
                         [('NEEDS_REVIEW','ACQUISITION_SOURCE_MISSING')])

    async def test_hash_mismatch_is_reviewed(self):
        worker=self.worker()
        await self.prepare_queue(worker)
        self.raw,_=metadata(name=b'Other Anime - 10.mkv')
        await worker.attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])
        self.assertEqual(self.rows("SELECT last_reason_code FROM jobs WHERE kind='PREPARE_ACQUISITION'"),
                         [('ACQUISITION_METADATA_INVALID',)])

    async def test_acquisition_does_not_recheck_filenames_after_verification(self):
        # Title, season and episode were verified before reservation; the info
        # hash binds the torrent to that release. File names are not re-checked.
        self.raw,self.digest=metadata(name=b"Overgeared - S01E10 - Family's Trust.mkv")
        event,evaluation=self.source(title='Tempal: Item no Chikara',
            release_name='[Anime Time] Overgeared - S01E10 [1080p][Dual-Audio]')
        worker=self.worker()
        for _ in range(3):await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM acquisition_plans'),[('PREPARED',)])
        source=load_acquisition_input(self.database,evaluation,event)
        manifest=parse_metainfo(self.raw,self.digest)
        validate_declaration(source,ObservedInventory(self.digest,manifest.files,0))
        for name in (b'Other Anime - 11.mkv',b'episode.mkv'):
            raw,digest=metadata(name=name)
            with self.subTest(name=name):
                validate_declaration(replace(source,info_hash=digest),parse_metainfo(raw,digest))

    async def test_concurrent_duplicate_preparation_cannot_replace_intent_or_marker(self):
        worker=self.worker()
        event,evaluation=await self.prepare_queue(worker)
        db=connect(self.database)
        try:
            with transaction(db):enqueue_job(db,kind='PREPARE_ACQUISITION',payload={'evaluation_job_id':evaluation},
                component='worker',event_id=event,available_at_ms=0)
        finally:db.close()
        await asyncio.gather(worker.attempt(),worker.attempt())
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(1,)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='PREPARE_ACQUISITION' ORDER BY state"),[('DONE',),('NEEDS_REVIEW',)])

    async def test_source_change_after_read_rejects_completion(self):
        worker=self.worker()
        event,evaluation=await self.prepare_queue(worker)
        claim,_=worker.queue.claim()
        spec=worker.registry.entries['PREPARE_ACQUISITION']
        worker.queue.start(claim,spec,'c'*32,worker.registry)
        source=load_acquisition_input(self.database,evaluation,event)
        value=AcquisitionProposal(source,parse_metainfo(self.raw,self.digest),1)
        db=connect(self.database)
        try:
            with transaction(db):db.execute('UPDATE incoming_events SET raw_body=? WHERE event_id=?',(b'changed',event))
        finally:db.close()
        with self.assertRaises(CompletionRejected):
            worker.queue.finish(claim,Outcome(value=value),spec=spec,invocation='c'*32,registry=worker.registry)
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])

    async def test_expired_lease_cannot_publish_a_plan(self):
        worker=self.worker()
        event,evaluation=await self.prepare_queue(worker)
        started=worker.queue.now()
        worker.queue.clock=lambda:started
        claim,_=worker.queue.claim()
        spec=worker.registry.entries['PREPARE_ACQUISITION']
        worker.queue.start(claim,spec,'c'*32,worker.registry)
        value=AcquisitionProposal(load_acquisition_input(self.database,evaluation,event),parse_metainfo(self.raw,self.digest),1)
        worker.queue.clock=lambda:started+30000
        with self.assertRaises(LeaseLost):
            worker.queue.finish(claim,Outcome(value=value),spec=spec,invocation='c'*32,registry=worker.registry)
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])

    async def test_later_completion_failure_rolls_back_plan_marker_and_audit(self):
        async def read(*args):return self.raw
        worker=self.worker(replace(prepare(read),route=lambda value:(Successor.create('NOT_ALLOWED',{}),)))
        await self.prepare_queue(worker)
        with self.assertRaises(ValueError):await worker.attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='ACQUISITION_PREPARED'"),[(0,)])
