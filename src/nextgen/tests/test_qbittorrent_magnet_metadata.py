"""Offline version-gated magnet metadata transport contracts (no network or CI)."""
import unittest
from unittest.mock import Mock, patch

from src.nextgen.acquisition.qbittorrent import Client, _Protocol
from src.nextgen.acquisition.submission import ClientInvalid, ClientUnsupported, SendPermit

HASH='a'*40
MAGNET='magnet:?xt=urn:btih:'+HASH


class MagnetMetadataTests(unittest.TestCase):
    def client(self,api_version,major=5):
        # Exercise the public transport methods without making HTTP requests.
        client=object.__new__(Client)
        client._sid='session'
        client.major=major
        client.api_version=api_version
        client.protocol=_Protocol(major,api_version)
        client.options=Mock(download_root='/media/downloads',category='anime')
        client._request=Mock()
        return client

    def test_metadata_poll_tuning_accepts_bounded_values(self):
        from types import SimpleNamespace
        from src.nextgen.acquisition.settings import DownloadOptions
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        options=DownloadOptions('http://localhost:8080',
                                '/downloads/ad-'+'c'*32,'anime',10)
        client=self.client((2,8,19),4)
        client.options=options
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN','f'*64)
        state=Observation(HASH,'pausedDL',options.download_root,
                          'anime',('ad-'+'c'*32,),False,())
        seen=[]
        with patch.object(client,'inspect',return_value=state):
            with patch.object(client,'stop_legacy_metadata',return_value=None):
                client.poll_legacy_metadata(intent,SimpleNamespace(is_set=lambda:False),
                                            attempts=3,interval=7,sleep=seen.append)
                with self.assertRaises(ClientInvalid):
                    client.poll_legacy_metadata(intent,SimpleNamespace(is_set=lambda:False),
                                                attempts=25,interval=7,sleep=seen.append)
        self.assertEqual(seen,[7,7])

    def test_status_poll_skips_file_list_until_due(self):
        import json
        client=self.client((2,8,19),4)
        row={'hash':HASH,'auto_tmm':False,'tags':'ad-'+'c'*32,
             'save_path':'/media/downloads','category':'anime','state':'metaDL'}
        client._request.return_value=(json.dumps([row]).encode(),{})
        value=client.inspect(HASH,include_files=False)
        self.assertEqual(value.files,())
        client._request.assert_called_once_with(
            'torrents/info',query={'hashes':HASH,'limit':2})

    def test_file_list_requires_indices_on_supported_api(self):
        import json
        client=self.client((2,8,19),4)
        row={'hash':HASH,'auto_tmm':False,'tags':'ad-'+'c'*32,
             'save_path':'/media/downloads','category':'anime','state':'pausedDL'}
        client._request.side_effect=[
            (json.dumps([row]).encode(),{}),
            (json.dumps([{'name':'episode.mkv','size':1024}]).encode(),{})]
        with self.assertRaises(ClientInvalid):
            client.inspect(HASH)

    def test_poll_limits_metadata_file_reads(self):
        from types import SimpleNamespace
        from src.nextgen.acquisition.settings import DownloadOptions
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        options=DownloadOptions('http://localhost:8080',
                                '/downloads/ad-'+'c'*32,'anime',10)
        client=self.client((2,8,19),4)
        client.options=options
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN','f'*64)
        state=Observation(HASH,'metaDL',options.download_root,
                          'anime',('ad-'+'c'*32,),False,())
        stopped=Observation(HASH,'pausedDL',options.download_root,
                            'anime',('ad-'+'c'*32,),False,())
        with patch.object(client,'inspect',side_effect=[stopped,state,state,state,state]) as read:
            with patch.object(client,'stop_legacy_metadata',return_value=None):
                client.poll_legacy_metadata(
                    intent,SimpleNamespace(is_set=lambda:False),
                    attempts=4,interval=0,sleep=lambda _:None)
        self.assertEqual(
            [c.kwargs.get('include_files',True) for c in read.call_args_list],
            [True,True,False,False,True])

    def test_version_selection_uses_web_api_not_app_major(self):
        self.assertEqual(self.client((2,11,9)).metadata_strategy,'fetch')
        self.assertEqual(self.client((2,8,15),4).metadata_strategy,'guarded_export')
        self.assertEqual(self.client((2,10,0),5).metadata_strategy,'guarded_export')
        with self.assertRaises(ClientUnsupported):
            self.client((2,8,13),4)
        with self.assertRaises(ClientUnsupported):
            self.client((2,5,1),4)

    def test_protocol_names_cover_submission_start_stop(self):
        old=_Protocol(4,(2,8,15))
        new=_Protocol(5,(2,11,9))
        self.assertEqual((old.stopped_parameter,old.start_endpoint,old.stop_endpoint),
                         ('paused','torrents/resume','torrents/pause'))
        self.assertEqual((new.stopped_parameter,new.start_endpoint,new.stop_endpoint),
                         ('stopped','torrents/start','torrents/stop'))

    def test_45_and_50_reuse_stopped_magnet_adapter(self):
        from src.nextgen.acquisition.settings import DownloadOptions
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        for major,version in ((4,(2,8,15)),(5,(2,10,0))):
            with self.subTest(major=major):
                client=self.client(version,major)
                options=DownloadOptions('http://localhost:8080',
                    '/downloads/ad-'+'c'*32,'anime',10)
                client.options=options
                intent=MagnetIntent(4,21,HASH,'c'*32,options,major,
                                    'UNCERTAIN','f'*64)
                client._request.return_value=(b'Ok.',{})
                client.add_legacy_magnet(MAGNET,intent,SendPermit('f'*64))
                sent=client._request.call_args_list
                self.assertEqual(len(sent),1)
                self.assertEqual(sent[0].args,('torrents/add',))
                self.assertEqual(sent[0].kwargs['form']['urls'],MAGNET)
                self.assertEqual(sent[0].kwargs['form']['category'],'anime')
                self.assertEqual(sent[0].kwargs['form']['tags'],'ad-'+'c'*32)
                self.assertEqual(sent[0].kwargs['form']['paused'],'true')

    def test_version_floor_rejects_pre_45_app_even_with_new_api(self):
        client=self.client((2,8,19),4)
        client._credentials=Mock(username='test',password='test')
        client._request.side_effect=[
            (b'Ok.',Mock(get_all=lambda *args:['SID=abcdef1234567890'])),
            (b'v4.4.5',{}),(b'2.8.19',{})]
        with self.assertRaises(ClientUnsupported):
            client.login()

    def test_legacy_probe_requires_proven_ownership_and_two_stable_reads(self):
        import json
        client=self.client((2,8,19),4)
        root='/media/downloads/ad-'+'c'*32
        client.options.download_root=root
        row={'hash':HASH,'auto_tmm':False,'tags':'ad-'+'c'*32,
             'save_path':root,'category':'anime','state':'pausedDL'}
        files=[{'index':0,'name':'episode.mkv','size':1024}]
        client._request.side_effect=[
            (json.dumps([row]).encode(),{}),(json.dumps(files).encode(),{}),
            (json.dumps([row]).encode(),{}),(json.dumps(files).encode(),{}),
        ]
        result=client.inspect_legacy_metadata(HASH,'c'*32,root)
        self.assertEqual(len(result.files),1)
        self.assertEqual(client._request.call_count,4)
        client._request.reset_mock()
        with self.assertRaises(ClientInvalid):
            client.inspect_legacy_metadata(HASH,'d'*32,root)

    def test_legacy_probe_rejects_changed_inventory(self):
        import json
        client=self.client((2,8,19),4)
        root='/media/downloads/ad-'+'c'*32
        client.options.download_root=root
        row={'hash':HASH,'auto_tmm':False,'tags':'ad-'+'c'*32,
             'save_path':root,'category':'anime','state':'pausedDL'}
        client._request.side_effect=[
            (json.dumps([row]).encode(),{}),
            (json.dumps([{'index':0,'name':'episode.mkv','size':1024}]).encode(),{}),
            (json.dumps([row]).encode(),{}),
            (json.dumps([{'index':0,'name':'different.mkv','size':1024}]).encode(),{}),
        ]
        with self.assertRaises(ClientInvalid):
            client.inspect_legacy_metadata(HASH,'c'*32,root)

    def test_45_metadata_poll_stops_after_bounded_resume(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.nextgen.acquisition.metadata import TorrentFile
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.acquisition.settings import DownloadOptions
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        client=self.client((2,8,19),4)
        options=DownloadOptions('http://localhost:8080',
                                '/downloads/ad-'+'c'*32,'anime',10)
        client.options=options
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN','f'*64)
        def observed(state,files=()):
            return Observation(HASH,state,options.download_root,'anime',
                               ('ad-'+'c'*32,),False,files)
        after_files=(TorrentFile(0,'Example.mkv',100),)
        with patch.object(client,'inspect',side_effect=[
                observed('pausedDL'),observed('metaDL'),
                observed('metaDL',after_files)]) as reads:
            with patch.object(client,'stop_legacy_metadata',
                              return_value='stopped') as stop:
                result=client.poll_legacy_metadata(
                    intent,SimpleNamespace(is_set=lambda:False),
                    attempts=3,interval=0,sleep=lambda _:None)
        self.assertEqual(result,'stopped')
        self.assertEqual(reads.call_count,3)
        self.assertEqual(client._request.call_args.args,('torrents/resume',))
        stop.assert_called_once_with(intent)

    def test_45_default_metadata_window_waits_beyond_two_seconds(self):
        from types import SimpleNamespace
        from src.nextgen.acquisition.metadata import TorrentFile
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.acquisition.settings import DownloadOptions
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        client=self.client((2,8,19),4)
        options=DownloadOptions('http://localhost:8080',
                                '/downloads/ad-'+'c'*32,'anime',10)
        client.options=options
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN','f'*64)
        def observation(state,files=()):
            return Observation(HASH,state,options.download_root,'anime',
                               ('ad-'+'c'*32,),False,files)
        # Early metadata polls are empty; the same original torrent eventually
        # provides its file list. Simulate elapsed time without sleeping.
        snapshots=[observation('pausedDL')]+[observation('metaDL')]*9+[
            observation('metaDL',(TorrentFile(0,'Episode.mkv',100),))]
        with patch.object(client,'inspect',side_effect=snapshots) as inspected:
            with patch.object(client,'stop_legacy_metadata',
                              return_value='stopped') as stopped:
                result=client.poll_legacy_metadata(
                    intent,SimpleNamespace(is_set=lambda:False),
                    sleep=lambda _:None)
        self.assertEqual(result,'stopped')
        self.assertEqual(inspected.call_count,11)
        stopped.assert_called_once_with(intent)
        self.assertEqual([call.args[0] for call in client._request.call_args_list],
                         ['torrents/resume'])

    def test_metadata_poll_after_upgrade_uses_current_52_stop_start(self):
        from types import SimpleNamespace
        from src.nextgen.acquisition.settings import DownloadOptions
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        client=self.client((2,11,9),5)
        options=DownloadOptions('http://localhost:8080',
                                '/downloads/ad-'+'c'*32,'anime',10)
        client.options=options
        # The magnet was originally admitted on 4.5, not sent again.
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,
                            'UNCERTAIN','f'*64)
        stopped=Observation(HASH,'pausedDL',options.download_root,
                            'anime',('ad-'+'c'*32,),False,())
        pending=Observation(HASH,'metaDL',options.download_root,
                            'anime',('ad-'+'c'*32,),False,())
        with patch.object(client,'inspect',side_effect=[stopped,pending]):
            with patch.object(client,'stop_legacy_metadata',
                              return_value=None) as stop:
                client.poll_legacy_metadata(
                    intent,SimpleNamespace(is_set=lambda:False),
                    attempts=1,interval=0,sleep=lambda _:None)
        client._request.assert_called_once_with(
            'torrents/start',form={'hashes':HASH},limit=4096)
        stop.assert_called_once_with(intent)

    def test_45_metadata_poll_refuses_foreign_marker(self):
        from types import SimpleNamespace
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.acquisition.settings import DownloadOptions
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        client=self.client((2,8,19),4)
        options=DownloadOptions('http://localhost:8080',
                                '/downloads/ad-'+'c'*32,'anime',10)
        client.options=options
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN','f'*64)
        with patch.object(client,'inspect',return_value=Observation(
                HASH,'pausedDL',options.download_root,'anime',
                ('ad-'+'d'*32,),False,())):
            with self.assertRaises(ClientInvalid):
                client.poll_legacy_metadata(
                    intent,SimpleNamespace(is_set=lambda:False))
        client._request.assert_not_called()

    def test_protocol_adapter_accepts_hash_verified_multifile_batch(self):
        from src.nextgen.tests.test_acquisition_metadata import metadata
        from src.nextgen.acquisition.metadata import BatchInventory
        files = [
            {b'length': 100, b'path': [b'S01E01.mkv']},
            {b'length': 120, b'path': [b'S01E02.mkv']},
        ]
        raw, digest = metadata(name=b'Season One', files=files)
        client = self.client((2,11,9))
        client._request.side_effect = [
            (b'{"infohash_v1":"' + digest.encode() + b'","info":{}}', {}),
            (raw, {}),
        ]
        inventory, received = client.fetch_metadata(digest, digest, batch=True)
        self.assertIs(type(inventory), BatchInventory)
        self.assertEqual(received, raw)
        self.assertEqual(len(inventory.files), 2)
        self.assertEqual(client._request.call_count, 2)
        client._request.reset_mock()
        client._request.side_effect = [
            (b'{"infohash_v1":"' + digest.encode() + b'","info":{}}', {}),
            (raw, {}),
        ]
        from src.nextgen.acquisition.metadata import MetadataInvalid
        with self.assertRaises(MetadataInvalid):
            client.fetch_metadata(digest, digest)
        self.assertEqual(client._request.call_count, 2)

    def test_export_adapter_supports_batch_on_older_api_when_present(self):
        from src.nextgen.tests.test_acquisition_metadata import metadata
        from src.nextgen.acquisition.metadata import BatchInventory
        files = [
            {b'length': 100, b'path': [b'S01E01.mkv']},
            {b'length': 120, b'path': [b'S01E02.mkv']},
        ]
        raw, digest = metadata(name=b'Season One', files=files)
        for major, version in ((4, (2,8,15)), (5, (2,10,0))):
            with self.subTest(major=major, api=version):
                client = self.client(version, major)
                client._request.return_value = (raw, {})
                inventory, content = client.export_metainfo(digest, batch=True)
                self.assertIs(type(inventory), BatchInventory)
                self.assertEqual(content, raw)
                client._request.assert_called_once_with(
                    'torrents/export', query={'hash':digest}, limit=4194304)

    def test_metadata_fetch_is_hash_bound_and_pending_is_not_failure(self):
        client=self.client((2,11,9))
        client._request.return_value=(b'{}',{})
        self.assertIsNone(client.fetch_metadata(MAGNET,HASH))
        client._request.assert_called_once_with(
            'torrents/fetchMetadata',form={'source':MAGNET},limit=4194304)
        client._request.reset_mock()
        with self.assertRaises(ClientInvalid):
            client.fetch_metadata('magnet:?xt=urn:btih:'+'b'*40,HASH)
        client._request.assert_not_called()

    def test_transport_accepts_202_only_for_metadata_polling(self):
        class Response:
            headers={}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def getcode(self):return 202
            def read(self,size):return b'{"infohash_v1":"'+HASH.encode()+b'"}'
        client=self.client((2,11,9))
        client.options.base_url='http://localhost:8080'
        client.options.timeout_seconds=5
        del client._request  # Exercise the real HTTP adapter, not the helper mock.
        client._transport=lambda request,timeout:Response()
        self.assertIsNone(client.fetch_metadata(HASH,HASH))

    def test_save_metadata_conflict_remains_pending(self):
        import io
        import urllib.error
        class Response:
            headers={}
            def __init__(self,code,raw):
                self.code,self.raw=code,raw
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def getcode(self):return self.code
            def read(self,size):return self.raw
        client=self.client((2,11,9))
        client.options.base_url='http://localhost:8080'
        client.options.timeout_seconds=5
        requests=[]
        def transport(request,timeout):
            requests.append(request.full_url)
            if request.full_url.endswith('saveMetadata'):
                raise urllib.error.HTTPError(request.full_url,409,'pending',{},io.BytesIO())
            return Response(200,b'{"infohash_v1":"'+HASH.encode()+b'","info":{}}')
        del client._request  # Exercise the real HTTP adapter, including HTTP 409.
        client._transport=transport
        self.assertIsNone(client.fetch_metadata(HASH,HASH))
        self.assertEqual(len(requests),2)

    def test_hash_only_response_is_pending_not_ready(self):
        client=self.client((2,11,9))
        client._request.return_value=(b'{"infohash_v1":"'+HASH.encode()+b'"}',{})
        self.assertIsNone(client.fetch_metadata(HASH,HASH))
        client._request.assert_called_once_with(
            'torrents/fetchMetadata',form={'source':HASH},limit=4194304)

    def test_metadata_hash_mismatch_does_not_export_or_authorize(self):
        client=self.client((2,11,9))
        client._request.return_value=(b'{"infohash_v1":"'+b'b'*40+b'"}',{})
        with self.assertRaises(ClientInvalid):
            client.fetch_metadata(MAGNET,HASH)
        client._request.assert_called_once()

    def test_magnet_submission_consumes_original_single_use_permit(self):
        client=self.client((2,11,9))
        client._request.return_value=(b'Ok.',{})
        intent=Mock(options=client.options,api_major=5,fingerprint='f'*64)
        intent.plan.manifest.info_hash=HASH
        intent.plan.marker='c'*32
        permit=SendPermit('f'*64)
        client.add_stopped(MAGNET,intent,permit)
        self.assertEqual(client._request.call_args.args,('torrents/add',))
        self.assertEqual(client._request.call_args.kwargs['form']['stopped'],'true')
        self.assertEqual(client._request.call_args.kwargs['form']['urls'],MAGNET)
        with self.assertRaises(ClientInvalid):
            client.add_stopped(MAGNET,intent,permit)
        client._request.assert_called_once()

    def test_metadata_poll_records_pre_stop_state_and_empty_inventory(self):
        from types import SimpleNamespace
        from src.nextgen.acquisition.settings import DownloadOptions
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.storage.batch_metadata_admission import EarlyBatchIntent
        from src.nextgen.acquisition.metadata_evidence import fresh
        options=DownloadOptions('http://localhost:8080',
                                '/downloads/ad-'+'c'*32,'anime',10)
        client=self.client((2,8,19),4)
        client.options=options
        intent=EarlyBatchIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN')
        def snapshot(state):
            return Observation(HASH,state,options.download_root,'anime',
                               ('ad-'+'c'*32,),False,())
        trace=fresh('poll')
        with patch.object(client,'inspect',side_effect=[
                snapshot('pausedDL'),snapshot('metaDL'),snapshot('metaDL')]):
            with patch.object(client,'stop_legacy_metadata',
                              return_value=snapshot('pausedDL')):
                observed=client.poll_legacy_metadata(
                    intent,SimpleNamespace(is_set=lambda:False),
                    attempts=2,interval=0,sleep=lambda _:None,trace=trace)
        self.assertEqual(observed.state,'pausedDL')
        self.assertEqual(trace,{
            'phase':'poll','initial_state':'pausedDL',
            'last_active_state':'metaDL','stopped_state':'pausedDL',
            'polls':2,'max_files':0,'resumed':True,
        })

    def test_same_submission_interface_uses_legacy_paused_parameter(self):
        client=self.client((2,8,15),major=4)
        intent=Mock(options=client.options,api_major=4,fingerprint='f'*64)
        intent.plan.manifest.info_hash=HASH
        intent.plan.marker='c'*32
        client.add_stopped(MAGNET,intent,SendPermit('f'*64))
        args,kwargs=client._request.call_args
        self.assertEqual(args,('torrents/add',))
        self.assertEqual(kwargs['form']['paused'],'true')
        self.assertEqual(kwargs['form']['urls'],MAGNET)
        self.assertNotIn('stopCondition',kwargs['form'])
