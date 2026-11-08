"""Offline cancel/reacquire history and active-source fencing; authored/unrun."""
import unittest

from src.anime.cancellation import CancellationError
from src.anime.cancellation_service import CancellationService
from src.anime.storage.management_actions import ManagementActions
from src.anime.storage.reservation_release import release_cancelled
from src.anime.storage.sqlite import connect,transaction
from src.anime.storage.acquisition import load_acquisition_input
from src.anime.acquisition.metadata import AcquisitionConflict
from src.anime.tests.test_acquisition_preparation import AcquisitionPreparationTests


class ReservationReleaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    worker=AcquisitionPreparationTests.worker
    prepare_queue=AcquisitionPreparationTests.prepare_queue

    async def prepared(self):
        worker=self.worker()
        event,evaluation=await self.prepare_queue(worker);await worker.attempt()
        target=self.rows("SELECT job_id FROM jobs WHERE kind='PREPARE_ACQUISITION'")[0][0]
        return worker,event,evaluation,target

    async def test_cleanup_releases_slot_and_same_hash_budget_without_erasing_history(self):
        worker,event,evaluation,target=await self.prepared()
        old=self.rows('SELECT reservation_id,owner_job_id FROM episode_reservations')[0]
        result=CancellationService(self.store.read(),object()).cancel(target)
        self.assertEqual(result['cleanup_state'],'COMPLETE')
        self.assertEqual(self.rows('SELECT state FROM episode_reservations'),[('RELEASED',)])
        self.assertEqual(self.rows('SELECT state FROM acquisition_plans'),[('RELEASED',)])
        with self.assertRaises(AcquisitionConflict):load_acquisition_input(self.database,evaluation,event)
        await self.prepare_queue(worker);await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM episode_reservations ORDER BY reservation_id'),[('RELEASED',),('ACTIVE',)])
        self.assertEqual(self.rows('SELECT state FROM acquisition_plans ORDER BY plan_id'),[('RELEASED',),('PREPARED',)])
        self.assertEqual(self.rows('SELECT reservation_id,owner_job_id FROM episode_reservations ORDER BY reservation_id')[0],old)
        self.assertEqual(self.rows('SELECT count(DISTINCT submission_marker) FROM acquisition_plans'),[(2,)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='EPISODE_RESERVATIONS_RELEASED'"),[(1,)])
        CancellationService(self.store.read(),object()).cleanup(target)
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='EPISODE_RESERVATIONS_RELEASED'"),[(1,)])

    async def test_staged_or_kept_scope_cannot_release_its_duplicate_protection(self):
        worker,event,evaluation,target=await self.prepared()
        db=connect(self.database)
        try:
            with transaction(db):
                scope=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',(target,)).fetchone()[0]
                db.execute("UPDATE job_operations SET state='STAGED' WHERE operation_id=?",(scope,))
            with self.assertRaises(CancellationError):
                with transaction(db):release_cancelled(db,scope,target,1,component='api')
        finally:db.close()
        self.assertEqual(self.rows('SELECT state FROM episode_reservations'),[('ACTIVE',)])
        with self.assertRaises(CancellationError):CancellationService(self.store.read(),object()).cancel(target)

    async def test_pending_cleanup_keeps_reservation_and_torrent_hash_claimed(self):
        worker,event,evaluation,target=await self.prepared()
        db=connect(self.database)
        try:
            with transaction(db):
                scope=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',(target,)).fetchone()[0]
                db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state='PENDING',reason_code='CANCELLATION_PENDING' WHERE operation_id=?",(scope,))
            with self.assertRaises(CancellationError):
                with transaction(db):release_cancelled(db,scope,target,1,component='api')
        finally:db.close()
        self.assertEqual(self.rows('SELECT state FROM episode_reservations'),[('ACTIVE',)])
        self.assertEqual(self.rows('SELECT state FROM acquisition_plans'),[('PREPARED',)])

    async def test_old_completed_cancellation_can_explicitly_release_retained_slots(self):
        worker,event,evaluation,target=await self.prepared()
        db=connect(self.database)
        try:
            with transaction(db):
                scope=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',(target,)).fetchone()[0]
                db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state='COMPLETE',reason_code='CANCELLATION_COMPLETE' WHERE operation_id=?",(scope,))
        finally:db.close()
        service=CancellationService(self.store.read(),object())
        self.assertTrue(service.status(target)['can_cleanup'])
        result=service.cleanup(target)
        self.assertFalse(result['can_cleanup'])
        self.assertEqual(self.rows('SELECT state FROM episode_reservations'),[('RELEASED',)])
