"""Pure configuration/restart/conversion cases; intentionally unexecuted."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.anime.configuration_schema import (
    CONFIGURATION_VERSION, FIELDS, MAX_BYTES, MAX_REVISION, ConfigurationError,
    ConfigurationMigrationRequired, ConfigurationSnapshot, convert_document,
    decode, defaults, document_snapshot, encode, for_operation, load_current,
    patch_snapshot, source_version, field_definitions, ConfigurationConflict,
)
from src.anime.settings import load_settings


class ConfigurationSchemaTests(unittest.TestCase):
    def test_defaults_reuse_logging_and_have_explicit_apply_metadata(self):
        snapshot = ConfigurationSnapshot(1, {})
        self.assertEqual(snapshot.schema_version, CONFIGURATION_VERSION)
        self.assertEqual(snapshot.logging_settings().logging.level, "INFO")
        self.assertEqual(snapshot.value("worker.concurrency"), 4)
        self.assertEqual(snapshot.value("qbittorrent.metadata_poll_seconds"), 5)
        self.assertEqual(snapshot.value("qbittorrent.metadata_poll_attempts"), 24)
        definitions = field_definitions()
        self.assertEqual(definitions["qbittorrent.metadata_poll_seconds"]["minimum"], 1)
        self.assertEqual(definitions["qbittorrent.metadata_poll_attempts"]["maximum"], 24)
        for definition in FIELDS.values():
            self.assertIn(definition.apply_mode, {"next_operation", "restart"})
            self.assertEqual(bool(definition.restart_components), definition.apply_mode == "restart")
        fresh = defaults()
        fresh["logging"]["components"]["api"] = "TRACE"
        self.assertEqual(defaults()["logging"]["components"], {})
        metadata = field_definitions()
        self.assertEqual(json.loads(json.dumps(metadata)), metadata)
        metadata["logging.components"]["default"]["api"] = "DEBUG"
        self.assertEqual(field_definitions()["logging.components"]["default"], {})

    def test_snapshots_are_detached_immutable_and_safe_to_represent(self):
        source = {"logging": {"components": {"api": "DEBUG"}}}
        snapshot = ConfigurationSnapshot(2, source)
        source["logging"]["components"]["api"] = "ERROR"
        self.assertEqual(snapshot.value("logging.components")["api"], "DEBUG")
        with self.assertRaises(TypeError):
            snapshot.values["logging"]["components"]["api"] = "TRACE"
        external = snapshot.to_document()
        external["settings"]["logging"]["level"] = "TRACE"
        self.assertEqual(snapshot.value("logging.level"), "INFO")
        self.assertNotIn("database_path", repr(snapshot))

    def test_new_operations_use_latest_values_but_pin_restart_fields(self):
        startup = ConfigurationSnapshot(1, {})
        newer, _ = patch_snapshot(startup, {
            "logging": {"level": "DEBUG"},
            "api": {"port": 9999}, "worker": {"concurrency": 8}})
        old_operation = for_operation(startup, startup, component="api")
        operation = for_operation(newer, startup, component="api")
        self.assertEqual(old_operation.value("logging.level"), "INFO")
        self.assertEqual(operation.value("logging.level"), "DEBUG")
        self.assertEqual(operation.value("api.port"), 8765)
        self.assertEqual(operation.value("worker.concurrency"), 4)
        self.assertEqual(operation.pending_restart_fields, ("api.port",))
        self.assertEqual((operation.desired_revision, operation.startup_revision), (2, 1))
        restarted = for_operation(newer, newer, component="api")
        self.assertEqual(restarted.value("api.port"), 9999)
        self.assertEqual(restarted.pending_restart_fields, ())
        reverted, _ = patch_snapshot(newer, {"api": {"port": 8765}})
        self.assertEqual(for_operation(reverted, startup, component="api").pending_restart_fields, ())
        with self.assertRaises(ConfigurationConflict):
            for_operation(startup, newer, component="api")

    def test_conversion_is_explicit_pure_and_preserves_existing_logging_values(self):
        old = {"logging": {"level": "DEBUG", "components": {"coordinator": "TRACE"}}}
        original = copy.deepcopy(old)
        migrated = convert_document(old)
        self.assertEqual(old, original)
        self.assertEqual(source_version(old), 0)
        self.assertEqual(migrated.revision, 15)
        self.assertEqual(migrated.value("logging.level"), "DEBUG")
        self.assertEqual(migrated.value("logging.components")["worker"], "TRACE")
        self.assertEqual(convert_document(migrated.to_document()), migrated)
        with self.assertRaises(ConfigurationMigrationRequired):
            load_current(json.dumps(old).encode())
        with self.assertRaises(ConfigurationError):
            convert_document({"daemon": {"concurrency": 4}})  # Not a legacy production import.

    def test_anime_language_is_strict_and_switchable(self):
        current = ConfigurationSnapshot(1, {})
        self.assertEqual(current.value("anime.title_language"), "english")
        changed, fields = patch_snapshot(current, {"anime": {"title_language": "japanese"}})
        self.assertEqual(changed.value("anime.title_language"), "japanese")
        self.assertEqual(fields, ("anime.title_language",))
        self.assertEqual(field_definitions()["anime.title_language"]["apply_mode"], "next_operation")
        for bad in ("ja", "Romaji", "", None, 1):
            with self.assertRaises(ConfigurationError):
                patch_snapshot(current, {"anime": {"title_language": bad}})

    def test_strict_decode_rejects_duplicate_nonfinite_oversized_or_invalid_input(self):
        for raw in (b'{"logging":{},"logging":{}}', b'{"x":NaN}', b'{"x":Infinity}',
                    b"\xff", b"{broken", b"x" * (MAX_BYTES + 1)):
            with self.subTest(raw=raw[:40]), self.assertRaises(ConfigurationError):
                decode(raw)

    def test_unknown_fields_and_bad_values_are_not_silently_dropped(self):
        bad = (
            {"api": {"port": True}}, {"api": {"port": 0}}, {"api": {"port": 65536}},
            {"worker": {"concurrency": 33}}, {"worker": {"concurrency": 1.5}},
            {"qbittorrent": {"metadata_poll_seconds": 0}},
            {"qbittorrent": {"metadata_poll_seconds": 11}},
            {"qbittorrent": {"metadata_poll_attempts": 25}},
            {"qbittorrent": {"metadata_poll_attempts": True}},
            {"api": {"host": "http://example.invalid/private-token"}},
            {"api": {"host": ":::invalid"}}, {"storage": {"database_path": "relative"}},
            {"storage": {"database_path": "/"}}, {"storage": {"database_path": "/x\nprivate"}},
            {"logging": {"level": "private-secret"}}, {"logging": {"components": {"unknown": "INFO"}}},
            {"secret": {"token": "private-secret"}}, {"api": {"password": "private-secret"}},
        )
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ConfigurationError) as caught:
                ConfigurationSnapshot(1, value)
            self.assertNotIn("private-secret", str(caught.exception))
        for host in ("localhost", "127.0.0.1", "::1", "api.example.invalid"):
            ConfigurationSnapshot(1, {"api": {"host": host}})

    def test_version_revision_roundtrip_noop_and_bounds(self):
        initial = ConfigurationSnapshot(1, {})
        self.assertEqual(document_snapshot(decode(encode(initial))), initial)
        candidate, changed = patch_snapshot(initial, {"logging": {"level": "INFO"}})
        self.assertIs(candidate, initial)
        self.assertEqual(changed, ())
        for version in (True, 0, 1, 2, 3, 4, 5, 6, 7, 9, "1"):
            with self.subTest(version=version), self.assertRaises(ConfigurationError):
                document_snapshot({"schema_version": version, "revision": 1, "settings": {}})
        for revision in (True, 0, -1, "1", MAX_REVISION + 1):
            with self.subTest(revision=revision), self.assertRaises(ConfigurationError):
                ConfigurationSnapshot(revision, {})
        with self.assertRaises(ConfigurationError):
            patch_snapshot(ConfigurationSnapshot(MAX_REVISION, {}), {"logging": {"level": "DEBUG"}})

    def test_logging_loader_supports_both_formats_without_writing_either(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            for raw in (b'{"logging":{"level":"DEBUG"}}',
                        encode(ConfigurationSnapshot(3, {"logging": {"level": "DEBUG"}}))):
                path.write_bytes(raw)
                self.assertEqual(load_settings(path).logging.level, "DEBUG")
                self.assertEqual(path.read_bytes(), raw)


class UserEvaluationSettingsRenameTests(unittest.TestCase):
    """v14 -> v15: the "trial" settings become "user evaluation", values kept (refactor RF-006d)."""

    def v14(self, **settings):
        return {'schema_version': 14, 'revision': 7, 'settings': settings}

    def test_values_move_to_the_new_names_and_the_revision_advances_once(self):
        from src.anime.configuration_schema import convert_document
        source = self.v14(trial={'backfill_count': 3, 'following_count': 2},
                          storage={'trial_root': '/media/evaluation', 'keep_root': '/media/keep'})
        original = copy.deepcopy(source)
        current = convert_document(source)
        self.assertEqual(source, original)
        self.assertEqual((current.schema_version, current.revision), (15, 8))
        self.assertEqual((current.value('user_evaluation.backfill_count'), current.value('user_evaluation.following_count'),
                          current.value('storage.user_evaluation_root'), current.value('storage.keep_root')),
                         (3, 2, '/media/evaluation', '/media/keep'))
        settings = current.to_document()['settings']
        self.assertNotIn('trial', settings)
        self.assertNotIn('trial_root', settings['storage'])

    def test_each_version_accepts_only_its_own_names(self):
        from src.anime.configuration_schema import ConfigurationSnapshot, convert_document
        with self.assertRaises(ConfigurationError):   # v14 cannot already use the new names
            convert_document(self.v14(user_evaluation={'backfill_count': 3}))
        with self.assertRaises(ConfigurationError):
            convert_document(self.v14(storage={'user_evaluation_root': '/media/evaluation'}))
        with self.assertRaises(ConfigurationError):   # current settings refuse the old names
            ConfigurationSnapshot(1, {'trial': {'backfill_count': 3}})
        with self.assertRaises(ConfigurationError):
            ConfigurationSnapshot(1, {'storage': {'trial_root': '/media/evaluation'}})

    def test_cross_checks_apply_under_both_names(self):
        from src.anime.configuration_schema import ConfigurationSnapshot, convert_document
        for make in (lambda: convert_document(self.v14(storage={'trial_root': '/media', 'keep_root': '/media/keep'})),
                     lambda: ConfigurationSnapshot(1, {'storage': {'user_evaluation_root': '/media',
                                                                   'keep_root': '/media/keep'}})):
            with self.assertRaises(ConfigurationError):   # nested media folders are refused either way
                make()
