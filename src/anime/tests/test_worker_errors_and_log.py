"""Work items 2 and 4 (docs/WORK_ITEMS_JOB_CLARITY.md).

2: an error no code expected is a bug: logged with its trace, recorded, failed, never retried.
4: "started" and "finished" show at DEBUG; a check that repeats the same wait prints nothing.
"""
import asyncio
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.anime.configuration import ConfigurationStore
from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import Outcome, Policy, Registration, Registry
from src.anime.reasons import _CHECK
from src.anime.storage.sqlite import enqueue_job, initialize, transaction
from src.anime.worker import Worker


class WorkerErrorsAndLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "queue.sqlite"
        self.store = ConfigurationStore(root)
        self.startup = self.store.initialize({"storage": {"database_path": str(self.database)},
                                              "worker": {"concurrency": 1}}).snapshot
        self.store.update({"logging": {"level": "DEBUG"}}, expected_revision=1)
        db = sqlite3.connect(self.database)
        initialize(db)
        with transaction(db):
            enqueue_job(db, kind="TEST", payload={}, component="intake", available_at_ms=0)
        db.close()

    def worker(self, execute):
        spec = Registration("TEST", HandlerDefinition("TEST", "Test work"), execute)
        worker = Worker(self.store, self.startup, Registry((spec,)),
                        policy=Policy(lease_ms=30000, poll_ms=10, max_retries=3, backoff_ms=1))
        self.addAsyncCleanup(worker.close)
        return worker

    async def attempt(self, worker):
        """One attempt; returns what it printed. Each attempt's logger is made
        during the attempt, so its handler writes to the patched stderr."""
        out = io.StringIO()
        with patch("sys.stderr", out):
            self.assertTrue(await worker.attempt())
        return out.getvalue()

    def query(self, sql, *args):
        db = sqlite3.connect(self.database)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    @staticmethod
    def order(log, *phrases):
        """Index of each phrase in the log, failing if one is missing."""
        return [log.index(phrase) for phrase in phrases]

    async def test_program_error_fails_once_with_trace_check_and_notification(self):
        async def handler(context):
            return {}["missing"]
        log = await self.attempt(self.worker(handler))
        self.assertEqual(self.query("SELECT state,attempt_count,last_reason_code FROM jobs"),
                         [("FAILED", 1, "JOB_HANDLER_FAILED")])
        # Not retried: no retry was scheduled.
        self.assertEqual(self.query("SELECT count(*) FROM job_history WHERE event_code='RETRY_SCHEDULED'"), [(0,)])
        # The log: ERROR, the error type and the frame where it was raised.
        self.assertIn("ERROR", log)
        self.assertIn("Traceback (most recent call last)", log)
        self.assertIn("KeyError: 'missing'", log)
        self.assertIn("test_worker_errors_and_log.py", log)
        self.assertIn("in handler", log)
        # History: file:line:function and the error type.
        details = json.loads(self.query("SELECT details_json FROM job_history WHERE event_code='FAILED'")[0][0])
        self.assertTrue(_CHECK.fullmatch(details["failed_check"]))
        self.assertIn("test_worker_errors_and_log.py:", details["failed_check"])
        self.assertTrue(details["failed_check"].endswith("handler RAISED_KEYERROR"))
        # The bell says it is a program error.
        notes = self.query("SELECT type,title,message FROM notifications")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], "JOB_FAILED")
        self.assertTrue(notes[0][1].startswith("Program error in test"))
        self.assertIn("not retried", notes[0][2])

    async def test_working_step_logs_started_then_finished(self):
        async def handler(context):
            return Outcome()
        log = await self.attempt(self.worker(handler))
        started, step_started, finished = self.order(log, "Job started", "Step started", "Step finished")
        self.assertLess(started, step_started)
        self.assertLess(step_started, finished)
        self.assertIn("DEBUG", log)

    async def test_raising_step_logs_started_then_trace_then_failed(self):
        async def handler(context):
            raise KeyError("missing")
        log = await self.attempt(self.worker(handler))
        started, trace, failed = self.order(log, "Step started", "Traceback (most recent call last)", "Step failed")
        self.assertLess(started, trace)
        self.assertLess(trace, failed)

    async def test_repeated_wait_logs_nothing(self):
        async def handler(context):
            return Outcome("defer", "JOB_LEASE_EXPIRED", delay_ms=1)
        worker = self.worker(handler)
        first = await self.attempt(worker)
        self.assertIn("Step started", first)
        await asyncio.sleep(0.01)
        self.assertEqual(await self.attempt(worker), "")


    async def retried(self, error, reason):
        """Resilience (owner, 2026-11-27): an outage never fails a job; only a bug does."""
        async def handler(context):
            raise error
        await self.attempt(self.worker(handler))
        self.assertEqual(self.query("SELECT state,last_reason_code FROM jobs"), [("RETRY_WAIT", reason)])
        self.assertEqual(self.query("SELECT count(*) FROM notifications"), [(0,)])

    async def test_connection_reset_is_retried(self):
        await self.retried(ConnectionResetError("reset by peer"), "NETWORK_UNAVAILABLE")

    async def test_timeout_is_retried(self):
        await self.retried(TimeoutError(), "NETWORK_UNAVAILABLE")

    async def test_disk_full_without_a_path_is_retried(self):
        import errno
        await self.retried(OSError(errno.ENOSPC, "No space left on device"), "STORAGE_UNAVAILABLE")

    async def test_io_error_without_a_path_is_retried(self):
        import errno
        await self.retried(OSError(errno.EIO, "Input/output error"), "STORAGE_UNAVAILABLE")

    async def test_other_os_error_is_a_program_error(self):
        import errno
        async def handler(context):
            raise OSError(errno.EBADF, "Bad file descriptor")
        await self.attempt(self.worker(handler))
        self.assertEqual(self.query("SELECT state,last_reason_code FROM jobs"), [("FAILED", "JOB_HANDLER_FAILED")])
