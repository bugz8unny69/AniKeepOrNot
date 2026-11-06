"""Offline episode-gate lifecycle/concurrency cases. Intentionally unexecuted."""
import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.job_contracts import CompletionRejected, Outcome, Policy, Registry, Successor
from src.nextgen.jobs.select_user_evaluation_job import registration as evaluate
from src.nextgen.jobs.reserve_episode_job import registration as reserve
from src.nextgen.storage.episode_reservations import load_reservation_input
from src.nextgen.storage.job_queue import LeaseLost
from src.nextgen.storage.sqlite import connect, initialize, accept_event_and_job, save_identity, enqueue_job, transaction
from src.nextgen.worker import Worker


class EpisodeReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.database=self.root/'queue.sqlite'
        self.store=ConfigurationStore(self.root)
        self.startup=self.store.initialize({'storage':{'database_path':str(self.database)},
                                          'trial':{'backfill_count':0,'following_count':2}}).snapshot
        db=connect(self.database)
        initialize(db)
        db.close()

    def rows(self,sql,args=()):
        db=connect(self.database)
        try:return db.execute(sql,args).fetchall()
        finally:db.close()

    def source(self,episode,*,aid=123,season=1):
        body=b'declared release'
        db=connect(self.database)
        try:
            event,release=accept_event_and_job(db,'test',body,kind='VERIFY_RELEASE',payload={'anidb_id':aid})
            identity=save_identity(db,event,{'schema_version':2,'anime':{'anidb_id':aid,'tvdb_series_id':456,'title':'Test'}})
            eid=aid*100000+season*1000+episode
            tveid=season*1000+episode
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE',attempt_count=1 WHERE job_id=?",(release,))
                db.execute('INSERT OR IGNORE INTO identified_episodes VALUES(?,?)',(eid,aid))
                db.execute("INSERT INTO release_verifications(job_id,identity_result_id,anidb_id,release_sha256,verification_scope,"
                    "anidb_episode_number,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms) "
                    "VALUES(?,?,?,?,'DECLARED_SINGLE_EPISODE',?,?,456,?,?,?,1)",
                    (release,identity,aid,hashlib.sha256(body).hexdigest(),episode,eid,season,episode,tveid))
                evaluation=enqueue_job(db,kind='SELECT_USER_EVALUATION',payload={'verification_job_id':release},
                    component='worker',event_id=event,created_by_job_id=release,available_at_ms=0)
            return event,evaluation
        finally:db.close()

    def worker(self,spec=None):
        worker=Worker(self.store,self.startup,Registry((evaluate(reservation_kind='RESERVE_EPISODE'),spec or reserve())),
                      policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        return worker

    async def test_selection_atomically_hands_off_and_records_readable_reservation(self):
        event,evaluation=self.source(10)
        worker=self.worker()
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state,event_id,created_by_job_id FROM jobs WHERE kind='RESERVE_EPISODE'"),
                         [('PENDING',event,evaluation)])
        receipts=[]
        with patch('src.nextgen.worker.log_history',side_effect=lambda logger,record:receipts.append(record)):
            await worker.attempt()
        self.assertEqual(self.rows('SELECT tvdb_series_id,tvdb_season,tvdb_episode FROM episode_reservations'),[(456,1,10)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='RESERVE_EPISODE'"),[('DONE',)])
        self.assertIn(('EPISODE_RESERVATION_CREATED','EPISODE_RESERVATION_GRANTED'),
                      [(r.event_code,r.reason_code) for r in receipts])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind NOT IN ('VERIFY_RELEASE','SELECT_USER_EVALUATION','RESERVE_EPISODE')"),[(0,)])

    async def test_competing_releases_hold_even_after_owner_job_completes(self):
        self.source(10)
        self.source(10)
        worker=self.worker()
        await worker.attempt()
        await worker.attempt()
        await asyncio.gather(worker.attempt(),worker.attempt())
        self.assertEqual(self.rows('SELECT count(*) FROM episode_reservations'),[(1,)])
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='RESERVE_EPISODE' ORDER BY state"),
                         [('DONE',None),('ON_HOLD','EPISODE_RESERVED')])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='EPISODE_RESERVATION_CREATED'"),[(1,)])

    async def test_released_reservation_lets_the_held_duplicate_take_over(self):
        from src.nextgen.storage.episode_reservations import wake_waiters
        self.source(10)
        self.source(10)
        worker=self.worker()
        await worker.attempt()
        await worker.attempt()
        await asyncio.gather(worker.attempt(),worker.attempt())
        (reservation,owner),=self.rows("SELECT reservation_id,owner_job_id FROM episode_reservations")
        db=connect(self.database)
        try:
            with transaction(db):
                # The owner's copy was dropped (e.g. over the size limit) and cleaned up.
                db.execute("UPDATE episode_reservations SET state='RELEASED',ended_at_ms=1,ended_by_job_id=? WHERE reservation_id=?",(owner,reservation))
                records=wake_waiters(db,reservation,1)
        finally:db.close()
        self.assertEqual([r.event_code for r in records],['RETRY_SCHEDULED'])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='RESERVE_EPISODE' AND job_id<>? ",(owner,)),[('PENDING',)])
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM episode_reservations WHERE state='ACTIVE'"),[(1,)])
        self.assertNotEqual(self.rows("SELECT owner_job_id FROM episode_reservations WHERE state='ACTIVE'"),[(owner,)])

    async def test_different_episodes_of_same_anime_are_independent(self):
        worker=self.worker()
        self.source(10)
        await worker.attempt()
        await worker.attempt()
        self.source(11)
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT tvdb_episode FROM episode_reservations ORDER BY tvdb_episode'),[(10,),(11,)])

    async def test_outside_window_completes_without_a_reservation(self):
        worker=self.worker()
        self.source(10)
        await worker.attempt()
        await worker.attempt()
        self.source(12)
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT tvdb_episode FROM episode_reservations'),[(10,)])
        self.assertEqual(self.rows("SELECT reason_code FROM job_history WHERE event_code='EPISODE_RESERVATION_SKIPPED'"),
                         [('USER_EVALUATION_OUTSIDE_WINDOW',)])

    async def test_conflicting_anidb_ownership_is_review_not_duplicate(self):
        worker=self.worker()
        self.source(10)
        await worker.attempt()
        await worker.attempt()
        self.source(10,aid=124)
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='RESERVE_EPISODE' ORDER BY job_id"),
                         [('DONE',None),('NEEDS_REVIEW','EPISODE_RESERVATION_CONFLICT')])
        self.assertEqual(self.rows('SELECT anidb_id FROM episode_reservations'),[(123,)])

    async def test_wrong_event_reference_cannot_reserve(self):
        event,evaluation=self.source(10)
        worker=self.worker()
        await worker.attempt()
        other,_=accept_event_and_job_in_fixture(self.database)
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET event_id=? WHERE kind='RESERVE_EPISODE'",(other,))
        finally:db.close()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM episode_reservations'),[(0,)])
        self.assertEqual(self.rows("SELECT last_reason_code FROM jobs WHERE kind='RESERVE_EPISODE'"),[('EPISODE_RESERVATION_CONFLICT',)])

    async def test_source_change_after_read_rejects_completion_and_rolls_back(self):
        event,evaluation=self.source(10)
        worker=self.worker()
        await worker.attempt()
        claim,_=worker.queue.claim()
        spec=worker.registry.entries['RESERVE_EPISODE']
        worker.queue.start(claim,spec,'b'*32,worker.registry)
        value=load_reservation_input(self.database,evaluation,event)
        db=connect(self.database)
        try:
            with transaction(db):db.execute('UPDATE incoming_events SET raw_body=? WHERE event_id=?',(b'changed',event))
        finally:db.close()
        with self.assertRaises(CompletionRejected):
            worker.queue.finish(claim,Outcome(value=value),spec=spec,invocation='b'*32,registry=worker.registry)
        self.assertEqual(self.rows('SELECT count(*) FROM episode_reservations'),[(0,)])

    async def test_stale_worker_cannot_reserve(self):
        event,evaluation=self.source(10)
        worker=self.worker()
        await worker.attempt()
        started=worker.queue.now()
        worker.queue.clock=lambda:started
        claim,_=worker.queue.claim()
        spec=worker.registry.entries['RESERVE_EPISODE']
        worker.queue.start(claim,spec,'b'*32,worker.registry)
        value=load_reservation_input(self.database,evaluation,event)
        worker.queue.clock=lambda:started+30000
        with self.assertRaises(LeaseLost):
            worker.queue.finish(claim,Outcome(value=value),spec=spec,invocation='b'*32,registry=worker.registry)
        self.assertEqual(self.rows('SELECT count(*) FROM episode_reservations'),[(0,)])

    async def test_failure_after_insert_rolls_back_reservation_and_audit(self):
        self.source(10)
        spec=replace(reserve(),route=lambda value:(Successor.create('NOT_ALLOWED',{}),))
        worker=self.worker(spec)
        await worker.attempt()
        with self.assertRaises(ValueError):await worker.attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM episode_reservations'),[(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='EPISODE_RESERVATION_CREATED'"),[(0,)])


def accept_event_and_job_in_fixture(database):
    db=connect(database)
    try:return accept_event_and_job(db,'other',b'other',kind='NOT_INSTALLED',payload={})
    finally:db.close()
