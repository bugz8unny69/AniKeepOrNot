"""Offline async worker integration and cancellation cases; intentionally unrun."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationError
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import Outcome, Policy, Registration, Registry
from src.nextgen.maintenance import MaintenanceBusy
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction
from src.nextgen.worker import Worker, WorkerNotReady, blocking


class AsyncWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "queue.sqlite"
        self.store = ConfigurationStore(self.root)
        self.startup = self.store.initialize({"storage":{"database_path":str(self.database)},
                                              "worker":{"concurrency":2}}).snapshot
        db = sqlite3.connect(self.database)
        initialize(db)
        db.close()
        self.policy = Policy(lease_ms=30000,poll_ms=10)

    def enqueue(self, kind="TEST", payload=None):
        db = sqlite3.connect(self.database)
        try:
            with transaction(db):
                return enqueue_job(db,kind=kind,payload={} if payload is None else payload,
                                   component="intake",available_at_ms=0)
        finally:
            db.close()

    def rows(self):
        db = sqlite3.connect(self.database)
        try:
            return db.execute("SELECT state,attempt_count,last_reason_code FROM jobs ORDER BY job_id").fetchall()
        finally:
            db.close()

    def worker(self, execute, mode="async"):
        spec = Registration("TEST",HandlerDefinition("TEST", "Test work"),execute,mode=mode)
        worker = Worker(self.store,self.startup,Registry((spec,)),policy=self.policy)
        self.addAsyncCleanup(worker.close)
        return worker

    async def test_async_attempt_completes_and_unknown_kind_goes_to_review(self):
        calls = []
        async def handler(context):
            calls.append(context.claim.job_id)
            return Outcome()
        first = self.enqueue()
        self.enqueue("UNKNOWN")
        worker = self.worker(handler)
        self.assertTrue(await worker.attempt())
        self.assertTrue(await worker.attempt())
        self.assertEqual(calls,[first])
        self.assertEqual(self.rows(),[("DONE",1,None),("NEEDS_REVIEW",1,"UNSUPPORTED_JOB_KIND")])

    async def test_current_configuration_per_attempt_and_no_whole_anime_serialization(self):
        release, first_started, second_started = asyncio.Event(),asyncio.Event(),asyncio.Event()
        seen = []
        async def handler(context):
            seen.append(context.configuration.value("storage.trial_root"))
            (first_started if len(seen)==1 else second_started).set()
            await release.wait()
            return Outcome()
        self.enqueue(payload={"anime":1})
        worker = self.worker(handler)
        first = asyncio.create_task(worker.attempt())
        try:
            await asyncio.wait_for(first_started.wait(),2)
            self.store.update({"storage":{"trial_root":str(self.root/"trial")}},expected_revision=1)
            self.enqueue(payload={"anime":1})
            second = asyncio.create_task(worker.attempt())
            try:
                await asyncio.wait_for(second_started.wait(),2)
                self.assertEqual(seen,["",str(self.root/"trial")])
            finally:
                release.set()
                await second
        finally:
            release.set()
            await first
        self.assertEqual([row[0] for row in self.rows()],["DONE","DONE"])

    async def test_thread_cancellation_keeps_maintenance_admission_until_thread_finishes(self):
        entered, release = threading.Event(),threading.Event()
        def handler(context):
            entered.set()
            release.wait(5)
            return Outcome()
        self.enqueue()
        worker = self.worker(handler,"thread")
        task = asyncio.create_task(worker.attempt())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            task.cancel()
            await asyncio.sleep(0)
            ticket = worker.gate.request()
            with self.assertRaises(MaintenanceBusy):
                with worker.gate.exclusive(ticket):
                    self.fail("thread still has work admission")
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.rows()[0][0],"RUNNING")
        with worker.gate.exclusive(ticket) as lease:
            lease.complete()

    async def test_stop_drains_and_does_not_claim_more_jobs(self):
        entered, release = asyncio.Event(),asyncio.Event()
        async def handler(context):
            entered.set()
            await release.wait()
            return Outcome()
        self.enqueue()
        worker = self.worker(handler)
        running = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(entered.wait(),2)
            worker.stop()
            self.enqueue()
        finally:
            release.set()
            await asyncio.wait_for(running,3)
        self.assertEqual([row[0] for row in self.rows()],["DONE","PENDING"])

    async def test_maintenance_and_readiness_block_new_claims(self):
        async def handler(context):
            return Outcome()
        with self.assertRaises(WorkerNotReady):
            Worker(self.store,self.startup,Registry(()))
        self.enqueue()
        worker = self.worker(handler)
        ticket = worker.gate.request()
        self.assertFalse(await worker.attempt())
        self.assertEqual(self.rows()[0][0],"PENDING")
        with worker.gate.exclusive(ticket) as lease:
            lease.complete()
        self.assertTrue(await worker.attempt())

    async def test_unexpected_handler_error_is_reason_coded_not_raw_text(self):
        async def handler(context):
            raise RuntimeError("private credentials must not be stored")
        self.enqueue()
        worker = self.worker(handler)
        await worker.attempt()
        self.assertEqual(self.rows()[0][2],"JOB_HANDLER_FAILED")
        db = sqlite3.connect(self.database)
        try:
            rows = db.execute("SELECT event_code,reason_code,details_json FROM job_history").fetchall()
            self.assertNotIn("private credentials",repr(rows))
            self.assertIn("HANDLER_FAILED",[row[0] for row in rows])
        finally:
            db.close()

    async def test_unexpected_handler_error_persists_failed_check_in_durable_history(self):
        """Medium 6: Unexpected handler error records structured failed_check in job_history."""
        import json
        from src.nextgen.reasons import _CHECK
        async def handler(context):
            raise ValueError("bad parameter")
        self.enqueue()
        worker = self.worker(handler)
        await worker.attempt()
        db = sqlite3.connect(self.database)
        try:
            row = db.execute("SELECT details_json FROM job_history WHERE event_code='RETRY_SCHEDULED'").fetchone()
            self.assertIsNotNone(row)
            details = json.loads(row[0])
            self.assertIn("failed_check", details)
            self.assertTrue(_CHECK.fullmatch(details["failed_check"]))
            self.assertTrue(details["failed_check"].endswith("VALUEERROR"))
        finally:
            db.close()

    async def test_run_bounds_active_jobs_by_startup_concurrency(self):
        entered, release = asyncio.Event(),asyncio.Event()
        active = 0
        peak = 0
        async def handler(context):
            nonlocal active,peak
            active += 1
            peak = max(peak,active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
                return Outcome()
            finally:
                active -= 1
        for _ in range(6): self.enqueue()
        worker = self.worker(handler)
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(entered.wait(),2)
            worker.stop()
        finally:
            release.set()
            await asyncio.wait_for(task,3)
        self.assertEqual(peak,2)
        self.assertEqual(sum(row[0]=="DONE" for row in self.rows()),2)
        self.assertEqual(sum(row[0]=="PENDING" for row in self.rows()),4)


    async def test_callable_cancelled_error_does_not_spin_and_handler_is_retried(self):
        def cancelled():
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(blocking(cancelled),2)
        self.policy = Policy(lease_ms=30000,poll_ms=10,max_retries=0)
        for mode in ("async", "thread"):
            async def async_handler(context):
                raise asyncio.CancelledError()
            def thread_handler(context):
                raise asyncio.CancelledError()
            self.enqueue()
            worker = self.worker(async_handler if mode == "async" else thread_handler,mode)
            self.assertTrue(await asyncio.wait_for(worker.attempt(),2))
        self.assertEqual([row[2] for row in self.rows()],["JOB_RETRY_EXHAUSTED"]*2)

    async def test_embedded_attempts_share_limit_and_close_drains_them(self):
        entered, release = asyncio.Event(),asyncio.Event()
        active = 0
        peak = 0
        async def handler(context):
            nonlocal active,peak
            active += 1
            peak = max(peak,active)
            if active == 2: entered.set()
            try:
                await release.wait()
                return Outcome()
            finally:
                active -= 1
        for _ in range(4): self.enqueue()
        worker = self.worker(handler)
        attempts = [asyncio.create_task(worker.attempt()) for _ in range(4)]
        closes = []
        try:
            await asyncio.wait_for(entered.wait(),2)
            self.assertEqual(peak,2)
            self.assertEqual(sum(row[0]=="RUNNING" for row in self.rows()),2)
            closes = [asyncio.create_task(worker.close()) for _ in range(2)]
            await asyncio.sleep(0)
            self.assertTrue(all(not task.done() for task in closes))
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*attempts,*closes),3)
        self.assertEqual(peak,2)
        self.assertEqual([row[0] for row in self.rows()],["DONE","DONE","PENDING","PENDING"])

    async def test_historical_oversized_payload_is_reviewed_without_handler_call(self):
        async def handler(context):
            self.fail("invalid payload must not reach handler")
        job = self.enqueue()
        db = sqlite3.connect(self.database)
        try:
            with db:
                db.execute("UPDATE jobs SET payload_json=? WHERE job_id=?",('"' + 'x'*65536 + '"',job))
        finally:
            db.close()
        worker = self.worker(handler)
        await worker.attempt()
        self.assertEqual(self.rows(),[("NEEDS_REVIEW",1,"JOB_PAYLOAD_INVALID")])


    async def test_unsafe_live_destination_and_pinned_database_prevent_claim(self):
        async def handler(context):
            self.fail("unsafe effective settings must prevent invocation")
        self.enqueue()
        worker = self.worker(handler)
        self.store.update({"storage": {
            "database_path":str(self.root.parent/"unopened-nextgen.sqlite"),
            "trial_root":str(self.root)}},expected_revision=1)
        with self.assertRaises(ConfigurationError):
            await worker.attempt()
        self.assertEqual(self.rows(),[("PENDING",0,None)])
        # Failure must also release admission, so management can repair/restart.
        ticket = worker.gate.request()
        with worker.gate.exclusive(ticket) as lease:
            lease.complete()
