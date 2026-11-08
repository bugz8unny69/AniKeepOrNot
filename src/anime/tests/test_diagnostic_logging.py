"""Readable logging, safe exceptions and reload cases; deliberately unrun."""
import io
import logging
import unittest

from src.anime.diagnostics import (
    TRACE, SafeFormatter, configure_logging, event, exception, redact,
)
from src.anime.messages import EVENTS, DEFAULT_CATALOG
from src.anime.settings import LoggingSettings, Settings, parse_settings
from src.anime.storage.sqlite import accept_event_and_job, initialize
import sqlite3


class DiagnosticLoggingTests(unittest.TestCase):
    def make_logger(self, level="TRACE"):
        stream = io.StringIO()
        logger = logging.Logger("anime.test")
        logger.addHandler(logging.StreamHandler(stream))
        configure_logging(parse_settings({"logging": {"level": level}}), "worker", logger=logger)
        return logger, stream

    def test_each_marker_has_readable_copy_and_machine_code(self):
        logger, stream = self.make_logger()
        for name, definition in EVENTS.items():
            reason = definition.exact_reason or ("JOB_HANDLER_FAILED" if definition.reason_required else None)
            fields=({'handler_code':'TEST_HANDLER','handler_label':'Test handler','invocation_id':'a'*32}
                    if name in ('HANDLER_STARTED','HANDLER_COMPLETED','HANDLER_FAILED') else {})
            event(logger, logging.INFO, name, reason_code=reason,**fields)
            # The line uses the catalog's words (codes stay in History). Check
            # lines show the check itself instead of their own sentence.
            if name not in ("CHECK_PASSED", "CHECK_FAILED"):
                self.assertIn(DEFAULT_CATALOG.event(name)["message"].rstrip("."), stream.getvalue())
            if reason:
                self.assertIn(DEFAULT_CATALOG.reason(reason)["message"].rstrip("."), stream.getvalue())
            self.assertNotIn("event_code=", stream.getvalue())

    def test_correlation_is_derived_and_untrusted_fields_are_dropped(self):
        logger, stream = self.make_logger()
        event(logger, TRACE, "RETRY_SCHEDULED", reason_code="SERVICE_UNAVAILABLE",
              event_id=4, job_id=8, created_by_job_id=3, attempt=2,
              trace_id="arbitrary-secret", raw_body="private-body", error_type="private-class",
              component="worker", retryable=True, outcome="RETRY_WAIT")
        output = stream.getvalue()
        for text in ("job #8 ·", "attempt 2", "will retry", "ended RETRY_WAIT"):
            self.assertIn(text, output)
        # One job, one name: no event, creator, trace or component fields (JD-1d).
        for text in ("event_id", "created_by", "trace_id", "component"):
            self.assertNotIn(text, output)
        for text in ("arbitrary-secret", "private-body", "private-class"):
            self.assertNotIn(text, output)
        event(logger, TRACE, "CREATED", job_id=9, event_id=True)
        self.assertIn("job #9 · Job created", stream.getvalue())
        self.assertNotIn("True", stream.getvalue())
        event(logger, TRACE, "ACCEPTED", event_id=5)
        self.assertIn("release #5 · Release received", stream.getvalue())

    def test_exhaustion_retains_a_readable_underlying_cause(self):
        logger, stream = self.make_logger()
        event(logger, logging.ERROR, "NEEDS_REVIEW", reason_code="JOB_RETRY_EXHAUSTED",
              cause_reason_code="SERVICE_UNAVAILABLE", job_id=8)
        self.assertIn("Automatic retries stopped", stream.getvalue())
        self.assertIn("because: Waiting for a service to come back", stream.getvalue())
        with self.assertRaises(ValueError):
            event(logger, logging.ERROR, "NEEDS_REVIEW", reason_code="JOB_RETRY_EXHAUSTED",
                  cause_reason_code="unregistered")

    def test_unknown_emission_rejected_even_when_disabled(self):
        logger, _ = self.make_logger("CRITICAL")
        with self.assertRaises(ValueError):
            event(logger, TRACE, "UNREGISTERED_MARKER")
        with self.assertRaises(ValueError):
            event(logger, TRACE, "RETRY_SCHEDULED", reason_code="UNKNOWN_REASON")

    def test_exceptions_never_reach_handlers_as_raw_exc_info(self):
        # Deliberately use an unconfigured formatter too: exception() itself
        # must be safe before a handler gets a record.
        class Capture(logging.Handler):
            def emit(self, record):
                self.record = record
        capture = Capture()
        logger = logging.Logger("capture")
        logger.setLevel(TRACE)
        logger.addHandler(capture)
        try:
            raise ValueError("token='arbitrary secret with spaces'")
        except ValueError as failure:
            exception(logger, "OPERATION_FAILED", failure,
                      reason_code="JOB_HANDLER_FAILED", job_id=1)
        self.assertIsNone(capture.record.exc_info)
        content = capture.record.getMessage()
        # The exact error is shown; named credentials are still redacted.
        self.assertIn("error: ValueError: token=[REDACTED] at anime/tests/test_diagnostic_logging.py:", content)
        self.assertNotIn("arbitrary secret", content)

    def test_safe_exception_at_all_supported_levels_including_custom_names(self):
        malicious_type = type("private_secret_class_name", (Exception,), {})
        for level in (TRACE, logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
            logger, stream = self.make_logger()
            try:
                raise malicious_type("entire provider body containing an unknown credential")
            except Exception as failure:
                exception(logger, "OPERATION_FAILED", failure, level=level,
                          reason_code="JOB_HANDLER_FAILED")
            content = stream.getvalue()
            self.assertIn("error: private_secret_class_name: entire provider body", content)
            self.assertLess(len(content), 4096)

    def test_formatter_discards_cached_exception_text_and_raw_stack(self):
        record = logging.LogRecord("safe", logging.ERROR, __file__, 1, "Static message.", (), None)
        record.exc_text = "private_cached_exception"
        record.stack_info = "private_stack_source"
        rendered = SafeFormatter("%(message)s").format(record)
        self.assertNotIn("private_cached_exception", rendered)
        self.assertNotIn("private_stack_source", rendered)
        self.assertEqual(record.exc_text, "private_cached_exception")
        try:
            raise RuntimeError("unknown_secret_value")
        except RuntimeError as failure:
            record.exc_info = (type(failure), failure, failure.__traceback__)
            self.assertNotIn("unknown_secret_value", SafeFormatter("%(message)s").format(record))

    def test_redaction_defense_handles_known_quoted_and_query_values(self):
        content = redact('password="two words" secret=\'three word value\' '
                         'https://example.invalid/?access_token=query_private&apikey=key_private '
                         'x-emby-token=emby_private signature=signed_private')
        for secret in ("two words", "three word value", "query_private", "key_private",
                       "emby_private", "signed_private"):
            self.assertNotIn(secret, content)

    def test_reload_changes_threshold_without_mutable_settings_or_new_handlers(self):
        logger, stream = self.make_logger("CRITICAL")
        overrides = {"worker": "TRACE"}
        settings = Settings(logging=LoggingSettings(components=overrides))
        overrides["worker"] = "CRITICAL"
        configure_logging(settings, "worker", logger=logger)
        event(logger, TRACE, "CREATED", job_id=1)
        self.assertIn("Job created", stream.getvalue())
        self.assertEqual(len(logger.handlers), 1)
        with self.assertRaises(TypeError):
            settings.logging.components["worker"] = "INFO"
        previous = (logger.level, logger.handlers[0].level, logger.handlers[0].formatter)
        with self.assertRaises(ValueError):
            configure_logging({"logging": "bad"}, "worker", logger=logger)
        self.assertEqual(previous, (logger.level, logger.handlers[0].level, logger.handlers[0].formatter))

    def test_failed_sink_cannot_turn_committed_work_into_failure(self):
        class Broken(logging.Handler):
            def emit(self, record):
                raise OSError("sink failure")
        logger = logging.getLogger("anime.intake")
        old = (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        db = sqlite3.connect(":memory:")
        try:
            logger.handlers = [Broken()]
            logger.setLevel(TRACE)
            logger.propagate = False
            logger.disabled = False
            initialize(db)
            event_id, job_id = accept_event_and_job(db, "test", b"x", kind="identity", payload={})
            self.assertGreater(event_id, 0)
            self.assertGreater(job_id, 0)
            self.assertEqual(db.execute("SELECT count(*) FROM job_history").fetchone()[0], 1)
            self.assertFalse(db.in_transaction)
        finally:
            db.close()
            logger.handlers, logger.level, logger.propagate, logger.disabled = old
