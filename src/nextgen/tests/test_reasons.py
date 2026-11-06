"""Offline reason registry tests; intentionally unexecuted during development."""
from dataclasses import FrozenInstanceError
import json
import re
import unittest

from src.nextgen.reasons import REGISTRY, display_reason, get_reason, safe_details


class ReasonTests(unittest.TestCase):
    def test_catalog_is_consistent_and_immutable(self):
        self.assertTrue({"IDENTITY_INCOMPLETE", "IDENTITY_INVALID", "IDENTITY_CONFLICT",
                         "PROVIDER_UNAVAILABLE", "JOB_LEASE_EXPIRED", "JOB_HANDLER_FAILED",
                         "JOB_RETRY_EXHAUSTED", "STORAGE_UNAVAILABLE", "UNSUPPORTED_JOB_KIND",
                         "CONFIGURATION_INVALID", "CONFIGURATION_CONFLICT",
                         "CONFIGURATION_MIGRATION_REQUIRED", "CONFIGURATION_STORAGE_FAILED",
                         "CONFIGURATION_DURABILITY_UNCONFIRMED", "MAINTENANCE_PENDING",
                         "MAINTENANCE_BUSY", "MAINTENANCE_FAILED",
                         "DATABASE_VERSION_UNSUPPORTED", "DATABASE_SCHEMA_INVALID",
                         "DATABASE_MIGRATION_CONFLICT", "DATABASE_MIGRATION_FAILED",
                         "JOB_STATE_INVALID"} <= set(REGISTRY))
        for key, reason in REGISTRY.items():
            with self.subTest(code=key):
                self.assertEqual(key, reason.code)
                self.assertRegex(key, re.compile(r"^[A-Z][A-Z0-9_]{0,63}$"))
                self.assertIn(reason.severity, {"WARNING", "ERROR", "INFO", "CRITICAL"})
                self.assertTrue(reason.description and reason.message and reason.action)
                self.assertTrue(reason.retryable is None or type(reason.retryable) is bool)
                self.assertEqual(display_reason(key)["message"], reason.message)
        with self.assertRaises(TypeError):
            REGISTRY["NEW"] = REGISTRY["IDENTITY_INVALID"]
        with self.assertRaises(FrozenInstanceError):
            REGISTRY["IDENTITY_INVALID"].message = "changed"

    def test_conflict_errors_carry_registered_codes(self):
        from src.nextgen.storage.batch_candidates import BatchCandidateConflict as BatchConflict
        from src.nextgen.storage.sonarr_candidates import CandidateConflict as SonarrConflict
        from src.nextgen.storage.sonarr_selection import SelectionConflict
        for error in (BatchConflict, SonarrConflict, SelectionConflict):
            with self.subTest(error=error.__module__):
                self.assertIsNotNone(get_reason(error.reason_code))

    def test_retry_classification_is_not_a_decision(self):
        for code in ("IDENTITY_INCOMPLETE", "JOB_HANDLER_FAILED", "STORAGE_UNAVAILABLE"):
            self.assertIsNone(get_reason(code).retryable)
        self.assertIs(get_reason("PROVIDER_UNAVAILABLE").retryable, True)
        self.assertIs(get_reason("JOB_RETRY_EXHAUSTED").retryable, False)
        self.assertNotIn("safely retry", get_reason("JOB_LEASE_EXPIRED").action)

    def test_unknown_historical_code_has_generic_copy(self):
        view = display_reason("FUTURE_CODE")
        self.assertEqual(view["code"], "FUTURE_CODE")
        self.assertIsNone(view["retryable"])
        self.assertEqual(view["message"], "Additional processing information is available.")
        self.assertIsNone(get_reason("FUTURE_CODE"))

    def test_malformed_unknown_codes_are_not_echoed(self):
        for code in (None, 42, [], {}, "", "A" * 65, "bad-code",
                     "TOKEN=private", "CODE\npassword=private", "<script>", "未知"):
            with self.subTest(code=code):
                self.assertEqual(display_reason(code)["code"], "UNKNOWN")
                self.assertIsNone(get_reason(code))

    def test_safe_details_are_deterministic_and_detached(self):
        missing = ["anime.title", "anime.anidb_id", "anime.title"]
        result = safe_details(missing_fields=missing,
                              invalid_fields=["schema_version"],
                              retry_at_ms=1234, attempt=2, max_attempts=3,
                              cause_reason_code="PROVIDER_UNAVAILABLE")
        missing.clear()
        self.assertEqual(result, {
            "missing_fields": ["anime.anidb_id", "anime.title"],
            "invalid_fields": ["schema_version"], "retry_at_ms": 1234,
            "attempt": 2, "max_attempts": 3,
            "cause_reason_code": "PROVIDER_UNAVAILABLE",
        })
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertEqual(safe_details(), {})

    def test_details_reject_unbounded_or_untyped_values(self):
        for key in ("missing_fields", "invalid_fields"):
            for value in ("anime.title", None, {}, ["password"], [True],
                          ["anime.title"] * 21):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    safe_details(**{key: value})
        for key in ("retry_at_ms", "attempt", "max_attempts"):
            for value in (True, False, -1, 1.5, "1", 1 << 63, float("inf")):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    safe_details(**{key: value})
            self.assertEqual(safe_details(**{key: 0}), {key: 0})
            self.assertEqual(safe_details(**{key: (1 << 63) - 1}),
                             {key: (1 << 63) - 1})
        for value in ("FUTURE_CODE", "password=private", [], True):
            with self.subTest(cause=value), self.assertRaises(ValueError):
                safe_details(cause_reason_code=value)
        with self.assertRaises(TypeError):
            safe_details(raw_body="private")

    def test_series_only_identity_has_no_missing_episode_requirement(self):
        self.assertEqual(safe_details(missing_fields=[]), {})
        # Episode IDs are reportable only for supplied episode entries.
        self.assertEqual(safe_details(invalid_fields=["episodes[].anidb_episode_id"]),
                         {"invalid_fields": ["episodes[].anidb_episode_id"]})


if __name__ == "__main__":
    unittest.main()

