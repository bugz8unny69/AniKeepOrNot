"""Transactional diagnostic cases only; no worker or live integrations."""
import io
import logging
import sqlite3
import unittest
from unittest.mock import patch

from src.anime.diagnostics import configure_logging, log_history
from src.anime.history import append_event_history, append_job_history, job_attempt, encode_details, trace_id
from src.anime.settings import parse_settings
from src.anime.storage.sqlite import (
    initialize, accept_event, accept_event_and_job, enqueue_job, save_identity, transaction,
)


class DiagnosticHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def test_intake_has_two_atomic_audits_and_independent_deliveries(self):
        for _ in range(2):
            event, job = accept_event_and_job(self.db, "test", b"private",
                                            kind="identity", payload={})
            self.assertEqual(self.db.execute(
                "SELECT event_code,component FROM event_history WHERE event_id=?", (event,)
            ).fetchall(), [("ACCEPTED", "intake")])
            self.assertEqual(self.db.execute(
                "SELECT event_code,component,details_json FROM job_history WHERE job_id=?", (job,)
            ).fetchall(), [("CREATED", "intake", '{"attempt":0}')])
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)

    def test_control_audit_observes_target_attempt_and_keeps_explicit_validation(self):
        _,target=accept_event_and_job(self.db,'test',b'private',kind='identity',payload={})
        with transaction(self.db):
            self.db.execute('UPDATE jobs SET attempt_count=3 WHERE job_id=?',(target,))
            record=append_job_history(self.db,job_id=target,event_code='MANAGEMENT_ACTION_RESUMED',
                component='api',occurred_at_ms=2,details={'attempt':job_attempt(self.db,target)})
            self.assertEqual(record.details_json,'{"attempt":3}')
            with self.assertRaises(ValueError):
                append_job_history(self.db,job_id=target,event_code='MANAGEMENT_ACTION_RESUMED',
                    component='api',occurred_at_ms=2,details={'attempt':1})
        with self.assertRaises(ValueError):job_attempt(self.db,target)

    def test_writer_refuses_missing_transaction_and_leaves_no_partial_job(self):
        with self.assertRaises(ValueError):
            enqueue_job(self.db, component="intake", kind="identity", payload={})
        with self.assertRaises(ValueError):
            append_event_history(self.db, event_id=1, event_code="ACCEPTED",
                                 component="intake", occurred_at_ms=1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_no_false_lifecycle_log_on_rollback_or_failed_audit(self):
        with patch("src.anime.storage.sqlite.append_job_history", side_effect=ValueError("fault")), \
                patch("src.anime.storage.sqlite._log") as log:
            with self.assertRaises(ValueError):
                accept_event_and_job(self.db, "test", b"private", kind="identity", payload={})
            log.assert_not_called()
        for table in ("jobs", "incoming_events", "job_history", "event_history"):
            self.assertEqual(self.db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)

    def test_atomic_successor_done_and_histories_rollback_together(self):
        event, job = accept_event_and_job(self.db, "test", b"private", kind="identity", payload={})
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET state='RUNNING',attempt_count=1,"
                            "lease_token='test-only',lease_expires_at_ms=100 WHERE job_id=?", (job,))
        with self.assertRaises(RuntimeError):
            with transaction(self.db):
                self.db.execute("UPDATE jobs SET state='DONE',lease_token=NULL,"
                                "lease_expires_at_ms=NULL WHERE job_id=?", (job,))
                append_job_history(self.db, job_id=job, event_code="DONE",
                                   component="worker", occurred_at_ms=2, details={"attempt": 1})
                enqueue_job(self.db, component="identity", kind="next", payload={},
                            event_id=event, created_by_job_id=job)
                raise RuntimeError("fault after all mutations")
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE job_id=?", (job,)).fetchone()[0],
                         "RUNNING")
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT event_code FROM job_history").fetchall(), [("CREATED",)])

    def test_completion_and_successor_commit_with_distinct_audits(self):
        event, first = accept_event_and_job(self.db, "test", b"x", kind="identity", payload={})
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET state='DONE',attempt_count=1 WHERE job_id=?", (first,))
            completion = append_job_history(
                self.db, job_id=first, event_code="DONE", component="worker",
                occurred_at_ms=3, details={"attempt": 1})
            second = enqueue_job(self.db, component="identity", kind="next", payload={},
                                 event_id=event, created_by_job_id=first)
        self.assertEqual(completion.job_id, first)
        self.assertEqual(self.db.execute(
            "SELECT job_id,event_code FROM job_history ORDER BY history_id").fetchall(),
            [(first, "CREATED"), (first, "DONE"), (second, "CREATED")])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_sql_audit_failure_rolls_back_delivery_job_and_both_histories(self):
        self.db.executescript("""
            CREATE TRIGGER fail_audit BEFORE INSERT ON job_history
            BEGIN SELECT RAISE(ABORT,'injected audit failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            accept_event_and_job(self.db, "test", b"x", kind="identity", payload={})
        self.assertFalse(self.db.in_transaction)
        for table in ("jobs", "incoming_events", "job_history", "event_history"):
            self.assertEqual(self.db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)

    def test_high_level_helpers_reject_nested_transaction_ownership(self):
        with transaction(self.db):
            with self.assertRaises(ValueError):
                accept_event(self.db, "test", b"x")
            self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute("SELECT count(*) FROM incoming_events").fetchone()[0], 0)

    def test_retry_keeps_correlation_attempt_and_provenance(self):
        event, original = accept_event_and_job(self.db, "test", b"x", kind="identity", payload={})
        with transaction(self.db):
            job = enqueue_job(self.db, component="identity", kind="next", payload={},
                              event_id=event, created_by_job_id=original)
            self.db.execute("UPDATE jobs SET state='RETRY_WAIT',attempt_count=2 WHERE job_id=?", (job,))
            receipt = append_job_history(
                self.db, job_id=job, component="worker", occurred_at_ms=10,
                event_code="RETRY_SCHEDULED", reason_code="SERVICE_UNAVAILABLE",
                details={"attempt": 2, "retry_at_ms": 20})
        self.assertEqual((receipt.event_id, receipt.job_id, receipt.created_by_job_id),
                         (event, job, original))
        self.assertEqual(trace_id(event_id=event, job_id=job), f"AD-{event}")
        self.assertEqual(trace_id(job_id=job), f"JOB-{job}")

    def test_no_event_job_and_critical_filter_still_preserve_history(self):
        logger = logging.Logger("history-test")
        stream = io.StringIO()
        logger.addHandler(logging.StreamHandler(stream))
        configure_logging(parse_settings({"logging": {"level": "CRITICAL"}}), "worker", logger=logger)
        with transaction(self.db):
            job = enqueue_job(self.db, component="worker", kind="housekeeping", payload={})
            self.db.execute("UPDATE jobs SET attempt_count=1 WHERE job_id=?", (job,))
            receipt = append_job_history(self.db, job_id=job, event_code="CLAIMED",
                                         component="worker", occurred_at_ms=1, details={"attempt": 1})
        self.assertFalse(log_history(logger, receipt))
        self.assertEqual(stream.getvalue(), "")
        self.assertIsNone(receipt.event_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM job_history").fetchone()[0], 2)

    def test_history_rejects_payloads_mismatched_attempts_and_wrong_scope(self):
        _, job = accept_event_and_job(self.db, "test", b"x", kind="identity", payload={})
        invalid = [
            {"event_code": "ACCEPTED"}, {"event_code": "NEW_CODE"},
            {"event_code": "CLAIMED", "details": {"attempt": 1}},
            {"event_code": "CREATED", "details": {"raw_body": "token=secret"}},
            {"event_code": "CREATED", "component": "unknown"},
            {"event_code": "CREATED", "occurred_at_ms": True},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                with transaction(self.db):
                    values = dict(job_id=job, event_code="CREATED", component="intake",
                                  occurred_at_ms=1)
                    values.update(overrides)
                    append_job_history(self.db, **values)
        self.assertEqual(self.db.execute("SELECT count(*) FROM job_history").fetchone()[0], 1)
        with patch("src.anime.history.MAX_DETAILS_BYTES", 1), self.assertRaises(ValueError):
            encode_details({"attempt": 0})

    def test_audit_failure_rolls_back_identity_and_idempotent_retry_adds_no_history(self):
        event = accept_event(self.db, "test", b"x")
        candidate = {"schema_version": 2, "anime": {
            "anidb_id": 1, "tvdb_series_id": 2, "title": "Test"}, "episodes": []}
        with patch("src.anime.storage.sqlite.append_event_history", side_effect=ValueError("fault")):
            with self.assertRaises(ValueError):
                save_identity(self.db, event, candidate)
        self.assertEqual(self.db.execute("SELECT count(*) FROM identity_results").fetchone()[0], 0)
        self.assertEqual(save_identity(self.db, event, candidate), save_identity(self.db, event, candidate))
        self.assertEqual(self.db.execute("SELECT event_code FROM event_history ORDER BY history_id").fetchall(),
                         [("ACCEPTED",), ("SERIES_IDENTIFIED",)])

    def test_explicit_transaction_supports_autocommit_connections(self):
        self.db.close()
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)
        with patch("src.anime.storage.sqlite.append_job_history", side_effect=ValueError("fault")):
            with self.assertRaises(ValueError):
                accept_event_and_job(self.db, "test", b"x", kind="identity", payload={})
        self.assertEqual(self.db.execute("SELECT count(*) FROM incoming_events").fetchone()[0], 0)

    def test_blocking_job_id_and_operation_id_in_history_details(self):
        """W-22/W-25: Reservation blockers can be recorded in history details."""
        import json
        raw = encode_details({"blocking_job_id": 42, "operation_id": 7, "attempt": 1})
        parsed = json.loads(raw)
        self.assertEqual(parsed["blocking_job_id"], 42)
        self.assertEqual(parsed["operation_id"], 7)

    def test_failure_facts_extraction(self):
        """W-24: failure_facts extracts location-qualified checks matching _CHECK."""
        from src.anime.job_contracts import failure_facts
        from src.anime.reasons import _CHECK
        try:
            raise ValueError("custom error")
        except ValueError as exc:
            facts = failure_facts(exc)
            self.assertIsNotNone(facts)
            self.assertIn("failed_check", facts)
            self.assertTrue(_CHECK.fullmatch(facts["failed_check"]))
            self.assertTrue(facts["failed_check"].endswith("VALUEERROR"))

