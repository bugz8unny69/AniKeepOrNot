"""Offline cancellation capabilities, fenced handoff and drain; unexecuted."""
import asyncio
from dataclasses import replace
import unittest

from src.anime.cancellation import CancellationRequested, CleanupBusy, OperationGate
from src.anime.cancellation_service import CancellationService
from src.anime.job_contracts import Outcome, Registry, Successor
from src.anime.storage.cancellation import CancellationStore, operation
from src.anime.storage.sqlite import connect, enqueue_job, transaction
from src.anime.tests import test_acquisition_preparation as fixture
from src.anime.tests.test_torrent_submission import Secrets
from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import Registration
from src.anime.worker import Worker


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.AcquisitionPreparationTests.asyncSetUp
    rows=fixture.AcquisitionPreparationTests.rows

    def add(self,kind='TASK',operation_id=None):
        db=connect(self.database)
        try:
            with transaction(db):return enqueue_job(db,kind=kind,payload={},component='api',operation_id=operation_id,available_at_ms=0)
        finally:db.close()

    def service(self):return CancellationService(self.store.read(),Secrets())

    async def test_all_idle_unfinished_states_offer_cancel_and_preserve_history(self):
        for state in ('PENDING','RETRY_WAIT','ON_HOLD','BLOCKED','NEEDS_REVIEW'):
            job=self.add()
            db=connect(self.database)
            try:
                with transaction(db):db.execute('UPDATE jobs SET state=? WHERE job_id=?',(state,job))
            finally:db.close()
            self.assertTrue(self.service().status(job)['can_cancel'])
            result=self.service().cancel(job)
            self.assertEqual((result['state'],result['cleanup_state']),('CANCELED','COMPLETE'))
            self.assertFalse(result['can_cancel'])
            self.assertEqual(self.rows('SELECT state,activity_code FROM jobs WHERE job_id=?',(job,)),[('CANCELED',None)])
            self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE job_id=? AND event_code='CANCELED'",(job,)),[(1,)])
            self.assertEqual(self.service().cancel(job),result)

    async def test_completed_scope_is_not_cancelable(self):
        job=self.add();db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?",(job,))
        finally:db.close()
        self.assertFalse(self.service().status(job)['can_cancel'])

    async def test_explicit_scope_cancels_successor_without_traversing_provenance(self):
        first=self.add();scope=self.service().status(first)['operation_id']
        second=self.add(operation_id=scope);unrelated=self.add()
        db=connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?",(first,))
                db.execute('UPDATE jobs SET created_by_job_id=? WHERE job_id=?',(first,unrelated))
        finally:db.close()
        self.assertTrue(self.service().status(first)['can_cancel'])
        self.service().cancel(first)
        self.assertEqual(self.rows('SELECT job_id,state FROM jobs ORDER BY job_id'),[(first,'DONE'),(second,'CANCELED'),(unrelated,'PENDING')])
        with self.assertRaises(ValueError):self.add(operation_id=scope)

    async def test_running_cancel_fences_completion_and_cleanup_waits_for_drain(self):
        job=self.add()
        from src.anime.storage.job_queue import JobQueue
        queue=JobQueue(self.database)
        claim,_=queue.claim();scope=queue.operation_id(claim)
        gate=OperationGate(self.database)
        with gate.hold(scope):
            self.service().request(job)
            self.assertEqual(self.service().cleanup(job)['cleanup_state'],'PENDING')
            self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('RUNNING',)])
            with self.assertRaises(CancellationRequested):queue.renew(claim)
        result=self.service().cleanup(job)
        self.assertEqual(result['cleanup_state'],'COMPLETE')
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('CANCELED',)])

    async def test_worker_drains_canceled_handler_and_never_routes_successor(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def handle(context):
            entered.set();await release.wait();return Outcome(value=1)
        spec=Registration('TASK',HandlerDefinition('TASK','Example work'),handle)
        spec=replace(spec,route=lambda value:(Successor.create('TASK',{}),),successor_kinds=('TASK',))
        worker=Worker(self.store,self.startup,Registry((spec,)))
        self.addAsyncCleanup(worker.close)
        job=self.add()
        task=asyncio.create_task(worker.attempt())
        await entered.wait()
        self.service().request(job)
        release.set();await task
        self.assertEqual(self.rows('SELECT state FROM jobs'),[('CANCELED',)])
        self.assertEqual(self.service().cleanup(job)['cleanup_state'],'COMPLETE')
