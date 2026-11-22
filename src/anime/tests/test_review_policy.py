"""Only a torrent that cannot be identified stops in Needs review; everything else is automatic."""
import json
import unittest

from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import Outcome, Registration, Registry
from src.anime.storage import review_policy
from src.anime.tests import test_async_worker as base
from src.anime.worker import Worker


class ReviewPolicyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.AsyncWorkerTests.asyncSetUp

    def row(self, sql, args=()):
        import sqlite3
        db = sqlite3.connect(self.database)
        try: return db.execute(sql, args).fetchone()
        finally: db.close()

    async def run_kind(self, kind, outcome, body=b'{"release_name":"[Group] Show - 02","torrent_infohash":"' + b'a'*40 + b'"}'):
        import sqlite3
        from src.anime.storage.sqlite import accept_event_and_job
        db = sqlite3.connect(self.database)
        try: _, job = accept_event_and_job(db, 'test', body, kind=kind, payload={})
        finally: db.close()
        async def handler(context): return outcome
        spec = Registration(kind, HandlerDefinition(kind, "Test step"), handler)
        worker = Worker(self.store, self.startup, Registry((spec,)), policy=self.policy)
        self.addAsyncCleanup(worker.close)
        await worker.attempt()
        return job

    def test_every_kind_is_routed(self):
        self.assertEqual(review_policy.route("IDENTIFY_ANIME"), "review")
        self.assertEqual(review_policy.route("CLEANUP_DECIDED_DOWNLOAD"), "retry")
        self.assertEqual(review_policy.route("VERIFY_RELEASE"), "cancel")

    async def test_identification_is_the_only_needs_review(self):
        job = await self.run_kind("IDENTIFY_ANIME", Outcome("review", "IDENTITY_INCOMPLETE"))
        self.assertEqual(self.row("SELECT state,last_reason_code FROM jobs WHERE job_id=?", (job,)),
                         ("NEEDS_REVIEW", "IDENTITY_INCOMPLETE"))

    async def test_a_torrent_a_step_cannot_use_is_canceled_with_a_notification(self):
        job = await self.run_kind("VERIFY_RELEASE", Outcome("review", "RELEASE_TITLE_UNCLEAR"))
        self.assertEqual(self.row("SELECT state,last_reason_code FROM jobs WHERE job_id=?", (job,)),
                         ("CANCELED", "RELEASE_TITLE_UNCLEAR"))
        self.assertEqual(self.row("SELECT o.state,o.cleanup_state FROM job_operations o JOIN job_operation_members m "
                                  "USING(operation_id) WHERE m.job_id=?", (job,)), ("CANCELED", "COMPLETE"))
        kind, title, message = self.row("SELECT type,title,message FROM notifications WHERE job_id=?", (job,))
        self.assertEqual((kind, title), ("RELEASE_UNSUPPORTED", "Canceled: [Group] Show - 02"))
        self.assertIn("next torrent for this episode is tried", message)
        self.assertIsNone(self.row("SELECT 1 FROM jobs WHERE state='NEEDS_REVIEW'"))

    async def test_retries_exhausted_on_a_torrent_cancel_with_the_steps_own_reason(self):
        from src.anime.job_contracts import Policy
        self.policy = Policy(lease_ms=30000, poll_ms=10, max_retries=0)
        job = await self.run_kind("PREPARE_ACQUISITION", Outcome("retry", "TORRENT_METADATA_TIMEOUT"))
        self.assertEqual(self.row("SELECT state,last_reason_code FROM jobs WHERE job_id=?", (job,)),
                         ("CANCELED", "TORRENT_METADATA_TIMEOUT"))

    async def test_housekeeping_retries_within_its_budget_then_fails_as_a_bug(self):
        # Nothing stays in Needs review, and nothing retries forever.
        job = await self.run_kind("CLEANUP_CANCELED_OPERATION", Outcome("review", "CANCELLATION_REVIEW"))
        self.assertEqual(self.row("SELECT state,last_reason_code FROM jobs WHERE job_id=?", (job,)),
                         ("RETRY_WAIT", "CANCELLATION_REVIEW"))
        from src.anime.job_contracts import Policy
        self.policy = Policy(lease_ms=30000, poll_ms=10, max_retries=0)
        job = await self.run_kind("CLEANUP_CANCELED_OPERATION", Outcome("review", "CANCELLATION_REVIEW"))
        self.assertEqual(self.row("SELECT state,last_reason_code FROM jobs WHERE job_id=?", (job,)),
                         ("FAILED", "CANCELLATION_REVIEW"))
        kind, title = self.row("SELECT type,title FROM notifications WHERE job_id=?", (job,))
        self.assertEqual((kind, title), ("JOB_FAILED", "Could not finish cleanup canceled operation: [Group] Show - 02"))
        self.assertIsNone(self.row("SELECT 1 FROM jobs WHERE state='NEEDS_REVIEW'"))

if __name__ == '__main__':
    unittest.main()
