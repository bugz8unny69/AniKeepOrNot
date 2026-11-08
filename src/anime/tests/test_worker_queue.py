"""Offline worker lifecycle/concurrency/fencing cases; authored, not executed."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import CompletionRejected, Outcome, Policy, Registration, Registry, Successor
from src.anime.storage.job_queue import JobQueue, LeaseLost, QueueUnavailable
from src.anime.storage.sqlite import enqueue_job, initialize, transaction


async def no_work(context):
    return Outcome()


class WorkerQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "queue.sqlite"
        db = sqlite3.connect(self.path)
        initialize(db)
        with transaction(db):
            self.job = enqueue_job(db,kind="TEST",payload={},component="intake",available_at_ms=0)
        db.close()
        self.now = 1000
        self.policy = Policy(lease_ms=1000,max_retries=1,backoff_ms=10,max_backoff_ms=100)
        self.queue = JobQueue(self.path,policy=self.policy,clock=lambda:self.now)
        self.spec = Registration("TEST",HandlerDefinition("TEST", "Test work"),no_work)
        self.registry = Registry((self.spec,))

    def state(self):
        with self.queue.connection() as db:
            return db.execute("SELECT state,attempt_count FROM jobs WHERE job_id=?",(self.job,)).fetchone()

    def started(self, invocation="a"*32):
        claim,_ = self.queue.claim()
        self.queue.start(claim,self.spec,invocation,self.registry)
        return claim

    def finish(self, claim, outcome, invocation="a"*32):
        return self.queue.finish(claim,outcome,spec=self.spec,invocation=invocation,registry=self.registry)

    def test_atomic_claims_are_exclusive_across_connections(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _:self.queue.claim()[0],range(2)))
        self.assertEqual(sum(item is not None for item in claims),1)
        self.assertEqual(self.state(),("RUNNING",1))

    def test_renew_and_expiry_recovery_reject_stale_completion(self):
        claim = self.started()
        self.now = 1500
        self.queue.renew(claim)
        self.now = 2500  # Equality is expired.
        with self.assertRaises(LeaseLost): self.queue.renew(claim)
        self.queue.recover_expired()
        self.assertEqual(self.state(),("RETRY_WAIT",1))
        with self.assertRaises(LeaseLost): self.finish(claim,Outcome())
        self.now += 10
        replacement = self.started("b"*32)
        self.assertNotEqual(replacement.token,claim.token)
        with self.assertRaises(LeaseLost): self.finish(claim,Outcome())
        self.finish(replacement,Outcome(),"b"*32)
        self.assertEqual(self.state(),("DONE",2))

    def test_deferral_does_not_spend_failure_retry_budget(self):
        for index in range(5):
            identifier = format(index,"032x")
            claim = self.started(identifier)
            self.finish(claim,Outcome("defer","JOB_WAITING",delay_ms=1),identifier)
            self.now += 1
        claim = self.started("a"*32)
        self.finish(claim,Outcome("retry","JOB_HANDLER_FAILED"))
        self.assertEqual(self.state(),("RETRY_WAIT",6))
        self.now += 10
        claim = self.started("b"*32)
        self.finish(claim,Outcome("retry","JOB_HANDLER_FAILED"),"b"*32)
        self.assertEqual(self.state(),("NEEDS_REVIEW",7))
        with self.queue.connection() as db:
            self.assertEqual(db.execute("SELECT last_reason_code FROM jobs").fetchone()[0],"JOB_RETRY_EXHAUSTED")

    def test_successor_and_fact_adapter_commit_once_with_completion(self):
        def persist(db,claim,value):
            db.execute("INSERT INTO identified_anime VALUES(1,2,'Fact')")
        spec = Registration("TEST",self.spec.definition,no_work,successor_kinds=("TEST",),
            route=lambda value:(Successor.create("TEST",{"next":True}),),persist=persist)
        registry = Registry((spec,))
        claim,_ = self.queue.claim()
        self.queue.start(claim,spec,"a"*32,registry)
        records = self.queue.finish(claim,Outcome(),spec=spec,invocation="a"*32,registry=registry)
        self.assertIn("CREATED",[record.event_code for record in records])
        with self.queue.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identified_anime").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT state,created_by_job_id FROM jobs ORDER BY job_id").fetchall(),[("DONE",None),("PENDING",self.job)])
        with self.assertRaises(LeaseLost):
            self.queue.finish(claim,Outcome(),spec=spec,invocation="a"*32,registry=registry)

    def test_failed_adapter_or_unauthorized_route_rolls_back_everything(self):
        def persist(db,claim,value):
            db.execute("INSERT INTO identified_anime VALUES(1,2,'Fact')")
        spec = Registration("TEST",self.spec.definition,no_work,route=lambda value:(Successor.create("NOT_ALLOWED",{}),),persist=persist)
        registry = Registry((spec,))
        claim,_ = self.queue.claim()
        self.queue.start(claim,spec,"a"*32,registry)
        with self.assertRaises(ValueError):
            self.queue.finish(claim,Outcome(),spec=spec,invocation="a"*32,registry=registry)
        self.assertEqual(self.state(),("RUNNING",1))
        with self.queue.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identified_anime").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM job_history WHERE event_code='HANDLER_COMPLETED'").fetchone()[0],0)

    def test_adapter_cannot_commit_the_worker_transaction(self):
        spec = Registration("TEST",self.spec.definition,no_work,persist=lambda db,claim,value:db.commit())
        registry = Registry((spec,))
        claim,_ = self.queue.claim()
        self.queue.start(claim,spec,"a"*32,registry)
        with self.assertRaises(QueueUnavailable):
            self.queue.finish(claim,Outcome(),spec=spec,invocation="a"*32,registry=registry)
        self.assertEqual(self.state(),("RUNNING",1))

    def test_nonclaimable_states_and_missing_database_are_not_processed(self):
        with self.queue.connection() as db,transaction(db):
            db.execute("UPDATE jobs SET state='ON_HOLD'")
        self.assertIsNone(self.queue.claim()[0])
        missing = self.path.with_name("missing.sqlite")
        with self.assertRaises(QueueUnavailable): JobQueue(missing).claim()
        self.assertFalse(missing.exists())


    def test_zero_attempt_expired_row_does_not_block_valid_recovery(self):
        claim = self.started()
        with self.queue.connection() as db,transaction(db):
            other = enqueue_job(db,kind="TEST",payload={},component="intake",available_at_ms=0)
            db.execute("UPDATE jobs SET state='RUNNING',lease_token='historical',lease_expires_at_ms=0 WHERE job_id=?",(other,))
        self.now = 2000
        self.queue.recover_expired()
        self.assertEqual(self.state(),("RETRY_WAIT",1))
        with self.queue.connection() as db:
            self.assertEqual(db.execute("SELECT state,attempt_count,last_reason_code,lease_token FROM jobs WHERE job_id=?",(other,)).fetchone(),
                             ("NEEDS_REVIEW",0,"JOB_STATE_INVALID",None))
            self.assertEqual(db.execute("SELECT event_code FROM job_history WHERE job_id=? ORDER BY history_id",(other,)).fetchall(),
                             [("CREATED",),("NEEDS_REVIEW",)])
        with self.assertRaises(LeaseLost): self.finish(claim,Outcome())

    def test_producer_rejects_oversized_payload_before_insert(self):
        with self.queue.connection() as db,transaction(db):
            with self.assertRaises(ValueError):
                enqueue_job(db,kind="TEST",payload={"value":"x"*65536},component="intake")
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0],1)


    def test_expected_completion_rejection_rolls_back_before_review(self):
        def persist(db,claim,value):
            db.execute("INSERT INTO identified_anime VALUES(1,2,'Partial')")
            raise CompletionRejected(Outcome("review","IDENTITY_CONFLICT"))
        spec = Registration("TEST",self.spec.definition,no_work,persist=persist)
        registry = Registry((spec,))
        claim,_ = self.queue.claim()
        self.queue.start(claim,spec,"a"*32,registry)
        with self.assertRaises(CompletionRejected) as rejected:
            self.queue.finish(claim,Outcome(),spec=spec,invocation="a"*32,registry=registry)
        with self.queue.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identified_anime").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM job_history WHERE event_code='HANDLER_COMPLETED'").fetchone()[0],0)
        self.queue.finish(claim,rejected.exception.outcome,spec=spec,invocation="a"*32,registry=registry)
        self.assertEqual(self.state(),("NEEDS_REVIEW",1))
        for outcome in (Outcome(),Outcome("retry","JOB_HANDLER_FAILED")):
            with self.assertRaises(ValueError): CompletionRejected(outcome)

    def test_invalid_audit_receipt_contract_rolls_back_adapter_facts(self):
        def persist(db,claim,value):
            db.execute("INSERT INTO identified_anime VALUES(1,2,'Partial')")
            return ("raw private text",)
        spec = Registration("TEST",self.spec.definition,no_work,persist=persist)
        registry = Registry((spec,))
        claim,_ = self.queue.claim()
        self.queue.start(claim,spec,"a"*32,registry)
        with self.assertRaises(ValueError):
            self.queue.finish(claim,Outcome(),spec=spec,invocation="a"*32,registry=registry)
        with self.queue.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identified_anime").fetchone()[0],0)
        self.assertEqual(self.state(),("RUNNING",1))


    def test_lease_expiring_inside_persistence_rolls_back_facts_and_ending(self):
        def persist(db,claim,value):
            db.execute("INSERT INTO identified_anime VALUES(1,2,'Partial')")
            self.now += self.policy.lease_ms
        spec = Registration("TEST",self.spec.definition,no_work,persist=persist)
        registry = Registry((spec,))
        claim,_ = self.queue.claim()
        self.queue.start(claim,spec,"a"*32,registry)
        with self.assertRaises(LeaseLost):
            self.queue.finish(claim,Outcome(),spec=spec,invocation="a"*32,registry=registry)
        with self.queue.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identified_anime").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM job_history WHERE event_code='HANDLER_COMPLETED'").fetchone()[0],0)
        self.assertEqual(self.state(),("RUNNING",1))
