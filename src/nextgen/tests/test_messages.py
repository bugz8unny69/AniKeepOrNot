"""Offline catalog coverage and locale fallback; tests intentionally unrun."""
import unittest

from src.nextgen.messages import (
    DEFAULT_CATALOG, ENGLISH, EVENTS, STATES, MessageCatalog, validate_event,
)
from src.nextgen.reasons import REGISTRY


class MessageTests(unittest.TestCase):
    def test_intake_copy_keeps_existing_diagnostic_keys(self):
        expected={
            'TRIAL_SELECTED':'This episode is selected for Intake.',
            'TRIAL_OUTSIDE_WINDOW':'This episode is outside the Intake selection.',
            'TRIAL_DISABLED':'Intake episode selection is disabled.',
            'TRIAL_PLAN_CONFLICT':'The Intake selection plan needs review.',
        }
        for code,message in expected.items():
            with self.subTest(code=code):
                rendered=DEFAULT_CATALOG.reason(code)
                self.assertEqual(rendered['message'],message)
                self.assertEqual(rendered['message_key'],f'reason.{code}.message')
                self.assertNotIn('trial',rendered['action'].lower())
        event=DEFAULT_CATALOG.event('TRIAL_EVALUATED')
        self.assertEqual(event['message'],'Intake episode selection evaluated.')
        self.assertEqual(event['message_key'],'event.TRIAL_EVALUATED.message')

    def test_every_registered_marker_has_readable_copy_and_a_stable_key(self):
        for code, entry in EVENTS.items():
            view = DEFAULT_CATALOG.event(code)
            self.assertEqual(view["message_key"], f"event.{code}.message")
            self.assertEqual(view["message"], entry.message)
            self.assertTrue(view["message"].strip())
        for code, entry in REGISTRY.items():
            view = DEFAULT_CATALOG.reason(code)
            self.assertEqual(view["message"], entry.message)
            self.assertEqual(view["action"], entry.action)
            self.assertIn(view["message_key"], ENGLISH)
            self.assertIn(view["action_key"], ENGLISH)
        expected = {
            "acquisition": {"ACTIVE", "STAGED", "CANCEL_REQUESTED", "CANCELED"},
            "job": {"PENDING", "RUNNING", "RETRY_WAIT", "ON_HOLD", "NEEDS_REVIEW", "DONE", "BLOCKED", "CANCELED", "FAILED"},
            "job_activity": {"DOWNLOADING", "WAITING_FOR_DOWNLOAD", "WAITING_FOR_SEARCH", "SKIPPED_DUPLICATE"},
            "incoming_event": {"PENDING", "RUNNING", "RETRY_WAIT", "NEEDS_REVIEW", "IDENTIFIED"},
        }
        for entity, codes in expected.items():
            self.assertEqual(set(STATES[entity]), codes)
            for code in codes:
                self.assertEqual(DEFAULT_CATALOG.state(entity, code)["message"],
                                 STATES[entity][code])

    def test_locale_maps_are_snapshots_with_english_fallback(self):
        translations = {"fr": {"event.CREATED.message": "Travail créé."}}
        catalog = MessageCatalog(translations)
        translations["fr"]["event.CREATED.message"] = "changed"
        self.assertEqual(catalog.event("CREATED", locale="fr")["message"], "Travail créé.")
        for locale in ("fr", "not-installed", None, []):
            self.assertEqual(catalog.reason("IDENTITY_INVALID", locale=locale)["message"],
                             REGISTRY["IDENTITY_INVALID"].message)
        self.assertEqual(catalog.event("CREATED")["message"], "Job created.")

    def test_historical_episode_selection_codes_use_current_plain_language(self):
        for code in ("TRIAL_SELECTED", "TRIAL_OUTSIDE_WINDOW", "TRIAL_DISABLED", "TRIAL_PLAN_CONFLICT"):
            rendered = DEFAULT_CATALOG.reason(code)
            self.assertTrue(rendered["message"])
            self.assertTrue(rendered["action"])
            self.assertNotIn("trial", rendered["message"].lower())
            self.assertNotIn("trial", rendered["action"].lower())

    def test_unknown_history_is_readable_without_untrusted_prose(self):
        for code in ("FUTURE_CODE", None, "PASSWORD=secret", "<script>", "A" * 65, []):
            self.assertIn("recorded", DEFAULT_CATALOG.event(code)["message"])
            self.assertEqual(DEFAULT_CATALOG.state("job", code)["message"], "Unknown status")
            self.assertIsNone(DEFAULT_CATALOG.reason(code)["retryable"])
        self.assertEqual(DEFAULT_CATALOG.event("FUTURE_CODE")["code"], "FUTURE_CODE")
        self.assertEqual(DEFAULT_CATALOG.event("password=secret")["code"], "UNKNOWN")

    def test_invalid_new_emissions_do_not_use_historical_fallback(self):
        for code, reason, scope in (
            ("NEW_EVENT", None, "job"), ("ACCEPTED", None, "job"),
            ("CREATED", "IDENTITY_INVALID", "job"),
            ("RETRY_SCHEDULED", None, "job"),
            ("LEASE_EXPIRED", "PROVIDER_UNAVAILABLE", "job"),
            ("OPERATION_FAILED", "JOB_HANDLER_FAILED", "job"),
        ):
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate_event(code, reason, scope=scope)
        validate_event("LEASE_EXPIRED", "JOB_LEASE_EXPIRED", scope="job")

    def test_translation_validation_rejects_injection_and_unknown_keys(self):
        for text in ("", "x" * 513, "line\nbreak", "{secret}", "<script>", "\u202e"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                MessageCatalog({"fr": {"event.CREATED.message": text}})
        with self.assertRaises(ValueError):
            MessageCatalog({"fr": {"new.key": "No registration"}})
        with self.assertRaises(ValueError):
            MessageCatalog({"en": {"event.CREATED.message": "Override"}})
        with self.assertRaises(ValueError):
            DEFAULT_CATALOG.text("unregistered")
