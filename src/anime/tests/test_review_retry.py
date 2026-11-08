"""Focused, offline checkpoint-retry storage and HTTP regression cases."""
from pathlib import Path
import tempfile
import unittest

from src.anime.history import append_job_history
from src.anime.job_contracts import Outcome, Policy
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.review_retry import (
    RETRY_KINDS, RetryNotAllowed, RetryStale, capability, request_review_retry,
)
from src.anime.storage.sqlite import (
    accept_event_and_job, connect, initialize, transaction,
)
from src.anime.tests.test_api_v1 import ApiV1Tests, ADMIN, WEB


class ReviewRetryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.database = Path(self.folder.name) / 'anime.sqlite'
        db = connect(self.database)
        initialize(db)
        self.event, self.job = accept_event_and_job(db, 'test',
            b'{"release_name":"Test S01E01"}', kind='STAGE_USER_EVALUATION_MEDIA',
            payload={'verification_job_id':123})
        with transaction(db):
            db.execute("UPDATE jobs SET state='NEEDS_REVIEW',attempt_count=4,"
                "last_reason_code='JOB_RETRY_EXHAUSTED' WHERE job_id=?", (self.job,))
            for n in (1, 2, 3):
                db.execute('UPDATE jobs SET attempt_count=? WHERE job_id=?',(n,self.job))
                append_job_history(db, job_id=self.job, event_code='RETRY_SCHEDULED',
                    reason_code='STAGING_UNAVAILABLE', component='worker',
                    occurred_at_ms=n, details={'attempt':n})
            db.execute('UPDATE jobs SET attempt_count=4 WHERE job_id=?',(self.job,))
        db.close()

    def rows(self, query, args=()):
        db = connect(self.database)
        try:
            return db.execute(query, args).fetchall()
        finally:
            db.close()

    def retry(self, attempt=4, reason='JOB_RETRY_EXHAUSTED'):
        return request_review_retry(self.database, self.job,
            expected_attempt=attempt, expected_reason=reason, clock=lambda:123456)

    def test_same_job_checkpoint_idempotence_and_preserved_history(self):
        result=self.retry()
        self.assertEqual((result.job_id,result.attempt,result.outcome),
                         (self.job,4,'QUEUED'))
        again=self.retry()
        self.assertEqual((again.outcome,again.history_id),
                         ('ALREADY_QUEUED',result.history_id))
        self.assertEqual(self.rows('SELECT count(*) FROM jobs'),[(1,)])
        self.assertEqual(self.rows(
            'SELECT state,attempt_count,last_reason_code,available_at_ms FROM jobs WHERE job_id=?',
            (self.job,)), [('PENDING',4,None,123456)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM job_history WHERE job_id=? AND event_code='RETRY_SCHEDULED'",
            (self.job,)), [(3,)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM job_history WHERE job_id=? AND event_code='RETRY_REQUESTED'",
            (self.job,)), [(1,)])

    def test_budget_restarts_after_manual_retry_without_deleting_history(self):
        self.retry()
        queue=JobQueue(self.database,policy=Policy(max_retries=1),clock=lambda:123457)
        claim,_=queue.claim()
        self.assertEqual(claim.attempt,5)
        with queue.connection() as db,transaction(db):
            record=queue._transition(db,claim,Outcome('retry','STAGING_UNAVAILABLE'),123457)
            self.assertEqual(record.event_code,'RETRY_SCHEDULED')
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(self.job,)),
                         [('RETRY_WAIT',)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM job_history WHERE job_id=? AND event_code='RETRY_SCHEDULED'",
            (self.job,)), [(4,)])

    def test_stale_and_unsupported_states_fail_closed(self):
        with self.assertRaises(RetryStale):self.retry(attempt=3)
        with self.assertRaises(RetryStale):self.retry(reason='STAGING_UNAVAILABLE')
        for state in ('DONE','RUNNING','RETRY_WAIT','CANCELED','BLOCKED','ON_HOLD'):
            with self.subTest(state=state):
                db=connect(self.database)
                with transaction(db):
                    db.execute('UPDATE jobs SET state=?,lease_token=?,lease_expires_at_ms=? WHERE job_id=?',
                               (state,'a'*32 if state=='RUNNING' else None,
                                999999 if state=='RUNNING' else None,self.job))
                db.close()
                with self.assertRaises(RetryNotAllowed):self.retry()
        self.assertEqual(self.rows(
            "SELECT count(*) FROM job_history WHERE event_code='RETRY_REQUESTED'"),[(0,)])

    def test_active_operation_and_no_successor_required(self):
        db=connect(self.database)
        with transaction(db):
            op=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',
                          (self.job,)).fetchone()[0]
            db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state='COMPLETE',"
                       "reason_code='CANCELLATION_COMPLETE' WHERE operation_id=?",(op,))
            self.assertFalse(capability(db,self.job)['enabled'])
        db.close()
        with self.assertRaises(RetryNotAllowed):self.retry()
        db=connect(self.database)
        with transaction(db):
            db.execute("UPDATE job_operations SET state='ACTIVE',cleanup_state='NONE',"
                       "reason_code=NULL WHERE operation_id=?",(op,))
            db.execute("INSERT INTO jobs(event_id,created_by_job_id,kind,payload_json,state,"
                "available_at_ms,created_at_ms,updated_at_ms) "
                "VALUES(?,?,'VERIFY_MEDIA','{}','DONE',1,1,1)",(self.event,self.job))
            self.assertFalse(capability(db,self.job)['enabled'])
        db.close()
        with self.assertRaises(RetryNotAllowed):self.retry()

    def test_requeues_single_and_batch_failed_steps_without_new_jobs(self):
        for kind in ('SUBMIT_TORRENT','DOWNLOAD_TORRENT','STAGE_USER_EVALUATION_MEDIA',
                     'SUBMIT_BATCH','TRANSFER_BATCH','STAGE_BATCH_EPISODE'):
            with self.subTest(kind=kind):
                db=connect(self.database)
                with transaction(db):
                    db.execute("UPDATE jobs SET kind=?,state='NEEDS_REVIEW',attempt_count=4,"
                               "last_reason_code='JOB_RETRY_EXHAUSTED',"
                               "lease_token=NULL,lease_expires_at_ms=NULL WHERE job_id=?",
                               (kind,self.job))
                    self.assertTrue(capability(db,self.job)['enabled'])
                db.close()
                self.assertEqual(self.retry().job_id,self.job)
                self.assertEqual(self.rows('SELECT count(*) FROM jobs'),[(1,)])

    def test_duplicate_retry_after_cancel_is_not_acknowledged_as_queued(self):
        self.retry()
        db=connect(self.database)
        with transaction(db):
            op=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',
                          (self.job,)).fetchone()[0]
            db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state='COMPLETE',"
                       "reason_code='CANCELLATION_COMPLETE' WHERE operation_id=?",(op,))
        db.close()
        with self.assertRaises(RetryNotAllowed):self.retry()

    def test_acquisition_only_allowlist_covers_batch_and_single_steps(self):
        for kind in ('IDENTIFY_ANIME','VERIFY_RELEASE','SUBMIT_TORRENT',
                     'DOWNLOAD_TORRENT','STAGE_USER_EVALUATION_MEDIA','SUBMIT_BATCH',
                     'TRANSFER_BATCH','VERIFY_BATCH_EPISODE','STAGE_BATCH_EPISODE'):
            self.assertIn(kind,RETRY_KINDS)
        for kind in ('KEEP_MEDIA','KEEP_BATCH','REJECT_SERIES',
                     'CLEANUP_CANCELED_OPERATION','CLEANUP_BATCH_DOWNLOAD'):
            self.assertNotIn(kind,RETRY_KINDS)
        db=connect(self.database)
        with transaction(db):
            db.execute("UPDATE jobs SET kind='KEEP_BATCH' WHERE job_id=?",(self.job,))
            self.assertFalse(capability(db,self.job)['enabled'])
        db.close()
        with self.assertRaises(RetryNotAllowed):self.retry()


class ReviewRetryApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ApiV1Tests.asyncSetUp
    request=ApiV1Tests.request
    release=ApiV1Tests.release
    rows=ApiV1Tests.rows

    async def test_retry_requires_session_csrf_and_fresh_job_snapshot(self):
        status,response,_=await self.request('POST','/api/v1/webhook',
                                             self.release(),token=WEB)
        self.assertEqual(status,202)
        job=response['data']['job_id']
        with connect(self.database) as db,transaction(db):
            db.execute("UPDATE jobs SET state='NEEDS_REVIEW',attempt_count=3,"
                       "last_reason_code='IDENTITY_INCOMPLETE' WHERE job_id=?",(job,))
        status,original,_=await self.request('GET',f'/api/v1/jobs/{job}',token=ADMIN)
        self.assertEqual(status,200)
        self.assertTrue(original['data']['actions']['retry']['enabled'])
        body={'expected_attempt':3,'expected_reason':'IDENTITY_INCOMPLETE'}
        for token in (None, WEB):
            status,_,_=await self.request('POST',f'/api/v1/jobs/{job}/retry',body,token=token,headers={'origin':'https://anime.example'})
            self.assertEqual(status,401)
        status,_,_=await self.request('POST',f'/api/v1/jobs/{job}/retry',body,
                                     headers={'cookie':self.admin_cookie,'origin':'https://anime.example'})
        self.assertEqual(status,403)
        status,_,_=await self.request('POST',f'/api/v1/jobs/{job}/retry',{},token=ADMIN)
        self.assertEqual(status,400)
        status,_,_=await self.request('POST',f'/api/v1/jobs/{job}/retry',
                        {'expected_attempt':2,'expected_reason':'IDENTITY_INCOMPLETE'},token=ADMIN)
        self.assertEqual(status,409)
        status,result,headers=await self.request('POST',f'/api/v1/jobs/{job}/retry',body,token=ADMIN)
        self.assertEqual(status,202)
        self.assertEqual((result['data']['job_id'],result['data']['state']),(job,'PENDING'))
        self.assertFalse(result['data']['reused'])
        self.assertEqual(headers[b'location'],f'/api/v1/jobs/{job}'.encode())
        status,again,_=await self.request('POST',f'/api/v1/jobs/{job}/retry',body,token=ADMIN)
        self.assertEqual(status,202)
        self.assertTrue(again['data']['reused'])
        self.assertEqual(self.rows('SELECT count(*) FROM jobs'),[(1,)])
        status,current,_=await self.request('GET',f'/api/v1/jobs/{job}',token=ADMIN)
        self.assertEqual(current['data']['state'],'PENDING')
        self.assertFalse(current['data']['actions']['retry']['enabled'])
        status,history,_=await self.request('GET',f'/api/v1/jobs/{job}/history',token=ADMIN)
        self.assertEqual(status,200)
        self.assertIn('RETRY_REQUESTED',[item['event']['code'] for item in history['data']['items']])

    async def test_running_and_canceled_job_not_retryable(self):
        _,response,_=await self.request('POST','/api/v1/webhook',
                                        self.release(),token=WEB)
        job=response['data']['job_id']
        body={'expected_attempt':1,'expected_reason':'IDENTITY_INCOMPLETE'}
        status,_,_=await self.request('POST',f'/api/v1/jobs/{job}/retry',body,token=ADMIN)
        self.assertEqual(status,409)
        with connect(self.database) as db,transaction(db):
            db.execute("UPDATE jobs SET state='NEEDS_REVIEW',attempt_count=1,"
                       "last_reason_code='IDENTITY_INCOMPLETE' WHERE job_id=?",(job,))
            db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state='COMPLETE',"
                       "reason_code='CANCELLATION_COMPLETE' WHERE operation_id="
                       "(SELECT operation_id FROM job_operation_members WHERE job_id=?)",(job,))
        status,_,_=await self.request('POST',f'/api/v1/jobs/{job}/retry',body,token=ADMIN)
        self.assertEqual(status,409)


    async def test_retry_capability_disabled_reasons_explain_exact_causes(self):
        """W-25: Disabled action tooltip explains exact reason, not generic unavailable."""
        from src.anime.storage.review_retry import capability
        _, response, _ = await self.request('POST', '/api/v1/webhook', self.release(), token=WEB)
        job = response['data']['job_id']
        with connect(self.database) as db:
            # Initially PENDING -> MANAGEMENT_JOB_QUEUED
            cap = capability(db, job)
            self.assertFalse(cap['enabled'])
            self.assertEqual(cap['disabled_reason']['code'], 'MANAGEMENT_JOB_QUEUED')

            # RUNNING -> MANAGEMENT_JOB_RUNNING
            with transaction(db):
                db.execute("UPDATE jobs SET state='RUNNING',lease_token='token1',lease_expires_at_ms=9999999999999 WHERE job_id=?", (job,))
            cap = capability(db, job)
            self.assertFalse(cap['enabled'])
            self.assertEqual(cap['disabled_reason']['code'], 'MANAGEMENT_JOB_RUNNING')

            # DONE -> MANAGEMENT_JOB_COMPLETE
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE',lease_token=NULL,lease_expires_at_ms=NULL WHERE job_id=?", (job,))
            cap = capability(db, job)
            self.assertFalse(cap['enabled'])
            self.assertEqual(cap['disabled_reason']['code'], 'MANAGEMENT_JOB_COMPLETE')

            # CANCELED -> MANAGEMENT_JOB_CANCELED
            with transaction(db):
                db.execute("UPDATE jobs SET state='CANCELED' WHERE job_id=?", (job,))
            cap = capability(db, job)
            self.assertFalse(cap['enabled'])
            self.assertEqual(cap['disabled_reason']['code'], 'MANAGEMENT_JOB_CANCELED')

            # ON_HOLD with EPISODE_RESERVED -> MANAGEMENT_JOB_WAITING
            with transaction(db):
                db.execute("UPDATE jobs SET state='ON_HOLD',last_reason_code='EPISODE_RESERVED' WHERE job_id=?", (job,))
            cap = capability(db, job)
            self.assertFalse(cap['enabled'])
            self.assertEqual(cap['disabled_reason']['code'], 'MANAGEMENT_JOB_WAITING')


if __name__=='__main__':
    unittest.main()
