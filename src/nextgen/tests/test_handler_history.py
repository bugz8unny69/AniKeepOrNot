"""Handler invocations are audit records, not a replacement job model; unrun."""
import io
import logging
import sqlite3
import unittest

from src.nextgen.diagnostics import event, log_history
from src.nextgen.handlers import HandlerCatalog, HandlerDefinition
from src.nextgen.history import append_job_history
from src.nextgen.messages import MessageCatalog
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction


class HandlerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)
        self.handlers = HandlerCatalog([
            HandlerDefinition("TEST_IDENTIFY", "Identify anime"),
            HandlerDefinition("TEST_NEXT", "Prepare the next step"),
        ])
        with transaction(self.db):
            self.job = enqueue_job(self.db, kind="test", payload={}, component="worker")
            self.other = enqueue_job(self.db, kind="test", payload={}, component="worker")
            self.db.execute("UPDATE jobs SET state='RUNNING',attempt_count=1,lease_token='lease',lease_expires_at_ms=9999")

    def tearDown(self):
        self.db.close()

    def append(self, code, *, invocation="a" * 32, handler="TEST_IDENTIFY",
               job=None, attempt=1, when=10, reason=None, handlers=None, component="worker"):
        return append_job_history(self.db, job_id=self.job if job is None else job,
            event_code=code, component=component, occurred_at_ms=when,
            reason_code=reason, details={"attempt": attempt}, handler_code=handler,
            invocation_id=invocation, handlers=self.handlers if handlers is None else handlers)

    def test_multiple_handlers_and_repeated_calls_are_visible_under_one_job(self):
        receipts = []
        with transaction(self.db):
            for identifier, handler in (("a" * 32, "TEST_IDENTIFY"), ("b" * 32, "TEST_NEXT"),
                                        ("c" * 32, "TEST_IDENTIFY")):
                receipts.append(self.append("HANDLER_STARTED", invocation=identifier, handler=handler))
                receipts.append(self.append("HANDLER_COMPLETED", invocation=identifier, handler=handler, when=11))
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE job_id=?", (self.job,)).fetchone(), ("RUNNING",))
        self.assertEqual(self.db.execute("SELECT handler_code,handler_label,invocation_id FROM job_history "
            "WHERE job_id=? AND event_code='HANDLER_STARTED' ORDER BY history_id", (self.job,)).fetchall(),
            [("TEST_IDENTIFY", "Identify anime", "a"*32), ("TEST_NEXT", "Prepare the next step", "b"*32),
             ("TEST_IDENTIFY", "Identify anime", "c"*32)])
        self.assertTrue(all(record.job_id == self.job for record in receipts))

    def test_failures_have_registered_reasons_and_do_not_decide_retry_policy(self):
        with transaction(self.db):
            self.append("HANDLER_STARTED")
            failed = self.append("HANDLER_FAILED", when=12, reason="SERVICE_UNAVAILABLE")
        self.assertEqual(failed.reason_code, "SERVICE_UNAVAILABLE")
        self.assertEqual(self.db.execute("SELECT state,attempt_count FROM jobs WHERE job_id=?", (self.job,)).fetchone(), ("RUNNING",1))
        with self.assertRaises(ValueError), transaction(self.db):
            self.append("HANDLER_FAILED", invocation="b"*32, reason=None)

    def test_missing_start_wrong_job_handler_attempt_or_time_is_rejected(self):
        with self.assertRaises(ValueError), transaction(self.db):
            self.append("HANDLER_COMPLETED")
        with transaction(self.db):
            self.append("HANDLER_STARTED")
        for overrides in ({"job": self.other}, {"handler": "TEST_NEXT"}, {"attempt": 2}, {"when": 9}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError), transaction(self.db):
                self.append("HANDLER_COMPLETED", **overrides)
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET attempt_count=2 WHERE job_id=?", (self.job,))
        with self.assertRaises(ValueError), transaction(self.db):
            self.append("HANDLER_COMPLETED", attempt=2)

    def test_double_start_or_double_end_and_reuse_across_jobs_are_rejected(self):
        with transaction(self.db):
            self.append("HANDLER_STARTED")
        with self.assertRaises(ValueError), transaction(self.db):
            self.append("HANDLER_STARTED")
        with self.assertRaises(ValueError), transaction(self.db):
            self.append("HANDLER_STARTED", job=self.other)
        with transaction(self.db):
            self.append("HANDLER_COMPLETED", when=11)
        with self.assertRaises(ValueError), transaction(self.db):
            self.append("HANDLER_FAILED", when=12, reason="JOB_HANDLER_FAILED")

    def test_metadata_requires_registered_handler_worker_running_state_and_valid_id(self):
        for overrides in ({"handler": "UNREGISTERED"}, {"invocation": None}, {"invocation": "bad/token"},
                          {"invocation": "a"*33}, {"handler": []}, {"component": "api"}, {"attempt": 0}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError), transaction(self.db):
                self.append("HANDLER_STARTED", **overrides)
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET state='PENDING' WHERE job_id=?", (self.job,))
        with self.assertRaises(ValueError), transaction(self.db):
            self.append("HANDLER_STARTED")
        with self.assertRaises(ValueError), transaction(self.db):
            self.append("CLAIMED")  # handler metadata on a non-handler event

    def test_history_and_caller_work_roll_back_together_and_no_writer_commit(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.db):
                self.append("HANDLER_STARTED")
                self.db.execute("UPDATE jobs SET updated_at_ms=123 WHERE job_id=?", (self.job,))
                self.assertTrue(self.db.in_transaction)
                raise RuntimeError("rollback")
        self.assertEqual(self.db.execute("SELECT count(*) FROM job_history WHERE invocation_id IS NOT NULL").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.append("HANDLER_STARTED")

    def test_labels_survive_registry_change_and_logs_have_readable_handler_copy(self):
        with transaction(self.db):
            self.append("HANDLER_STARTED")
        changed = HandlerCatalog([HandlerDefinition("TEST_IDENTIFY", "Updated label")])
        with transaction(self.db):
            receipt = self.append("HANDLER_COMPLETED", when=11, handlers=changed)
        self.assertEqual(receipt.handler_label, "Identify anime")
        logger = logging.Logger("test.handler")
        stream = io.StringIO()
        logger.addHandler(logging.StreamHandler(stream))
        log_history(logger, receipt)
        text = stream.getvalue()
        for value in ("Identify anime", "Step finished"):        # activity, then what happened
            self.assertIn(value, text)
        for value in ("HANDLER_COMPLETED", "TEST_IDENTIFY", "a"*32):  # codes and invocation ids stay in History
            self.assertNotIn(value, text)
        with self.assertRaises(ValueError):
            event(logger, logging.INFO, "HANDLER_STARTED", handler_code="TEST_IDENTIFY", handler_label="unsafe\nlabel", invocation_id="a"*32)

    def test_handler_translations_and_removed_handler_fallback(self):
        translations = {"fr": {"handler.TEST_IDENTIFY.label": "Identifier un anime"}}
        catalog = MessageCatalog(translations, handlers=self.handlers)
        translations["fr"]["handler.TEST_IDENTIFY.label"] = "changed"
        self.assertEqual(catalog.handler("TEST_IDENTIFY", locale="fr")["message"], "Identifier un anime")
        self.assertEqual(catalog.handler("TEST_IDENTIFY", locale="missing")["message"], "Identify anime")
        self.assertEqual(MessageCatalog().handler("TEST_IDENTIFY", recorded_label="Identify anime")["message"], "Identify anime")
        self.assertEqual(MessageCatalog().handler("bad/token", recorded_label="unsafe\nlabel")["message"], "Unknown job handler")
        with self.assertRaises(ValueError):
            HandlerCatalog([HandlerDefinition("TEST", "A"), HandlerDefinition("TEST", "B")])
        for label in ("", "x"*129, "line\nbreak", "{payload}", "<script>"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                HandlerDefinition("TEST", label)
