"""Offline download settings, credentials and conversion cases. Unexecuted."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.nextgen.acquisition.settings import (DownloadConfigurationError, SizeLimitExceeded, SizeLimits,
                                             download_context, download_options)
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationError, ConfigurationConflict, ConfigurationSnapshot, CONVERSIONS, convert_document
from src.nextgen.secrets import SecretStore, SecretStorageError


class DownloadSettingsTests(unittest.TestCase):
    def test_historical_outputs_and_current_conversion_defaults(self):
        doc={}
        for version in range(5):
            doc=CONVERSIONS[version](doc)
            self.assertNotIn('qbittorrent',doc['settings'])
            self.assertNotIn('acquisition',doc['settings'])
            self.assertNotIn('download_root',doc['settings']['storage'])
        current=convert_document(doc)
        self.assertEqual((current.schema_version,current.revision),(15,15))
        self.assertEqual(current.value('acquisition.max_episode_mb'),600)
        self.assertEqual(current.value('acquisition.max_plan_mb'),3000)
        with self.assertRaises(DownloadConfigurationError):download_options(current)
        with self.assertRaises(ConfigurationError):convert_document({**doc,'settings':{'qbittorrent':{'base_url':'http://localhost'}}})

    def test_endpoint_credentials_paths_and_limits_are_strict(self):
        good={'storage':{'download_root':'/srv/downloads'},'qbittorrent':{'base_url':'http://127.0.0.1:8080/'}}
        self.assertEqual(download_options(ConfigurationSnapshot(1,good)).base_url,'http://127.0.0.1:8080')
        for url in ('http://user:password@host','http://host/path','http://host/?x=y','http://host/#x',
                    'file:///srv/downloads','http://host:0','http://host:','http://bad host','http://host\\evil','https://host?'):
            with self.assertRaises(ConfigurationError):ConfigurationSnapshot(1,{'qbittorrent':{'base_url':url}})
        for folder in ('/srv/trial','/srv/trial/downloads','/srv'):
            with self.assertRaises(ConfigurationError):ConfigurationSnapshot(1,{'storage':{'user_evaluation_root':'/srv/trial','download_root':folder}})
        with self.assertRaises(ConfigurationError):ConfigurationSnapshot(1,{'storage':{'download_root':'/var/lib/anime-discovery'}})
        with self.assertRaises(ConfigurationError):ConfigurationSnapshot(1,{'qbittorrent':{'password':'not-public'}})
        for value in (True,0,-1,1000001):
            with self.assertRaises(ConfigurationError):ConfigurationSnapshot(1,{'acquisition':{'max_plan_mb':value}})
        limit=SizeLimits(1,2)
        limit.admit(1_000_000,1_000_000)
        with self.assertRaises(SizeLimitExceeded):limit.admit(1_000_001)
        with self.assertRaises(SizeLimitExceeded):limit.admit(1_000_000,1_000_001)


class DownloadSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=SecretStore(self.root)

    def test_atomic_pair_updates_preserve_tvdb_and_never_return_values(self):
        self.store.initialize('tvdb-private')
        result=self.store.update_qbittorrent_credentials('qb-private-user','qb-private-pass',expected_revision=1)
        self.assertTrue(result.status.qbittorrent_configured)
        public=json.dumps(asdict(result))+repr(self.store.qbittorrent_credentials())
        self.assertNotIn('qb-private',public)
        self.assertNotIn('tvdb-private',public)
        self.assertEqual(self.store.tvdb_credentials().api_key,'tvdb-private')
        self.store.update_tvdb_api_key('tvdb-rotated',expected_revision=2)
        self.assertEqual(self.store.qbittorrent_credentials().password,'qb-private-pass')
        self.assertEqual((self.root/'secrets.json').stat().st_mode&0o777,0o600)
        with self.assertRaises(ConfigurationConflict):self.store.update_qbittorrent_credentials('u','p',expected_revision=1)
        with self.assertRaises(SecretStorageError):self.store.update_qbittorrent_credentials('u','',expected_revision=3)
        self.assertFalse(self.store.update_qbittorrent_credentials('qb-private-user','qb-private-pass',expected_revision=3).saved)
        self.store.update_qbittorrent_credentials('','',expected_revision=3)
        with self.assertRaises(DownloadConfigurationError):self.store.qbittorrent_credentials()

    def test_v1_read_preserves_format_and_updates_publish_password_first_schema(self):
        path=self.root/'secrets.json'
        path.write_text(json.dumps({'schema_version':1,'revision':1,'credentials':{'tvdb_api_key':'old-private'}}))
        path.chmod(0o600)
        before=path.read_bytes()
        self.assertFalse(self.store.status().qbittorrent_configured)
        self.assertEqual(path.read_bytes(),before)
        self.store.update_tvdb_api_key('rotated-private',expected_revision=1)
        self.assertEqual(json.loads(path.read_text())['schema_version'],8)
        self.store.update_qbittorrent_credentials('qb-user','qb-pass',expected_revision=2)
        self.assertEqual(json.loads(path.read_text())['schema_version'],8)
        self.assertEqual(self.store.tvdb_credentials().api_key,'rotated-private')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['.settings.lock','secrets.json'])

    def test_failed_pair_publication_keeps_old_values_and_captured_context_is_immutable(self):
        store=ConfigurationStore(self.root)
        first=store.initialize({'storage':{'download_root':'/srv/downloads'},'qbittorrent':{'base_url':'http://localhost:8080'}}).snapshot
        self.store.initialize('',qbittorrent_username='old-user',qbittorrent_password='old-pass')
        old_options,old_credentials=download_context(first,self.store)
        with patch('src.nextgen.secrets.os.replace',side_effect=OSError('private-data')):
            with self.assertRaises(SecretStorageError) as failed:self.store.update_qbittorrent_credentials('new-user','new-pass',expected_revision=1)
        self.assertNotIn('private-data',str(failed.exception))
        self.store.update_qbittorrent_credentials('new-user','new-pass',expected_revision=1)
        store.update({'qbittorrent':{'timeout_seconds':10}},expected_revision=1)
        new_options,new_credentials=download_context(store.read(),self.store)
        self.assertEqual((old_options.timeout_seconds,old_credentials.password),(30,'old-pass'))
        self.assertEqual((new_options.timeout_seconds,new_credentials.password),(10,'new-pass'))
