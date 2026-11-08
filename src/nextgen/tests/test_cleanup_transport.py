"""Offline exact cleanup controls and one-use permits; unexecuted."""
from dataclasses import replace
import unittest
from unittest.mock import Mock
from src.nextgen.acquisition.cleanup import CleanupPermit
from src.nextgen.torrent_client.qbittorrent import Client
from src.nextgen.torrent_client.qbittorrent_versions import select_adapter
from src.nextgen.acquisition.settings import DownloadCredentials
from src.nextgen.acquisition.submission import ClientInvalid
from src.nextgen.cancellation import CancellationError
from src.nextgen.tests.test_transfer_policy import example


class CleanupTransportTests(unittest.TestCase):
    def test_version_specific_stop_and_owned_torrent_payload_deletion(self):
        intent,snapshot=example()
        for major,api,endpoint in ((4,(2,8,19),'torrents/pause'),
                                   (5,(2,10,0),'torrents/pause'),
                                   (5,(2,11,0),'torrents/stop')):
            client=Client(intent.options,DownloadCredentials('user','pass'))
            client.major=major;client._sid='offline-session'
            client.protocol=select_adapter(major,api)
            client.inspect=Mock(return_value=snapshot.observation)
            client._request=Mock(return_value=(b'',None))
            permit=CleanupPermit(intent.fingerprint,'stop')
            client.cleanup_control(intent,intent.fingerprint,permit,'stop')
            client._request.assert_called_once_with(endpoint,form={'hashes':intent.plan.manifest.info_hash},limit=4096)
            with self.assertRaises(CancellationError):client.cleanup_control(intent,intent.fingerprint,permit,'stop')
            client._request.reset_mock()
            client.cleanup_control(intent,intent.fingerprint,CleanupPermit(intent.fingerprint,'remove'),'remove')
            client._request.assert_called_once_with('torrents/delete',form={'hashes':intent.plan.manifest.info_hash,'deleteFiles':'true'},limit=4096)
            client._request.reset_mock()
            client.inspect=Mock(return_value=replace(snapshot.observation,state='downloading'))
            with self.assertRaises(ClientInvalid):
                client.cleanup_control(intent,intent.fingerprint,CleanupPermit(intent.fingerprint,'remove'),'remove')
            client._request.assert_not_called()
