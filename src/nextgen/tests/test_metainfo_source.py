"""Offline qBittorrent metadata readers and v11 -> v12 migration tests."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from src.nextgen.acquisition.metainfo_source import (
    MetadataSourceWaiting, MetadataClientUnsupported, make_batch_reader,
    make_reader, source_url, MetadataSourceConfigurationError,
)
from src.nextgen.acquisition.metadata import parse_batch_metainfo, ObservedBatchInventory
from src.nextgen.configuration_schema import (
    CONVERSIONS, CONFIGURATION_VERSION, ConfigurationSnapshot,
    ConfigurationError, ConfigurationMigrationRequired, convert_document,
    load_current, FIELDS,
)
from src.nextgen.tests.test_acquisition_metadata import metadata


class MetadataReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        files = [
            {b'length': 200, b'path': [b'S01E01.mkv']},
            {b'length': 250, b'path': [b'S01E02.mkv']},
        ]
        self.raw, self.digest = metadata(name=b'Show', files=files)
        self.inventory = parse_batch_metainfo(self.raw, self.digest)
        self.configuration = ConfigurationSnapshot(1, {
            'qbittorrent': {'base_url':'http://localhost:8080'},
            'storage': {'download_root':'/downloads'},
        })
        self.secrets = SimpleNamespace(qbittorrent_credentials=lambda: object())
        self.calls = []

    def factory(self, strategy='fetch', *, stopped=True, pending=False):
        outer = self
        class Adapter:
            metadata_strategy = strategy
            def __init__(self, options, credentials):
                outer.calls.append(('construct',options.base_url))
            def login(self):
                outer.calls.append(('login',))
            def fetch_metadata(self, source, expected_hash, *, batch=False):
                outer.calls.append(('fetch',source,expected_hash,batch))
                return None if pending else (outer.inventory,outer.raw)
            def inspect(self,digest):
                outer.calls.append(('inspect',digest))
                return SimpleNamespace(state='pausedDL') if stopped else None
            def export_metainfo(self,digest,*,batch=False):
                outer.calls.append(('export',digest,batch))
                return outer.inventory,outer.raw
        return Adapter

    async def test_batch_fetch_reads_two_videos_without_a_torrent_add(self):
        reader = make_batch_reader(secrets=self.secrets,client_factory=self.factory())
        actual = await reader(self.digest,self.configuration,asyncio.Event())
        self.assertEqual(parse_batch_metainfo(actual,self.digest),self.inventory)
        self.assertIn(('fetch',self.digest,self.digest,True),self.calls)
        self.assertFalse(any(call[0]=='export' for call in self.calls))

    async def test_metadata_only_pending_defers_without_submission(self):
        reader = make_batch_reader(secrets=self.secrets,
                                   client_factory=self.factory(pending=True))
        with self.assertRaises(MetadataSourceWaiting):
            await reader(self.digest,self.configuration,asyncio.Event())
        self.assertIn(('fetch',self.digest,self.digest,True),self.calls)

    async def test_older_qb_does_not_adopt_unjournaled_torrents(self):
        for strategy in ('guarded_export','legacy_poll'):
            with self.subTest(strategy=strategy):
                reader=make_batch_reader(secrets=self.secrets,
                                         client_factory=self.factory(strategy))
                with patch('src.nextgen.storage.batch_metadata_admission.lookup',
                           return_value=(None,None)):
                    with self.assertRaises(MetadataClientUnsupported):
                        await reader(self.digest,self.configuration,asyncio.Event())
        self.assertFalse(any(call[0]=='export' for call in self.calls))

    async def test_cancelled_batch_makes_no_client_requests(self):
        cancelled=asyncio.Event()
        cancelled.set()
        reader=make_batch_reader(secrets=self.secrets,client_factory=self.factory())
        with self.assertRaises(asyncio.CancelledError):
            await reader(self.digest,self.configuration,cancelled)
        self.assertEqual(self.calls,[])


class SourceSchemaRetirementTests(unittest.TestCase):
    def test_obsolete_source_settings_are_not_live_fields(self):
        self.assertEqual(CONFIGURATION_VERSION,13)
        self.assertNotIn('torrent_metadata.base_url',FIELDS)
        self.assertNotIn('torrent_metadata.timeout_seconds',FIELDS)
        for settings in (
            {'torrent_metadata':{'base_url':'https://cache.example'}},
            {'torrent_metadata':{'timeout_seconds':30}},
        ):
            with self.subTest(settings=settings),self.assertRaises(ConfigurationError):
                ConfigurationSnapshot(1,settings)

    def test_v11_validates_then_removes_exact_old_section(self):
        doc=CONVERSIONS[0]({})
        for n in range(1,11):
            doc=CONVERSIONS[n](doc)
        self.assertEqual(doc['schema_version'],11)
        doc['settings']['torrent_metadata']={
            'base_url':'https://cache.example/torrents',
            'timeout_seconds':45,
        }
        doc['settings']['logging']['level']='DEBUG'
        old=copy.deepcopy(doc)
        with self.assertRaises(ConfigurationMigrationRequired):
            load_current(json.dumps(doc).encode())
        upgraded=convert_document(doc)
        self.assertEqual(doc,old)
        self.assertEqual(upgraded.revision,doc['revision']+2)
        self.assertEqual(upgraded.schema_version,13)
        self.assertNotIn('torrent_metadata',upgraded.to_document()['settings'])
        self.assertEqual(upgraded.value('logging.level'),'DEBUG')

    def test_v11_obsolete_source_not_silently_skipped_when_invalid(self):
        doc=CONVERSIONS[0]({})
        for n in range(1,11):
            doc=CONVERSIONS[n](doc)
        doc['settings']['torrent_metadata']['base_url']='https://cache.example?private=1'
        with self.assertRaises(ConfigurationError):
            convert_document(doc)

    def test_historical_source_url_is_validation_only(self):
        self.assertEqual(source_url('https://cache.example/torrents/'),
                         'https://cache.example/torrents')
        with self.assertRaises(MetadataSourceConfigurationError):
            source_url('https://private:token@cache.example/torrents')
