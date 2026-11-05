"""Atomic publication, service ownership, conflicts and migrations; unrun."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import (
    ConfigurationConflict, ConfigurationError, ConfigurationIOError,
    ConfigurationMigrationRequired,
)


class ConfigurationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ConfigurationStore(self.root)
        self.initial = self.store.initialize().snapshot
        self.path = self.root / "settings.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_initialization_never_overwrites_existing_configuration(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        before = self.path.read_bytes()
        with self.assertRaises(ConfigurationConflict):
            self.store.initialize()
        self.assertEqual(self.path.read_bytes(), before)

    def test_preview_update_fresh_reads_restart_classification_and_noop(self):
        before = self.path.read_bytes()
        plan = self.store.preview({"api": {"port": 9999}}, expected_revision=1)
        self.assertFalse(plan.saved)
        self.assertEqual(plan.restart_fields, ("api.port",))
        self.assertEqual(plan.restart_components, ("api",))
        self.assertEqual(self.path.read_bytes(), before)
        result = self.store.update({"logging": {"level": "DEBUG"}}, expected_revision=1)
        self.assertTrue(result.saved)
        self.assertTrue(result.durability_confirmed)
        self.assertEqual(result.restart_fields, ())
        self.assertEqual(self.store.operation(self.initial, component="api").value("logging.level"), "DEBUG")
        self.assertEqual(self.initial.value("logging.level"), "INFO")
        after = self.path.read_bytes()
        no_op = self.store.update({"logging": {"level": "DEBUG"}}, expected_revision=2)
        self.assertFalse(no_op.saved)
        self.assertEqual(no_op.snapshot.revision, 2)
        self.assertEqual(self.path.read_bytes(), after)
        with self.assertRaises(ConfigurationConflict):
            self.store.update({"logging": {"level": "TRACE"}}, expected_revision=1)

    def test_two_publishers_cannot_overwrite_each_other_using_one_revision(self):
        def publish(level):
            try:
                result = ConfigurationStore(self.root).update(
                    {"logging": {"level": level}}, expected_revision=1)
                return result.snapshot.value("logging.level")
            except ConfigurationConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, ("DEBUG", "TRACE")))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(self.store.read().revision, 2)
        self.assertIn(self.store.read().value("logging.level"), {"DEBUG", "TRACE"})

    def test_invalid_updates_leave_old_bytes_and_revision_untouched(self):
        before = self.path.read_bytes()
        for changes in ({"secrets": {"token": "private"}}, {"api": {"port": True}}):
            with self.assertRaises(ConfigurationError):
                self.store.update(changes, expected_revision=1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.read().revision, 1)

    def test_failure_before_rename_preserves_old_file_and_cleans_temporary_files(self):
        before = self.path.read_bytes()
        with patch("src.nextgen.configuration.os.replace", side_effect=PermissionError("private")):
            with self.assertRaises(ConfigurationIOError) as caught:
                self.store.update({"logging": {"level": "DEBUG"}}, expected_revision=1)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".settings-*.tmp")), [])

    def test_file_fsync_failure_does_not_publish_or_change_revision(self):
        before = self.path.read_bytes()
        with patch("src.nextgen.configuration.os.fsync", side_effect=OSError("private")):
            with self.assertRaises(ConfigurationIOError):
                self.store.update({"logging": {"level": "DEBUG"}}, expected_revision=1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.read().revision, 1)
        self.assertEqual(list(self.root.glob(".settings-*.tmp")), [])

    def test_reading_missing_configuration_never_creates_directories(self):
        absent = self.root / "not-installed"
        with self.assertRaises(ConfigurationIOError):
            ConfigurationStore(absent).read()
        self.assertFalse(absent.exists())

    def test_directory_fsync_failure_reports_publication_without_claiming_rollback(self):
        original = os.fsync

        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory durability failure")
            return original(fd)

        with patch("src.nextgen.configuration.os.fsync", side_effect=sync):
            result = self.store.update({"logging": {"level": "DEBUG"}}, expected_revision=1)
        self.assertTrue(result.saved)
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(result.reason_code, "CONFIGURATION_DURABILITY_UNCONFIRMED")
        self.assertEqual(self.store.read().revision, 2)
        with self.assertRaises(ConfigurationConflict):
            self.store.update({"logging": {"level": "TRACE"}}, expected_revision=1)

    def test_publication_preserves_existing_owner_group_and_mode(self):
        os.chmod(self.path, 0o640)
        before = self.path.stat()
        self.store.update({"logging": {"level": "DEBUG"}}, expected_revision=1)
        after = self.path.stat()
        self.assertEqual((before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)),
                         (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)))

    def test_symlink_file_lock_or_directory_and_fifo_are_rejected(self):
        target = self.root / "target.json"
        target.write_bytes(self.path.read_bytes())
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(ConfigurationIOError):
            self.store.read()
        self.path.unlink()
        self.path.write_bytes(target.read_bytes())
        lock = self.root / ".settings.lock"
        lock.unlink()
        lock.symlink_to(target)
        with self.assertRaises(ConfigurationIOError):
            self.store.update({"logging": {"level": "DEBUG"}}, expected_revision=1)
        link = self.root / "linked-directory"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ConfigurationIOError):
            ConfigurationStore(link).read()
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(ConfigurationIOError):
            self.store.read()


class ConfigurationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "settings.json"
        self.raw = b'{ "logging": { "level": "DEBUG" } }\n'
        self.path.write_bytes(self.raw)
        self.store = ConfigurationStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_has_no_writes_and_apply_preserves_exact_original_backup(self):
        before = set(self.root.iterdir())
        plan = self.store.preview_migration()
        self.assertEqual((plan.from_version, plan.to_version, plan.required), (0, 14, True))
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertEqual(self.path.read_bytes(), self.raw)
        with self.assertRaises(ConfigurationMigrationRequired):
            self.store.read()
        result = self.store.migrate(expected_fingerprint=plan.source_fingerprint)
        self.assertTrue(result.saved)
        self.assertEqual((self.root / result.backup_name).read_bytes(), self.raw)
        self.assertEqual(self.store.read().value("logging.level"), "DEBUG")
        self.assertEqual(self.store.read().revision, 14)
        again = self.store.preview_migration()
        self.assertFalse(again.required)
        self.assertFalse(self.store.migrate(expected_fingerprint=again.source_fingerprint).saved)

    def test_changed_source_is_rejected_without_backup_or_conversion(self):
        plan = self.store.preview_migration()
        self.path.write_bytes(self.raw + b"\n")
        with self.assertRaises(ConfigurationConflict):
            self.store.migrate(expected_fingerprint=plan.source_fingerprint)
        self.assertEqual(self.path.read_bytes(), self.raw + b"\n")
        self.assertEqual(list(self.root.glob("*.bak")), [])

    def test_unsupported_source_never_silently_drops_legacy_keys(self):
        self.path.write_bytes(b'{"daemon":{"concurrency":4},"secret":"private"}')
        with self.assertRaises(ConfigurationError):
            self.store.preview_migration()
        self.assertEqual(list(self.root.glob("*.bak")), [])
