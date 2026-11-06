"""WI-13: a candidate's stop keeps its real reason; Cancel moves on to the next release after cleanup."""
import json
from types import SimpleNamespace
import unittest

from src.nextgen.cancellation_service import CancellationService
from src.nextgen.storage.batch_candidates import (
    coordinate_release, schedule_after_cleanup_batch, succeed_operation_candidate,
    auto_cleanup_failed_batch_candidate,
)
from src.nextgen.storage.cancellation import CancellationStore
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import connect, enqueue_job, transaction
from src.nextgen.tests import test_api_v1 as v1
from src.nextgen.tests.test_batch_candidate_controller import make_series_json
from src.nextgen.tests.test_torrent_submission import Secrets

AID, TID = 16999, 393400


def digest(n):
    return f'{n:040x}'


class CandidateTransitionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = v1.ApiV1Tests.asyncSetUp
    request = v1.ApiV1Tests.request

    def sql(self, statement, args=()):
        db = connect(self.database)
        try:
            with db:
                return db.execute(statement, args).fetchall()
        finally:
            db.close()

    def arrive(self, n, title=None):
        """An identified season pack, coordinated like VERIFY_RELEASE does. Returns its job id."""
        title = title or f'[Group{n}] Cyberpunk Edgerunners S01 (Batch) [1080p]'
        db = connect(self.database)
        try:
            with transaction(db):
                if n == 1:
                    db.execute("INSERT INTO identified_anime(anidb_id,tvdb_series_id,title) "
                               "VALUES(?,?,'Cyberpunk: Edgerunners')", (AID, TID))
                db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,state) "
                           "VALUES(?, 'autobrr', ?, ?, 'IDENTIFIED')",
                           (n, json.dumps({'release_name': title, 'torrent_infohash': digest(n)}).encode(), 1000 + n))
                db.execute("INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,validated_at_ms) "
                           "VALUES(?,?,2,?,?)", (n, AID, make_series_json(), 1000 + n))
                job = enqueue_job(db, component='intake', kind='VERIFY_RELEASE', payload={'anidb_id': AID},
                                  event_id=n, available_at_ms=0)
        finally:
            db.close()
        self.coordinate(n, job, title)
        return job

    def coordinate(self, n, job, title=None):
        title = title or f'[Group{n}] Cyberpunk Edgerunners S01 (Batch) [1080p]'
        db = connect(self.database)
        try:
            with transaction(db):
                return coordinate_release(db, anidb_id=AID, tvdb_series_id=TID, season=1, info_hash=digest(n),
                                          title=title, event_id=n, job_id=job)
        finally:
            db.close()

    def candidates(self):
        return dict(self.sql("SELECT info_hash,state FROM batch_candidates"))

    def setup_two(self):
        """A is active and running; B waits in the pool (its own job finished as pooled)."""
        a = self.arrive(1)
        b = self.arrive(2)
        self.sql("UPDATE jobs SET state='DONE' WHERE job_id=?", (b,))
        claim, _ = JobQueue(self.database).claim()
        self.assertEqual(claim.job_id, a)
        return a, b, claim

    def verify_jobs(self, event):
        return self.sql("SELECT job_id,state FROM jobs WHERE kind='VERIFY_RELEASE' AND event_id=? ORDER BY job_id",
                        (event,))

    async def test_user_cancel_through_the_api_cancels_cleans_up_then_starts_the_next_release(self):
        a, b, claim = self.setup_two()
        status, result, _ = await self.request('POST', f'/api/v1/jobs/{a}/cancel', {}, token=v1.ADMIN)
        self.assertEqual(status, 202)
        control = result['data']['job_id']
        self.assertEqual(self.sql("SELECT kind FROM jobs WHERE job_id=?", (control,)),
                         [('CLEANUP_CANCELED_OPERATION',)])                     # A real cleanup job.
        self.assertEqual(self.sql("SELECT disposition,reason_code FROM batch_candidate_stops"),
                         [('CANCELED', 'JOB_CANCELED')])
        # Nothing moves on before cleanup has completed.
        self.assertEqual(self.candidates()[digest(1)], 'ACTIVE')
        self.assertEqual(self.verify_jobs(2), [(b, 'DONE')])
        CancellationStore(self.database).acknowledge(claim)                     # The running job drains...
        CancellationService(self.store.read(), Secrets()).cleanup(a)            # ...and cleanup completes.
        self.assertEqual(self.candidates(), {digest(1): 'CANCELED', digest(2): 'ACTIVE'})
        self.assertEqual(self.sql("SELECT reason_code FROM batch_candidates WHERE info_hash=?", (digest(1),)),
                         [('JOB_CANCELED',)])
        jobs = self.verify_jobs(2)
        self.assertEqual([state for _, state in jobs], ['DONE', 'PENDING'])
        # The scope runs B's new job; the canceled release is never picked again.
        op = self.sql("SELECT operation_id FROM job_operation_members WHERE job_id=?", (jobs[-1][0],))[0][0]
        self.assertEqual(self.sql("SELECT operation_id,state FROM batch_candidate_scopes"), [(op, 'ACTIVE')])
        # The canceled release arriving again is pooled, never reactivated.
        self.assertEqual(self.coordinate(1, a).action, 'POOLED')
        self.assertEqual(self.candidates()[digest(1)], 'CANCELED')

    async def test_an_immediate_cancel_with_nothing_to_delete_also_moves_on(self):
        # A is active but not running and owns no torrent or files: the API
        # completes the cancellation at once, without a cleanup job.
        a = self.arrive(1)
        b = self.arrive(2)
        self.sql("UPDATE jobs SET state='DONE' WHERE job_id=?", (b,))
        status, result, _ = await self.request('POST', f'/api/v1/jobs/{a}/cancel', {}, token=v1.ADMIN)
        self.assertEqual((status, result['data']['completed']), (200, True))
        self.assertEqual(self.sql("SELECT count(*) FROM jobs WHERE kind='CLEANUP_CANCELED_OPERATION'"), [(0,)])
        self.assertEqual(self.candidates(), {digest(1): 'CANCELED', digest(2): 'ACTIVE'})
        self.assertEqual([state for _, state in self.verify_jobs(2)], ['DONE', 'PENDING'])
        # Cancelling again changes nothing.
        await self.request('POST', f'/api/v1/jobs/{a}/cancel', {}, token=v1.ADMIN)
        self.assertEqual(len(self.verify_jobs(2)), 2)

    async def test_a_definitive_failure_is_failed_with_its_reason_and_also_moves_on(self):
        a, b, claim = self.setup_two()
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='FAILED',last_reason_code='TORRENT_METADATA_TIMEOUT' "
                           "WHERE job_id=?", (a,))
                auto_cleanup_failed_batch_candidate(db, SimpleNamespace(job_id=a, event_id=1))
        finally:
            db.close()
        self.assertEqual(self.sql("SELECT disposition,reason_code FROM batch_candidate_stops"),
                         [('FAILED', 'TORRENT_METADATA_TIMEOUT')])
        CancellationService(self.store.read(), Secrets()).cleanup(a)
        self.assertEqual(self.candidates(), {digest(1): 'FAILED', digest(2): 'ACTIVE'})
        self.assertEqual(self.sql("SELECT reason_code FROM batch_candidates WHERE info_hash=?", (digest(1),)),
                         [('TORRENT_METADATA_TIMEOUT',)])

    async def test_advancing_is_idempotent_across_restarts(self):
        a, b, claim = self.setup_two()
        op = self.sql("SELECT operation_id FROM job_operation_members WHERE job_id=?", (a,))[0][0]
        await self.request('POST', f'/api/v1/jobs/{a}/cancel', {}, token=v1.ADMIN)
        CancellationStore(self.database).acknowledge(claim)
        CancellationService(self.store.read(), Secrets()).cleanup(a)
        db = connect(self.database)
        try:
            with transaction(db):   # A repeated completion (a restarted cleanup) changes nothing.
                self.assertEqual(schedule_after_cleanup_batch(db, op), ())
        finally:
            db.close()
        self.assertEqual(len(self.verify_jobs(2)), 2)
        self.assertEqual(self.sql("SELECT count(*) FROM batch_candidates WHERE state='ACTIVE'"), [(1,)])

    async def test_without_more_candidates_the_scope_is_exhausted(self):
        a = self.arrive(1)
        claim, _ = JobQueue(self.database).claim()
        await self.request('POST', f'/api/v1/jobs/{a}/cancel', {}, token=v1.ADMIN)
        CancellationStore(self.database).acknowledge(claim)
        CancellationService(self.store.read(), Secrets()).cleanup(a)
        self.assertEqual(self.candidates(), {digest(1): 'CANCELED'})
        self.assertEqual(self.sql("SELECT state FROM batch_candidate_scopes"), [('EXHAUSTED',)])

    async def test_rejecting_the_series_does_not_start_the_next_release(self):
        a, b, claim = self.setup_two()
        op = self.sql("SELECT operation_id FROM job_operation_members WHERE job_id=?", (a,))[0][0]
        self.sql("INSERT INTO anime_review_decisions(anidb_id,operation_id,job_id,decision,cleanup_state,decided_at_ms) "
                 "VALUES(?,?,?,'REJECT','PENDING',1)", (AID, op, a))
        await self.request('POST', f'/api/v1/jobs/{a}/cancel', {}, token=v1.ADMIN)
        CancellationStore(self.database).acknowledge(claim)
        CancellationService(self.store.read(), Secrets()).cleanup(a)
        self.assertEqual(self.candidates(), {digest(1): 'CANCELED', digest(2): 'READY'})
        self.assertEqual(len(self.verify_jobs(2)), 1)

    async def test_only_staging_completion_makes_the_candidate_succeed(self):
        a, b, claim = self.setup_two()
        op = self.sql("SELECT operation_id FROM job_operation_members WHERE job_id=?", (a,))[0][0]
        self.assertEqual(self.sql("SELECT state FROM batch_candidate_scopes"), [('ACTIVE',)])
        db = connect(self.database)
        try:
            with transaction(db):
                succeed_operation_candidate(db, op)     # Called by mark_staged once every episode is published.
        finally:
            db.close()
        self.assertEqual(self.candidates(), {digest(1): 'SUCCEEDED', digest(2): 'READY'})
        self.assertEqual(self.sql("SELECT state FROM batch_candidate_scopes"), [('SUCCEEDED',)])
        self.assertEqual(self.arrive(3) is not None, True)                 # A later arrival only pools.
        self.assertEqual(self.sql("SELECT count(*) FROM batch_candidates WHERE state='ACTIVE'"), [(0,)])


if __name__ == '__main__':
    unittest.main()
