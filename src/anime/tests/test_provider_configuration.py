"""Offline provider settings/secret composition cases; intentionally unexecuted."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.anime.configuration import ConfigurationStore
from src.anime.configuration_schema import (
    ConfigurationError, ConfigurationConflict, ConfigurationSnapshot, CONVERSIONS,
    convert_document, for_operation, patch_snapshot,
)
from src.anime.providers.configuration import options_from_configuration
from src.anime.providers.errors import ProviderConfigurationError
from src.anime.providers.http import Credentials
from src.anime.secrets import SecretStore, SecretStorageError
from src.anime.storage.sqlite import connect, initialize


class ProviderConfigurationTests(unittest.TestCase):
    def test_provider_fields_are_live_and_never_include_enable_or_pin(self):
        initial = ConfigurationSnapshot(1,{})
        self.assertEqual(initial.value("anidb.client_name"),"")
        with self.assertRaises(ProviderConfigurationError): options_from_configuration(initial)
        current,changed = patch_snapshot(initial,{"anidb":{"client_name":"registeredclient","client_version":2},
                                                    "providers":{"timeout_seconds":15}})
        live = for_operation(current,initial,component="worker")
        self.assertEqual(options_from_configuration(live).client_version,2)
        self.assertEqual(live.pending_restart_fields,())
        self.assertEqual(set(changed),{"anidb.client_name","anidb.client_version","providers.timeout_seconds"})
        for values in ({"anidb":{"enabled":False}},{"tvdb":{"enabled":False}},{"tvdb":{"pin":"value"}},
                       {"anidb":{"client_name":"bad value"}},{"providers":{"timeout_seconds":61}}):
            with self.assertRaises(ConfigurationError): ConfigurationSnapshot(1,values)
        with self.assertRaises(TypeError): Credentials("key","unsupported-pin")

    def test_explicit_v3_conversion_preserves_all_historical_shapes(self):
        first = CONVERSIONS[0]({})
        second = CONVERSIONS[1](first)
        third = CONVERSIONS[2](second)
        for document in (first,second,third):
            self.assertNotIn("anidb",document["settings"])
            self.assertNotIn("providers",document["settings"])
            with self.assertRaises(ConfigurationError):
                convert_document({**document,"settings":{**document["settings"],"anidb":{"client_name":"new"}}})
        current = convert_document(third)
        self.assertEqual((current.schema_version,current.revision),(17,17))
        self.assertEqual(current.value("anidb.client_name"),"")
        self.assertEqual(current.value("providers.timeout_seconds"),30)


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root/"secrets.json"
        self.store = SecretStore(self.root)

    def test_private_atomic_revisioned_updates_have_presence_only_results(self):
        self.assertEqual(self.store.status().revision,0)
        self.assertFalse(self.path.exists())
        with self.assertRaises(ProviderConfigurationError): self.store.tvdb_credentials()
        result = self.store.initialize("private-key-one")
        self.assertTrue(result.status.tvdb_api_key_configured)
        self.assertNotIn("private-key",repr(result))
        self.assertNotIn("private-key",json.dumps(asdict(result)))
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.store.tvdb_credentials().api_key,"private-key-one")
        updated = self.store.update_tvdb_api_key("private-key-two",expected_revision=1)
        self.assertEqual(updated.status.revision,2)
        with self.assertRaises(ConfigurationConflict): self.store.update_tvdb_api_key("bad",expected_revision=1)
        self.assertEqual(self.store.tvdb_credentials().api_key,"private-key-two")
        unchanged = self.store.update_tvdb_api_key("private-key-two",expected_revision=2)
        self.assertFalse(unchanged.saved)
        self.store.update_tvdb_api_key("",expected_revision=2)
        with self.assertRaises(ProviderConfigurationError): self.store.tvdb_credentials()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),[".settings.lock","secrets.json"])

    def test_symlink_fifo_bad_permissions_and_unknown_secret_fields_are_rejected(self):
        self.store.initialize("private-key")
        self.path.chmod(0o644)
        with self.assertRaises(SecretStorageError): self.store.tvdb_credentials()
        self.path.chmod(0o600)
        document = json.loads(self.path.read_text())
        document["credentials"]["tvdb_pin"]="unsupported"
        self.path.write_text(json.dumps(document))
        with self.assertRaises(SecretStorageError): self.store.status()
        self.path.unlink()
        target = self.root/"other"
        target.write_text('{}')
        self.path.symlink_to(target)
        with self.assertRaises(SecretStorageError): self.store.status()
        self.path.unlink()
        os.mkfifo(self.path,0o600)
        with self.assertRaises(SecretStorageError): self.store.status()

    def test_prepublication_failure_keeps_old_secret_and_postpublication_failure_reports_saved(self):
        self.store.initialize("original-private-key")
        with patch("src.anime.secrets.os.replace",side_effect=OSError("private failure")):
            with self.assertRaises(SecretStorageError) as failed:
                self.store.update_tvdb_api_key("replacement-private-key",expected_revision=1)
        self.assertNotIn("private failure",str(failed.exception))
        self.assertEqual(self.store.tvdb_credentials().api_key,"original-private-key")
        actual_fsync = os.fsync
        def fsync(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("directory fsync failure")
            return actual_fsync(fd)
        with patch("src.anime.secrets.os.fsync",side_effect=fsync):
            result = self.store.update_tvdb_api_key("replacement-private-key",expected_revision=1)
        self.assertTrue(result.saved)
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(result.reason_code,"CONFIGURATION_DURABILITY_UNCONFIRMED")
        self.assertEqual(self.store.tvdb_credentials().api_key,"replacement-private-key")


class IdentityProcessConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root/"queue.sqlite"
        self.store = ConfigurationStore(self.root)
        self.store.initialize({"storage":{"database_path":str(self.database)},"anidb":{"client_name":"registeredclient"}})
        self.secrets = SecretStore(self.root)
        db = connect(self.database)
        initialize(db)
        db.close()




    async def test_identity_process_uses_current_settings_reports_and_exits(self):
        import io, json
        from src.anime import identity_process
        from src.anime.identity_bridge import queue_release
        self.secrets.initialize("private-key")
        db = connect(self.database)
        request = queue_release(db, b'{"release_name":"Other Anime - 02"}')
        db.close()
        seen = []
        def factory(options, credentials, **kwargs):
            async def resolve(request, configuration, cancelled, *, matching=None):
                seen.append((options(configuration).client_version, credentials().api_key))
                return {"schema_version": 2, "anime": {"anidb_id": 124, "tvdb_series_id": 457, "title": "Other Anime"}}
            return resolve
        self.store.update({"anidb": {"client_version": 2}}, expected_revision=1)
        self.secrets.update_tvdb_api_key("rotated-private-key", expected_revision=1)
        out = io.StringIO()
        with patch("src.anime.providers.identity.make_resolver", side_effect=factory):
            self.assertEqual(await identity_process.run(self.root, out), 0)   # Exits: nothing left due.
        self.assertEqual(seen, [(2, "rotated-private-key")])
        line = json.loads(out.getvalue())
        self.assertEqual((line["request_id"], line["result"], line["series"]["anime"]["anidb_id"]),
                         (request, "identified", 124))
        db = connect(self.database)
        try:   # Reported, not applied yet: kept for the API, not tried again meanwhile.
            self.assertEqual(db.execute("SELECT last_reason_code FROM identity_requests WHERE outcome IS NULL").fetchall(), [("REPORTED",)])
        finally: db.close()






