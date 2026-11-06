"""Offline settings and diagnostics tests; no CI or live components."""
import io
import logging
import tempfile
import unittest
from pathlib import Path

from src.nextgen.settings import Settings, parse_settings, load_settings
from src.nextgen.diagnostics import TRACE, SafeFormatter, configure_logging, event, exception, Span

class FoundationTests(unittest.TestCase):
    def logger(self):
        stream = io.StringIO()
        logger = logging.Logger("nextgen.test")
        logger.addHandler(logging.StreamHandler(stream))
        return logger, stream

    def test_defaults_and_override(self):
        config = parse_settings({"logging": {"level": "INFO",
                                 "components": {"worker": "TRACE"}}})
        self.assertEqual(config.logging.level_for("worker"), "TRACE")
        self.assertEqual(config.logging.level_for("intake"), "INFO")
        self.assertEqual(Settings().logging.level, "INFO")

    def test_reject_unrecognized_settings(self):
        for config in ({"logging": {"level": "verbose"}},
                       {"logging": {"components": {"unknown": "TRACE"}}},
                       {"logging": {"components": {"intake": "oops"}}},
                       {"logging": {"quietly_disable_redaction": True}},
                       {"unexpected": {}}, [], {"logging": []}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                parse_settings(config)

    def test_settings_file_and_reload_level(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "settings.json"
            file.write_text('{"logging": {"components": {"worker": "TRACE"}}}')
            config = load_settings(file)
            logger, stream = self.logger()
            configure_logging(config, "worker", logger=logger)
            self.assertEqual(logger.level, TRACE)
            initial_handlers = len(logger.handlers)
            file.write_text('{"logging": {"level": "WARNING"}}')
            configure_logging(load_settings(file), "worker", logger=logger)
            self.assertEqual(logger.level, logging.WARNING)
            self.assertEqual(len(logger.handlers), initial_handlers)

    def test_redaction_at_trace_and_exception(self):
        logger, stream = self.logger()
        configure_logging(parse_settings({"logging": {"level": "TRACE"}}),
                          "worker", logger=logger)
        logger.log(TRACE, "Bearer secretBearer password=hidden api_key=apikey123 "
                          "https://alice:pass@example.org/?token=querysecret")
        try:
            raise RuntimeError("authorization: abc123")
        except RuntimeError as exc:
            exception(logger, "OPERATION_FAILED", exc, job_id=42,
                      event_id=2, retryable=False, reason_code="JOB_HANDLER_FAILED")
        content = stream.getvalue()
        for secret in ("secretBearer", "hidden", "apikey123", "alice:pass",
                       "querysecret", "abc123"):
            self.assertNotIn(secret, content)
        self.assertIn("event_code=OPERATION_FAILED", content)
        self.assertIn("job_id=42", content)
        self.assertIn("trace_id=AD-2", content)
        self.assertIn("error=RuntimeError: authorization=[REDACTED] at nextgen/tests/test_settings_diagnostics.py:", content)

    def test_metadata_allowlist(self):
        logger, stream = self.logger()
        logger.setLevel(logging.INFO)
        logger.handlers[0].setFormatter(SafeFormatter("%(message)s"))
        event(logger, logging.INFO, "RETRY_SCHEDULED", job_id=42, attempt=2,
              reason_code="SERVICE_UNAVAILABLE",
              retryable=True, raw_body="never log this")
        self.assertIn("job_id=42", stream.getvalue())
        self.assertNotIn("never log this", stream.getvalue())

    def test_span(self):
        self.assertGreaterEqual(Span().milliseconds(), 0)

if __name__ == "__main__":
    unittest.main()

