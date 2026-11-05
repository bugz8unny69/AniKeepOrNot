"""Offline atomic size-budget admission and frozen settings; unexecuted."""
import asyncio
import unittest
from dataclasses import replace
from src.nextgen.job_contracts import Policy, Registry, Successor
from src.nextgen.jobs.evaluate_trial_job import registration as evaluate
from src.nextgen.jobs.reserve_episode_job import registration as reserve
from src.nextgen.jobs.prepare_acquisition_job import registration as prepare
from src.nextgen.storage.sqlite import connect, transaction
from src.nextgen.tests import test_acquisition_preparation as fixture
from src.nextgen.tests.test_acquisition_metadata import metadata
from src.nextgen.worker import Worker


class AcquisitionBudgetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.AcquisitionPreparationTests.asyncSetUp
    rows=fixture.AcquisitionPreparationTests.rows
    source=fixture.AcquisitionPreparationTests.source

    def worker(self,*,fail_after_insert=False):
        self.sources={}
        async def read(source,*args):return self.sources[source.info_hash]
        spec=prepare(read)
        if fail_after_insert:spec=replace(spec,route=lambda value:(Successor.create('NOT_ALLOWED',{}),))
        worker=Worker(self.store,self.startup,Registry((evaluate(reservation_kind='RESERVE_EPISODE'),
            reserve(preparation_kind='PREPARE_ACQUISITION'),spec)),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        return worker

    def release(self,episode,size):
        self.raw,self.digest=metadata(name=f'Test Anime - {episode}.mkv'.encode(),
            extra={b'length':size,b'pieces':b'p'*(20*((size+262143)//262144))})
        self.sources[self.digest]=self.raw
        return self.source(episode=episode)

    async def test_two_concurrent_episodes_cannot_each_spend_same_remaining_budget(self):
        self.store.update({'trial':{'backfill_count':0,'following_count':2},
                           'acquisition':{'max_episode_mb':1,'max_plan_mb':1}},expected_revision=1)
        worker=self.worker()
        self.release(10,700000)
        self.release(11,700000)
        for _ in range(4):await worker.attempt()
        await asyncio.gather(worker.attempt(),worker.attempt())
        self.assertEqual(self.rows('SELECT count(*),sum(selected_size_bytes) FROM acquisition_plans'),[(1,700000)])
        # The over-budget copy is canceled automatically so another release can take the episode.
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='PREPARE_ACQUISITION' ORDER BY state"),
                         [('CANCELED','ACQUISITION_PLAN_SIZE_LIMIT'),('DONE',None)])
        # Two 0.7 MB videos against a 1 MB trial total: the trial-total limit, with its numbers.
        (details,),=self.rows("SELECT details_json FROM job_history WHERE reason_code='ACQUISITION_PLAN_SIZE_LIMIT'")
        import json
        self.assertEqual({k:v for k,v in json.loads(details).items() if k!='attempt'},
                         {'video_mb':1,'plan_used_mb':1,'limit_mb':1})
        from src.nextgen.api.app import size_detail
        self.assertEqual(size_detail(json.loads(details)),
            "The video is 1 MB; this anime's trial episodes already use 1 MB of their 1 MB total limit.")
        self.assertEqual(size_detail({'video_mb':800,'limit_mb':600}),
            'The video is 800 MB; the episode limit is 600 MB.')
        self.assertEqual(self.rows('SELECT max_episode_bytes,max_plan_bytes FROM acquisition_budgets'),[(1000000,1000000)])

    async def test_existing_budget_retains_captured_limits_after_live_settings_change(self):
        self.store.update({'trial':{'backfill_count':0,'following_count':2},
                           'acquisition':{'max_episode_mb':1,'max_plan_mb':2}},expected_revision=1)
        worker=self.worker()
        self.release(10,600000)
        for _ in range(3):await worker.attempt()
        self.store.update({'acquisition':{'max_plan_mb':1}},expected_revision=2)
        self.release(11,600000)
        for _ in range(3):await worker.attempt()
        self.assertEqual(self.rows('SELECT sum(selected_size_bytes) FROM acquisition_plans'),[(1200000,)])
        self.assertEqual(self.rows('SELECT max_plan_bytes,configuration_revision FROM acquisition_budgets'),[(2000000,2)])

    async def test_per_episode_limit_rejects_without_creating_a_budget(self):
        self.store.update({'trial':{'backfill_count':0,'following_count':2},
                           'acquisition':{'max_episode_mb':1,'max_plan_mb':2}},expected_revision=1)
        worker=self.worker()
        self.release(10,1000001)
        for _ in range(3):await worker.attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_budgets'),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])

    async def test_previously_prepared_unknown_size_blocks_further_admission(self):
        self.store.update({'trial':{'backfill_count':0,'following_count':2}},expected_revision=1)
        worker=self.worker()
        self.release(10,100)
        for _ in range(3):await worker.attempt()
        db=connect(self.database)
        try:
            with transaction(db):db.execute('UPDATE acquisition_plans SET selected_size_bytes=NULL')
        finally:db.close()
        self.release(11,100)
        for _ in range(3):await worker.attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(1,)])
        self.assertIn(('ACQUISITION_SIZE_EVIDENCE_MISSING',),
            self.rows("SELECT last_reason_code FROM jobs WHERE kind='PREPARE_ACQUISITION'"))

    async def test_budget_creation_rolls_back_with_failed_completion(self):
        worker=self.worker(fail_after_insert=True)
        self.release(10,100)
        await worker.attempt()
        await worker.attempt()
        with self.assertRaises(ValueError):await worker.attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_budgets'),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])
