"""Offline destination settings and historical conversion cases; unexecuted."""
import copy
import json
import unittest

from src.nextgen.configuration_schema import (
    ConfigurationError, ConfigurationSnapshot, convert_document, defaults,
    field_definitions, for_operation, patch_snapshot, CONVERSIONS,
)


class MediaDestinationTests(unittest.TestCase):
    def test_blank_defaults_labels_and_new_attempt_snapshots(self):
        startup = ConfigurationSnapshot(1, {})
        self.assertEqual(startup.value("storage.trial_root"), "")
        self.assertEqual(startup.value("storage.keep_root"), "")
        current, changed = patch_snapshot(startup, {"storage": {
            "trial_root": "/media/trial", "keep_root": "/media/library"}})
        self.assertEqual(set(changed), {"storage.trial_root", "storage.keep_root"})
        operation = for_operation(current, startup, component="worker")
        self.assertEqual(operation.value("storage.trial_root"), "/media/trial")
        self.assertEqual(operation.value("storage.keep_root"), "/media/library")
        self.assertEqual(startup.value("storage.trial_root"), "")
        self.assertEqual(operation.pending_restart_fields, ())
        for name in changed:
            field = field_definitions()[name]
            self.assertTrue(field["label"] and field["help"])
            self.assertEqual(field["apply_mode"], "next_operation")
        json.dumps(field_definitions())

    def test_bad_or_overlapping_paths_are_rejected_without_filesystem_access(self):
        for path in ("relative", "/", "//host/media", "/media/../trial", "/media/./trial", "/media/trial/", "/media\ntrial", 123):
            with self.subTest(path=path), self.assertRaises(ConfigurationError):
                ConfigurationSnapshot(1, {"storage": {"trial_root": path}})
        for user_evaluation, keep in (("/media/trial", "/media/trial"), ("/media", "/media/keep"), ("/media/trial", "/media")):
            with self.subTest(user_evaluation=user_evaluation, keep=keep), self.assertRaises(ConfigurationError):
                ConfigurationSnapshot(1, {"storage": {"trial_root": user_evaluation, "keep_root": keep}})
        with self.assertRaises(ConfigurationError):
            ConfigurationSnapshot(1, {"storage": {"trial_root": "/var/lib/anime-discovery"}})

    def test_explicit_v2_upgrade_preserves_values_and_does_not_guess_roots(self):
        old = {"schema_version": 2, "revision": 9, "settings": {
            "worker": {"concurrency": 8}, "logging": {"level": "DEBUG"}}}
        before = copy.deepcopy(old)
        snapshot = convert_document(old)
        self.assertEqual(old, before)
        self.assertEqual((snapshot.schema_version, snapshot.revision), (14, 21))
        self.assertEqual(snapshot.value("worker.concurrency"), 8)
        self.assertEqual(snapshot.value("storage.trial_root"), "")
        self.assertEqual(snapshot.value("storage.keep_root"), "")
        for version in (1, 2):
            with self.subTest(version=version), self.assertRaises(ConfigurationError):
                convert_document({"schema_version": version, "revision": 1, "settings": {
                    "storage": {"trial_root": "/media/trial"}}})
        historical = CONVERSIONS[0]({})
        self.assertNotIn("trial_root", historical["settings"]["storage"])
        self.assertNotIn("keep_root", CONVERSIONS[1](historical)["settings"]["storage"])


    def test_restart_pinning_revalidates_media_roots_against_active_database(self):
        startup = ConfigurationSnapshot(1, {"storage": {
            "database_path": "/srv/old/queue.sqlite"}})
        for root_field in ("trial_root", "keep_root"):
            with self.subTest(root_field=root_field):
                # Valid after restart, unsafe while the old database is pinned.
                desired, _ = patch_snapshot(startup, {"storage": {
                    "database_path": "/srv/new/queue.sqlite", root_field: "/srv/old"}})
                for component in ("worker", "api"):
                    with self.subTest(component=component), self.assertRaises(ConfigurationError):
                        for_operation(desired,startup,component=component)
                restarted = for_operation(desired,desired,component="worker")
                self.assertEqual(restarted.value("storage."+root_field),"/srv/old")
                self.assertEqual(restarted.value("storage.database_path"),"/srv/new/queue.sqlite")
                self.assertEqual(restarted.pending_restart_fields,())
                # An unrelated live root can still apply before restart.
                adjusted, _ = patch_snapshot(desired,{"storage": {root_field: "/media/safe"}})
                operation = for_operation(adjusted,startup,component="worker")
                self.assertEqual(operation.value("storage."+root_field),"/media/safe")
                self.assertEqual(operation.value("storage.database_path"),"/srv/old/queue.sqlite")
                self.assertIn("storage.database_path",operation.pending_restart_fields)
