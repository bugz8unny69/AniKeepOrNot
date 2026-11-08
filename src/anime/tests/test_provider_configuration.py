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
from src.anime.providers.configuration import build_identity_worker, options_from_configuration
from src.anime.providers.errors import ProviderConfigurationError
from src.anime.providers.http import Credentials
from src.anime.secrets import SecretStore, SecretStorageError
from src.anime.storage.sqlite import connect, initialize, accept_event_and_job


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
        self.assertEqual((current.schema_version,current.revision),(15,15))
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


class IdentityWorkerConfigurationTests(unittest.IsolatedAsyncioTestCase):
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
        self.event,self.job = accept_event_and_job(db,"test",b'{"release_name":"Test Anime"}',kind="IDENTIFY_ANIME",payload={})
        db.close()

    def state(self):
        db = connect(self.database)
        try: return db.execute("SELECT state,attempt_count FROM jobs WHERE job_id=?",(self.job,)).fetchone()
        finally: db.close()

    async def test_startup_requires_key_without_contacting_provider_or_claiming(self):
        with self.assertRaises(ProviderConfigurationError):
            await build_identity_worker(self.root, require_series_credentials=True)
        self.assertEqual(self.state(),("PENDING",0))
        self.assertFalse((self.root/"secrets.json").exists())

    async def test_secret_removal_or_invalid_configuration_blocks_before_claim(self):
        self.secrets.initialize("private-key")
        worker = await build_identity_worker(self.root, require_series_credentials=True)
        self.addAsyncCleanup(worker.close)
        self.secrets.update_tvdb_api_key("",expected_revision=1)
        with self.assertRaises(ProviderConfigurationError): await worker.attempt()
        self.assertEqual(self.state(),("PENDING",0))
        self.secrets.update_tvdb_api_key("private-new-key",expected_revision=2)
        self.store.update({"anidb":{"client_name":""}},expected_revision=1)
        with self.assertRaises(ProviderConfigurationError): await worker.attempt()
        self.assertEqual(self.state(),("PENDING",0))
        ticket = worker.gate.request()
        with worker.gate.exclusive(ticket) as lease: lease.complete()

    async def test_worker_without_tvdb_credentials_reaches_release_verifier(self):
        # Without TVDB credentials, worker starts cleanly for AniDB-origin releases
        worker = await build_identity_worker(self.root)
        self.addAsyncCleanup(worker.close)
        self.assertEqual(self.state(), ("PENDING", 0))

    async def test_composed_worker_finishes_with_current_settings_and_no_network_at_startup(self):
        self.secrets.initialize("private-key")
        seen = []
        def factory(options,credentials,**kwargs):
            async def resolve(request,configuration,cancelled):
                seen.append((options(configuration).client_version,credentials().api_key))
                return {"schema_version":2,"anime":{"anidb_id":123,"tvdb_series_id":456,"title":"Test Anime"}}
            return resolve
        with patch("src.anime.providers.configuration.make_resolver",side_effect=factory):
            worker = await build_identity_worker(self.root)
        self.addAsyncCleanup(worker.close)
        self.assertEqual(seen,[])
        self.store.update({"anidb":{"client_version":2}},expected_revision=1)
        self.secrets.update_tvdb_api_key("rotated-private-key",expected_revision=1)
        await worker.attempt()
        self.assertEqual(seen,[(2,"rotated-private-key")])
        self.assertEqual(self.state(),("DONE",1))


    async def test_resolver_captures_credentials_once_and_refreshes_options_next_attempt(self):
        import threading
        from src.anime.jobs.identify_anime_job import IdentityRequest
        from src.anime.providers.configuration import options_from_configuration
        from src.anime.providers.identity import make_resolver
        self.secrets.initialize("old-private-key")
        seen,constructed = [],[]
        secrets = self.secrets
        class Client:
            def __init__(self,database,options):
                self.options=options
                constructed.append(options)
            def title_candidates(self,aliases):
                if secrets.status().revision==1:
                    secrets.update_tvdb_api_key("new-private-key",expected_revision=1)
                return {123:("Test Anime",)}
            def anime(self,aid): return {"anidb_id":aid,"title":"Test Anime","titles":("Test Anime",)}
            def mapping(self,aid): return 456
            def series(self,tid,credentials):
                seen.append((self.options.client_version,credentials.api_key))
                return {"titles":("Test Anime",),"animation":True,"live":False,"anidb_ids":{123}}
        resolver = make_resolver(options_from_configuration,self.secrets.tvdb_credentials,client_factory=Client)
        startup=self.store.read()
        request=IdentityRequest(self.event,"test",b'{"release_name":"Test Anime"}')
        await resolver(request,for_operation(startup,startup,component="worker"),threading.Event())
        current=self.store.update({"anidb":{"client_version":2}},expected_revision=1).snapshot
        await resolver(request,for_operation(current,startup,component="worker"),threading.Event())
        self.assertEqual(seen,[(1,"old-private-key"),(2,"new-private-key")])
        self.assertEqual([item.client_version for item in constructed],[1,2])


    async def test_default_composition_advances_to_reserved_episode_without_external_acquisition(self):
        from src.anime.release_contracts import VerifiedRelease
        self.secrets.initialize("private-key")
        def identity_factory(*args,**kwargs):
            async def resolve(source,*rest):
                return {"schema_version":2,"anime":{"anidb_id":123,"tvdb_series_id":456,"title":"Test Anime"}}
            return resolve
        def release_factory(*args,**kwargs):
            async def verify(source,*rest): return VerifiedRelease(source,1,987,0,1,100)
            return verify
        from src.anime.services import UP, ServiceAdapter, ServiceHealth, ServiceRegistry, default_registry
        class AlwaysUp(ServiceAdapter):
            def __init__(self,name,label):self.name,self.label=name,label
            def probe(self,configuration,secrets,*,now_ms=0):return self.result(UP,now_ms=now_ms)
        always_up=ServiceHealth(ServiceRegistry(AlwaysUp(a.name,a.label) for a in default_registry()))
        with patch("src.anime.providers.configuration.make_resolver",side_effect=identity_factory), \
             patch("src.anime.providers.configuration.make_verifier",side_effect=release_factory):
            worker=await build_identity_worker(self.root,backfill=True,service_health=always_up)
        self.addAsyncCleanup(worker.close)
        await worker.attempt()
        await worker.attempt()
        db=connect(self.database)
        try:
            self.assertEqual(db.execute('SELECT kind,state,event_id,created_by_job_id FROM jobs ORDER BY job_id').fetchall(),
                             [('IDENTIFY_ANIME','DONE',self.event,None),('VERIFY_RELEASE','DONE',self.event,self.job),('SELECT_USER_EVALUATION','PENDING',self.event,self.job+1)])
            self.assertEqual(db.execute('SELECT verification_scope FROM release_verifications').fetchall(),[('DECLARED_SINGLE_EPISODE',)])
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],3)
        finally:db.close()
        await worker.attempt()
        await worker.attempt()
        db=connect(self.database)
        try:
            self.assertEqual(db.execute('SELECT kind,state FROM jobs ORDER BY job_id').fetchall(),
                             [('IDENTIFY_ANIME','DONE'),('VERIFY_RELEASE','DONE'),('SELECT_USER_EVALUATION','DONE'),('RESERVE_EPISODE','DONE'),('PREPARE_ACQUISITION','PENDING')])
            self.assertEqual(db.execute('SELECT tvdb_season,tvdb_episode FROM episode_reservations').fetchall(),[(0,1)])
        finally:db.close()
        await worker.attempt()  # No torrent hash: preparation needs review.
        db=connect(self.database)
        try:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE kind='PREPARE_ACQUISITION'").fetchall(),
                             [('NEEDS_REVIEW',)])
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='DISCOVER_EPISODE_RELEASES'").fetchone()[0],0)
        finally:db.close()

    async def test_default_batch_acquisition_chain(self):
        self.secrets.initialize('private-key')
        async def batch_reader(*args):
            raise AssertionError('worker startup cannot read batch metadata')
        enabled=await build_identity_worker(
            self.root,read_batch_metainfo=batch_reader)
        self.addAsyncCleanup(enabled.close)
        stages=(
            ('EVALUATE_BATCH','RESERVE_BATCH'),
            ('RESERVE_BATCH','SUBMIT_BATCH'),
            ('SUBMIT_BATCH','TRANSFER_BATCH'),
            ('TRANSFER_BATCH','VERIFY_BATCH_PROBE'),
            ('VERIFY_BATCH_PROBE','UNLOCK_BATCH'),
            ('UNLOCK_BATCH','VERIFY_BATCH_EPISODE'),
            ('VERIFY_BATCH_EPISODE','STAGE_BATCH_EPISODE'),
            ('STAGE_BATCH_EPISODE',None),
        )
        for job,next_job in stages:
            with self.subTest(job=job):
                self.assertIn(job,enabled.registry.entries)
                self.assertEqual(enabled.registry.entries[job].successor_kinds,
                                 () if next_job is None else (next_job,))
        # Batch staging and Keep/Reject are not implemented: do not route
        # the last step into the unrelated single-episode staging pipeline.
        self.assertEqual(enabled.registry.entries['STAGE_BATCH_EPISODE'].successor_kinds,())
        self.assertIn('STAGE_USER_EVALUATION_MEDIA',enabled.registry.entries)

    async def test_metadata_preparation_is_installed_by_default_with_trusted_override(self):
        self.secrets.initialize('private-key')
        worker=await build_identity_worker(self.root)
        self.addAsyncCleanup(worker.close)
        self.assertIn('PREPARE_ACQUISITION',worker.registry.entries)
        self.assertIn('SUBMIT_TORRENT',worker.registry.entries)
        self.assertIn('DOWNLOAD_TORRENT',worker.registry.entries)
        self.assertIn('VERIFY_MEDIA',worker.registry.entries)
        self.assertIn('STAGE_USER_EVALUATION_MEDIA',worker.registry.entries)
        self.assertIn('CLEANUP_CANCELED_OPERATION',worker.registry.entries)
        self.assertIn('KEEP_MEDIA',worker.registry.entries)
        self.assertIn('REJECT_SERIES',worker.registry.entries)
        self.assertNotIn('REJECT_MEDIA',worker.registry.entries)
        self.assertIn('REFRESH_JELLYFIN',worker.registry.entries)
        self.assertIn('CLEANUP_KEPT_MEDIA',worker.registry.entries)
        self.assertIn('CLEANUP_DECIDED_DOWNLOAD',worker.registry.entries)
        self.assertIn('ENRICH_SHOKO_METADATA',worker.registry.entries)
        async def reader(*args):raise AssertionError('startup must not read metadata')
        composed=await build_identity_worker(self.root,read_metainfo=reader)
        self.addAsyncCleanup(composed.close)
        self.assertIn('PREPARE_ACQUISITION',composed.registry.entries)
        self.assertIn('DOWNLOAD_TORRENT',composed.registry.entries)
        self.assertEqual(composed.registry.entries['SUBMIT_TORRENT'].successor_kinds,('DOWNLOAD_TORRENT',))
        self.assertEqual(composed.registry.entries['DOWNLOAD_TORRENT'].successor_kinds,('VERIFY_MEDIA',))
        self.assertEqual(composed.registry.entries['VERIFY_MEDIA'].successor_kinds,('STAGE_USER_EVALUATION_MEDIA',))
        self.assertEqual(composed.registry.entries['RESERVE_EPISODE'].successor_kinds,('PREPARE_ACQUISITION',))
