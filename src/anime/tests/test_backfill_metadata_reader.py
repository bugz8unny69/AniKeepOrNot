"""Offline backfill metadata fallback contracts; no network or workflow execution."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.anime.acquisition.metainfo_source import (
    MetadataClientUnsupported, MetadataSourceWaiting, make_reader,
)
from src.anime.episode_identity import VerifiedEpisode

HASH='a'*40
MAGNET='magnet:?xt=urn:btih:'+HASH


class BackfillMetadataReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source=SimpleNamespace(info_hash=HASH,event_id=25)
        self.configuration=SimpleNamespace(value=lambda key:{
            'storage.database_path':'unused.sqlite',
            'torrent_metadata.base_url':'',
            'torrent_metadata.timeout_seconds':30,
            'qbittorrent.base_url':'http://localhost:8080',
            'qbittorrent.timeout_seconds':30,
            'qbittorrent.category':'anime',
            'storage.download_root':'/var/downloads',
        }[key])
        self.secrets=SimpleNamespace(qbittorrent_credentials=lambda:SimpleNamespace())
        self.identity=VerifiedEpisode(100,200,1,10)
        self.qb=Mock()
        self.qb.metadata_strategy='fetch'
        self.qb.fetch_metadata.return_value=(object(),b'metainfo')
        self.patches=[
            patch('src.anime.storage.release_selection.acquisition_identity',
                  return_value=self.identity),
            # A backfill delivery carries its own sanitized magnet.
            patch('src.anime.storage.acquisition.source_magnet',return_value=MAGNET),
            patch('src.anime.torrent_client.qbittorrent.Client',return_value=self.qb),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_verified_magnet_produces_same_metainfo_reader_output(self):
        reader=make_reader(secrets=self.secrets,client_factory=lambda *_:self.qb)
        self.assertEqual(await reader(self.source,self.configuration,asyncio.Event()),
                         b'metainfo')
        self.qb.login.assert_called_once()
        self.qb.fetch_metadata.assert_called_once_with(MAGNET,HASH,batch=False)

    async def test_pending_metadata_defers_without_submission(self):
        self.qb.fetch_metadata.return_value=None
        reader=make_reader(secrets=self.secrets,client_factory=lambda *_:self.qb)
        with self.assertRaises(MetadataSourceWaiting):
            await reader(self.source,self.configuration,asyncio.Event())
        self.qb.add_stopped.assert_not_called()

    async def test_legacy_client_does_not_add_unverified_magnet(self):
        self.qb.metadata_strategy='guarded_export'
        reader=make_reader(secrets=self.secrets,client_factory=lambda *_:self.qb)
        with self.assertRaises(MetadataClientUnsupported):
            await reader(self.source,self.configuration,asyncio.Event())
        self.qb.fetch_metadata.assert_not_called()
        self.qb.add_stopped.assert_not_called()

    async def test_obsolete_metadata_source_setting_is_never_consulted(self):
        configuration=SimpleNamespace(value=lambda key: (
            (_ for _ in ()).throw(AssertionError('obsolete HTTP setting consulted'))
            if key.startswith('torrent_metadata.') else self.configuration.value(key)))
        reader=make_reader(secrets=self.secrets,client_factory=lambda *_:self.qb)
        self.assertEqual(await reader(self.source,configuration,asyncio.Event()),
                         b'metainfo')

    async def test_webhook_event_without_torrent_source_fetches_its_own_magnet(self):
        from src.anime.storage import release_selection
        with patch.object(release_selection,'acquisition_identity',return_value=None):
            reader=make_reader(secrets=self.secrets,client_factory=lambda *_:self.qb)
            self.assertEqual(await reader(self.source,self.configuration,asyncio.Event()),
                             b'metainfo')
        self.qb.fetch_metadata.assert_called_once_with(MAGNET,HASH,batch=False)
