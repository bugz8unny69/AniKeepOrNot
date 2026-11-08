"""Offline batch reader compatibility across old qBittorrent protocol modes."""
import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from src.anime.acquisition.metadata import (
    ObservedBatchInventory, parse_batch_metainfo,
)
from src.anime.acquisition.metainfo_source import make_batch_reader
from src.anime.acquisition.settings import DownloadOptions, download_options
from src.anime.acquisition.submission import Observation
from src.anime.configuration_schema import ConfigurationSnapshot
from src.anime.storage.batch_metadata_admission import (
    EarlyBatchIntent, EarlyBatchPermit,
)
from src.anime.tests.test_acquisition_metadata import metadata


class LegacyBatchReaderTests(IsolatedAsyncioTestCase):
    async def test_old_qb_one_send_and_replay_without_second_submission(self):
        files=[{b'length':100,b'path':[b'S01E01.mkv']},
               {b'length':150,b'path':[b'S01E02.mkv']}]
        raw,digest=metadata(name=b'Show',files=files)
        verified=parse_batch_metainfo(raw,digest)
        stored=ObservedBatchInventory(digest,verified.files)
        config=ConfigurationSnapshot(1,{
            'qbittorrent':{'base_url':'http://localhost:8080'},
            'storage':{'download_root':'/downloads'},
        })
        opts=download_options(config)
        marker='b'*32
        target_opts=DownloadOptions(opts.base_url,opts.download_root+'/ad-'+marker,
                                    opts.category,opts.timeout_seconds)
        observations=Observation(digest,'pausedDL',target_opts.download_root,
                                 target_opts.category,('ad-'+marker,),False,stored.files)
        for major,strategy in ((4,'legacy_poll'),(4,'guarded_export'),
                               (5,'guarded_export')):
            with self.subTest(major=major,strategy=strategy):
                sends=[]
                early=EarlyBatchIntent(4,7,digest,marker,target_opts,major,'UNCERTAIN')
                owned=EarlyBatchIntent(4,7,digest,marker,target_opts,major,
                                       'OWNED_STOPPED',stored.json())

                class Probe:
                    metadata_strategy=strategy
                    def __init__(self,*args):self.major=major
                    def login(self):pass
                    def inspect(self,digest):return None

                class Target:
                    metadata_strategy=strategy
                    def __init__(self,*args):pass
                    def login(self):pass
                    def add_early_batch_magnet(self,magnet,intent,permit):
                        permit.take(intent)
                        sends.append(('add',intent.info_hash))
                    def inspect(self,digest):return observations
                    def inspect_legacy_metadata(self,*args):return observations
                    def poll_legacy_metadata(self,*args):return observations
                    def stop_legacy_metadata(self,*args):return observations
                    def inspect_owned_batch_metadata(self,*args,**kwargs):
                        return stored
                    def export_metainfo(self,*args,**kwargs):
                        return verified,raw

                class Journal:
                    def __init__(self,*args):pass
                    def authorize(self,*args):return early,EarlyBatchPermit(4,marker)
                    def guard(self,*args):pass
                    def observe(self,*args):return owned

                reader=make_batch_reader(client_factory=Probe,secrets=type(
                    'Secrets',(),{'qbittorrent_credentials':lambda self:object()})())
                with patch('src.anime.storage.batch_metadata_admission.lookup',
                           side_effect=[(None,None),(owned,stored),(owned,stored)]),patch(
                           'src.anime.storage.batch_metadata_admission.BatchMetadataJournal',
                           Journal),patch('src.anime.batch.qb_client.BatchClient',Target),patch(
                           'src.anime.acquisition.metainfo_source._event_magnet',
                           return_value='magnet:?xt=urn:btih:'+digest):
                    first=await reader(digest,config,asyncio.Event(),
                                       claim=object(),event_id=4)
                    second=await reader(digest,config,asyncio.Event())

                if strategy=='legacy_poll':
                    self.assertIs(type(first),ObservedBatchInventory)
                    self.assertIsNone(first.info_sha256)
                    self.assertEqual(first,second)
                else:
                    self.assertEqual(first,raw)
                    self.assertEqual(second,raw)
                self.assertEqual(sends,[('add',digest)])
