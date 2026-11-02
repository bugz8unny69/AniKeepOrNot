"""Offline trial windows, concurrency and fenced persistence; unexecuted."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationError, CONVERSIONS, convert_document, ConfigurationSnapshot
from src.nextgen.job_contracts import Outcome, Policy, Registry, Successor
from src.nextgen.jobs.evaluate_trial_job import registration
from src.nextgen.storage.job_queue import LeaseLost
from src.nextgen.storage.sqlite import connect, initialize, accept_event_and_job, save_identity, enqueue_job, transaction
from src.nextgen.storage.trials import load_trial_source
from src.nextgen.trial_policy import Window, TrialProposal, InvalidEpisodeCounts
from src.nextgen.worker import Worker


class TrialWindowTests(unittest.TestCase):
    def test_following_includes_trigger_and_overlaps_count_once(self):
        self.assertEqual(Window(6,1).episodes(10),(1,2,3,4,5,6,10))
        self.assertEqual(Window(0,1).episodes(10),(10,))
        self.assertEqual(Window(0,2).episodes(10),(10,11))
        self.assertEqual(Window(0,6).episodes(10),(10,11,12,13,14,15))
        self.assertEqual(Window(6,2).episodes(3),(1,2,3,4,5,6))
        self.assertEqual(Window(6,3).episodes(5),(1,2,3,4,5,6,7))
        self.assertEqual(Window(6,0).episodes(10),(1,2,3,4,5,6))
        with self.assertRaises(InvalidEpisodeCounts):Window(0,0)
        for value in (-1,True,1.5,1001):
            with self.assertRaises(ValueError):Window(value,1)
        self.assertEqual(len(Window(100,100).episodes(200)),200)

    def test_zero_zero_rejected_for_snapshot_and_conversion(self):
        settings={'trial':{'backfill_count':0,'following_count':0}}
        for make in (lambda:ConfigurationSnapshot(1,settings),
                     lambda:convert_document({'schema_version':5,'revision':1,'settings':settings})):
            with self.assertRaises(ConfigurationError) as error:make()
            self.assertEqual(error.exception.reason_code,'TRIAL_COUNTS_INVALID')
            self.assertIn('at least 1',str(error.exception))
        for backfill,following in ((6,0),(0,1)):
            snapshot=ConfigurationSnapshot(1,{'trial':{'backfill_count':backfill,'following_count':following}})
            self.assertEqual(snapshot.value('trial.following_count'),following)

    def test_v4_upgrade_adds_defaults_without_changing_historical_shapes(self):
        document={}
        for version in range(4):
            document=CONVERSIONS[version](document)
            self.assertNotIn('trial',document['settings'])
        current=convert_document(document)
        self.assertEqual((current.schema_version,current.revision),(13,13))
        self.assertEqual(current.value('trial.backfill_count'),6)
        self.assertEqual(current.value('trial.following_count'),1)
        with self.assertRaises(ConfigurationError):
            convert_document({'schema_version':4,'revision':1,'settings':{'trial':{'following_count':2}}})
        for value in (-1,True,1001):
            with self.assertRaises(ConfigurationError):ConfigurationSnapshot(1,{'trial':{'backfill_count':value}})


class TrialEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.database=self.root/'queue.sqlite'
        self.store=ConfigurationStore(self.root)
        self.startup=self.store.initialize({'storage':{'database_path':str(self.database)}}).snapshot
        db=connect(self.database)
        initialize(db)
        db.close()

    def rows(self,sql,args=()):
        db=connect(self.database)
        try:return db.execute(sql,args).fetchall()
        finally:db.close()

    def source(self,episode,season=1):
        db=connect(self.database)
        try:
            event,release_job=accept_event_and_job(db,'test',b'declared release',kind='VERIFY_RELEASE',payload={'anidb_id':123})
            identity=save_identity(db,event,{'schema_version':2,'anime':{'anidb_id':123,'tvdb_series_id':456,'title':'Test'}})
            eid=10000+season*1000+episode
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE',attempt_count=1 WHERE job_id=?",(release_job,))
                db.execute('INSERT OR IGNORE INTO identified_episodes VALUES(?,123)',(eid,))
                db.execute("INSERT INTO release_verifications(job_id,identity_result_id,anidb_id,release_sha256,verification_scope,"
                           "anidb_episode_number,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms) "
                           "VALUES(?,?,123,?,'DECLARED_SINGLE_EPISODE',?,?,456,?,?,?,1)",
                           (release_job,identity,'a'*64,episode,eid,season,episode,eid+100000))
                job=enqueue_job(db,kind='EVALUATE_TRIAL',payload={'verification_job_id':release_job},
                                component='worker',event_id=event,created_by_job_id=release_job,available_at_ms=0)
            return event,release_job,job
        finally:db.close()

    def worker(self,spec=None):
        worker=Worker(self.store,self.startup,Registry((spec or registration(),)),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        return worker

    async def test_received_episode_10_keeps_first_six_and_trigger_without_sliding(self):
        self.source(10)
        worker=self.worker()
        audits=[]
        with patch('src.nextgen.worker.log_history',side_effect=lambda logger,record:audits.append(record)):
            await worker.attempt()
        self.source(11)
        await worker.attempt()
        self.source(1)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT anchor_episode,backfill_count,following_count FROM trial_plans'),[(10,6,1)])
        self.assertEqual(self.rows('SELECT selected,reason_code FROM trial_evaluations ORDER BY evaluation_id'),
                         [(1,'TRIAL_SELECTED'),(0,'TRIAL_OUTSIDE_WINDOW'),(1,'TRIAL_SELECTED')])
        self.assertIn(('TRIAL_EVALUATED','TRIAL_SELECTED'),[(row.event_code,row.reason_code) for row in audits])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind NOT IN ('VERIFY_RELEASE','EVALUATE_TRIAL')"),[(0,)])

    async def test_live_changes_apply_only_to_new_plans_and_each_season_is_separate(self):
        self.store.update({'trial':{'backfill_count':0,'following_count':2}},expected_revision=1)
        worker=self.worker()
        self.source(10)
        await worker.attempt()
        self.store.update({'trial':{'following_count':100}},expected_revision=2)
        self.source(11)
        await worker.attempt()
        self.source(12)
        await worker.attempt()
        self.source(20,season=2)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT tvdb_season,anchor_episode,following_count,configuration_revision FROM trial_plans ORDER BY plan_id'),
                         [(1,10,2,2),(2,20,100,3)])
        self.assertEqual(self.rows('SELECT selected FROM trial_evaluations ORDER BY evaluation_id'),[(1,),(1,),(0,),(1,)])

    async def test_zero_counts_rejected_before_saving_or_creating_work(self):
        before=self.store.read()
        with self.assertRaises(ConfigurationError) as error:
            self.store.update({'trial':{'backfill_count':0,'following_count':0}},expected_revision=1)
        self.assertEqual(error.exception.reason_code,'TRIAL_COUNTS_INVALID')
        self.assertEqual(self.store.read(),before)
        self.assertEqual(self.rows('SELECT count(*) FROM trial_plans'),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM trial_evaluations'),[(0,)])

    async def test_concurrent_evaluations_create_one_plan_and_one_anchor(self):
        self.store.update({'trial':{'backfill_count':0,'following_count':1}},expected_revision=1)
        self.source(10)
        self.source(20)
        worker=self.worker()
        await asyncio.gather(worker.attempt(),worker.attempt())
        plans=self.rows('SELECT plan_id,anchor_episode FROM trial_plans')
        self.assertEqual(len(plans),1)
        self.assertIn(plans[0][1],(10,20))
        self.assertEqual(self.rows('SELECT count(*),sum(selected),count(DISTINCT plan_id) FROM trial_evaluations'),[(2,1,1)])

    async def test_failed_completion_rolls_back_plan_decision_and_trial_marker(self):
        self.source(10)
        spec=replace(registration(),route=lambda value:(Successor.create('NOT_ALLOWED',{}),))
        worker=self.worker(spec)
        with self.assertRaises(ValueError):await worker.attempt()
        for table in ('trial_plans','trial_evaluations'):
            self.assertEqual(self.rows('SELECT count(*) FROM '+table),[(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='TRIAL_EVALUATED'"),[(0,)])

    async def test_stale_lease_cannot_create_a_trial_plan(self):
        event,release_job,_=self.source(10)
        worker=self.worker()
        worker.queue.clock=lambda:1000
        claim,_=worker.queue.claim()
        spec=worker.registry.entries['EVALUATE_TRIAL']
        worker.queue.start(claim,spec,'a'*32,worker.registry)
        value=TrialProposal(load_trial_source(self.database,release_job,event),Window(),1)
        worker.queue.clock=lambda:31000
        with self.assertRaises(LeaseLost):
            worker.queue.finish(claim,Outcome(value=value),spec=spec,invocation='a'*32,registry=worker.registry)
        self.assertEqual(self.rows('SELECT count(*) FROM trial_plans'),[(0,)])
