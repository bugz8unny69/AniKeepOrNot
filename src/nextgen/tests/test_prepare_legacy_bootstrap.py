"""Offline routing and recovery contracts for opted-in legacy magnet intake."""
import logging
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.nextgen.acquisition.metainfo_source import MetadataSourceConfigurationError
from src.nextgen.acquisition.legacy_magnet import make_bootstrap
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.acquisition.submission import Observation
from src.nextgen.job_contracts import Outcome
from src.nextgen.jobs.prepare_acquisition_job import registration
from src.nextgen.storage.job_queue import Claim
from src.nextgen.storage.magnet_intake import MagnetIntent, MagnetPermit
from src.nextgen.episode_identity import VerifiedEpisode

HASH='a'*40
MAGNET='magnet:?xt=urn:btih:'+HASH
CLAIM=Claim(21,4,'PREPARE_ACQUISITION','{"evaluation_job_id":3}',1,'lease')


class LegacyWorkerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_callback_used_only_when_normal_reader_waits(self):
        async def missing(source,configuration,cancelled):
            raise MetadataSourceConfigurationError()
        async def legacy(source,context):
            return Outcome('defer','TORRENT_METADATA_WAITING',delay_ms=60000)
        spec=registration(missing,legacy_bootstrap=legacy)
        context=SimpleNamespace(
            data=lambda:{'evaluation_job_id':3},claim=CLAIM,
            configuration=SimpleNamespace(value=lambda key:'/unused'),
            cancelled=asyncio.Event())
        with patch('src.nextgen.jobs.prepare_acquisition_job.load_acquisition_input',
                   return_value=SimpleNamespace(info_hash=HASH)):
            outcome=await spec.execute(context)
        self.assertEqual((outcome.kind,outcome.reason_code),
                         ('defer','TORRENT_METADATA_WAITING'))

    async def test_without_opt_in_preserves_existing_error(self):
        async def missing(source,configuration,cancelled):
            raise MetadataSourceConfigurationError()
        spec=registration(missing)
        context=SimpleNamespace(
            data=lambda:{'evaluation_job_id':3},claim=CLAIM,
            configuration=SimpleNamespace(value=lambda key:'/unused'),
            cancelled=asyncio.Event())
        with patch('src.nextgen.jobs.prepare_acquisition_job.load_acquisition_input',
                   return_value=SimpleNamespace(info_hash=HASH)):
            outcome=await spec.execute(context)
        self.assertEqual((outcome.kind,outcome.reason_code),
                         ('review','TORRENT_METADATA_CONFIGURATION_INVALID'))


class LegacyBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config=SimpleNamespace(value=lambda key:{
            'storage.database_path':'unused.sqlite',
            'qbittorrent.base_url':'http://localhost:8080',
            'storage.download_root':'/downloads',
            'qbittorrent.category':'anime',
            'qbittorrent.timeout_seconds':10,
            'qbittorrent.metadata_poll_seconds':5,
            'qbittorrent.metadata_poll_attempts':24,
        }[key])
        self.context=SimpleNamespace(
            claim=CLAIM,configuration=self.config,cancelled=asyncio.Event())
        self.source=SimpleNamespace(event_id=4,info_hash=HASH)
        self.secrets=SimpleNamespace(qbittorrent_credentials=lambda:Mock())
        self.identity=VerifiedEpisode(100,200,1,10)

    def _config(self,**extra):
        values={'storage.database_path':'unused.sqlite',
                'qbittorrent.base_url':'http://localhost:8080',
                'storage.download_root':'/downloads',
                'qbittorrent.category':'anime',
                'qbittorrent.timeout_seconds':10,
                'qbittorrent.metadata_poll_seconds':5,
                'qbittorrent.metadata_poll_attempts':24,
                'torrent_metadata.base_url':'',**extra}
        return SimpleNamespace(claim=CLAIM,configuration=SimpleNamespace(value=values.__getitem__),
                               cancelled=asyncio.Event(),
                               logger=logging.getLogger('anime-discovery.test.bootstrap'))

    async def test_webhook_event_without_torrent_source_admits_its_own_magnet(self):
        """Autobrr intake: no .torrent URL; Legacy magnet admission."""
        root='/downloads/ad-'+'d'*32
        options=DownloadOptions('http://localhost:8080',root,'anime',10)
        from src.nextgen.storage.magnet_intake import _fingerprint
        intent=MagnetIntent(4,21,HASH,'d'*32,options,4,'UNCERTAIN',
                            _fingerprint(4,HASH,'d'*32,options,4))
        probe=Mock(major=4,metadata_strategy='guarded_export')
        probe.inspect.return_value=None
        owned=Mock()
        owned.inspect.return_value=None
        journal=Mock()
        journal.existing.return_value=None
        journal.authorize.return_value=(intent,MagnetPermit(intent.fingerprint),())
        journal.metadata_expired.return_value=False
        cb=make_bootstrap(self.secrets,client_factory=Mock(side_effect=[probe,owned]),
                          journal_factory=lambda _:journal)
        with patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity',
                   return_value=None), \
             patch('src.nextgen.acquisition.legacy_magnet.source_magnet',
                   return_value=MAGNET+'&tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce'):
            outcome=await cb(self.source,self._config())
        self.assertEqual((outcome.kind,outcome.reason_code),('defer','TORRENT_METADATA_WAITING'))
        owned.add_legacy_magnet.assert_called_once()
        self.assertTrue(owned.add_legacy_magnet.call_args.args[0].startswith(MAGNET))

    async def test_qbittorrent_failures_report_the_failed_check(self):
        from src.nextgen.acquisition.submission import ClientInvalid, ClientAuthenticationFailed
        for error,reason in ((ClientInvalid,'DOWNLOAD_CLIENT_RESPONSE_INVALID'),
                             (ClientAuthenticationFailed,'DOWNLOAD_CLIENT_AUTH_FAILED')):
            probe=Mock(major=4,metadata_strategy='guarded_export')
            probe.inspect.side_effect=error()
            journal=Mock()
            journal.existing.return_value=None
            cb=make_bootstrap(self.secrets,client_factory=Mock(return_value=probe),
                              journal_factory=lambda _:journal)
            with self.subTest(reason=reason), \
                 patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity',return_value=None), \
                 patch('src.nextgen.acquisition.legacy_magnet.source_magnet',return_value=MAGNET):
                with self.assertLogs('anime-discovery.test.bootstrap','WARNING') as logs:
                    outcome=await cb(self.source,self._config())
                self.assertEqual((outcome.kind,outcome.reason_code),('review',reason))
                journal.authorize.assert_not_called()
                # Which call failed is recorded as line numbers only, never secrets.
                self.assertIn('error=',logs.output[0])
                self.assertIn('reason_code='+reason,logs.output[0])
                self.assertNotIn('SID',logs.output[0])

    async def test_metadata_failure_logs_its_location_without_resubmitting(self):
        from src.nextgen.acquisition.metadata import MetadataInvalid
        probe=Mock(major=4,metadata_strategy='guarded_export')
        probe.inspect.side_effect=MetadataInvalid()
        journal=Mock()
        journal.existing.return_value=None
        cb=make_bootstrap(self.secrets,client_factory=Mock(return_value=probe),
                          journal_factory=lambda _:journal)
        with patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity',return_value=None), \
             patch('src.nextgen.acquisition.legacy_magnet.source_magnet',return_value=MAGNET), \
             self.assertLogs('anime-discovery.test.bootstrap','WARNING') as logs:
            outcome=await cb(self.source,self._config())
        self.assertEqual((outcome.kind,outcome.reason_code),('fail',MetadataInvalid.reason_code))
        self.assertIn('error=',logs.output[0])
        self.assertIn('reason_code='+MetadataInvalid.reason_code,logs.output[0])
        probe.add_legacy_magnet.assert_not_called()

    async def test_existing_torrent_reports_duplicate_without_another_add(self):
        from src.nextgen.storage.magnet_intake import _fingerprint
        from src.nextgen.reasons import get_reason
        options=DownloadOptions('http://localhost:8080','/downloads/ad-'+'c'*32,'anime',10)
        for recovered in (False,True):
            with self.subTest(recovered=recovered):
                intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'EXTERNAL',
                                   _fingerprint(4,HASH,'c'*32,options,4))
                probe=Mock(major=4,metadata_strategy='guarded_export')
                journal=Mock()
                journal.existing.return_value=intent if recovered else None
                journal.authorize.return_value=(intent,None,())
                factory=Mock(return_value=probe)
                cb=make_bootstrap(self.secrets,client_factory=factory,journal_factory=lambda _:journal)
                with patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity',return_value=None), \
                     patch('src.nextgen.acquisition.legacy_magnet.source_magnet',return_value=MAGNET):
                    outcome=await cb(self.source,self._config())
                self.assertEqual((outcome.kind,outcome.reason_code),('review','TORRENT_ALREADY_PRESENT'))
                self.assertIn('already exists in qBittorrent',get_reason(outcome.reason_code).message)
                probe.add_legacy_magnet.assert_not_called()
                self.assertEqual(factory.call_count,1)

    async def test_webhook_event_on_fetch_client_defers_to_metadata_reader(self):
        probe=Mock(major=5,metadata_strategy='fetch')
        journal=Mock()
        journal.existing.return_value=None
        cb=make_bootstrap(self.secrets,client_factory=Mock(return_value=probe),
                          journal_factory=lambda _:journal)
        with patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity',
                   return_value=None), \
             patch('src.nextgen.acquisition.legacy_magnet.source_magnet',return_value=MAGNET):
            self.assertIsNone(await cb(self.source,self._config()))
        journal.authorize.assert_not_called()

    async def test_restart_recovers_admitted_torrent_without_new_search(self):
        from src.nextgen.storage.magnet_intake import _fingerprint
        root='/downloads/ad-'+'c'*32
        options=DownloadOptions('http://localhost:8080',root,'anime',10)
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN',
                            _fingerprint(4,HASH,'c'*32,options,4))
        probe=Mock(major=4,metadata_strategy='guarded_export')
        owned=Mock()
        owned.inspect.return_value=Observation(
            HASH,'pausedDL',root,'anime',('ad-'+'c'*32,),False,())
        owned.poll_legacy_metadata.return_value=None
        journal=Mock()
        journal.metadata_expired.return_value=False
        journal.existing.return_value=intent
        journal.reconcile.return_value='UNCERTAIN'
        factory=Mock(side_effect=[probe,owned])
        cb=make_bootstrap(self.secrets,client_factory=factory,
                          journal_factory=lambda db:journal)
        with patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity',
                   return_value=self.identity):
            result=await cb(self.source,self.context)
        self.assertEqual(result.kind,'defer')
        journal.authorize.assert_not_called()
        owned.add_legacy_magnet.assert_not_called()
        owned.poll_legacy_metadata.assert_called_once()

    async def test_upgrade_to_52_recovers_original_magnet_before_fetch(self):
        from src.nextgen.storage.magnet_intake import _fingerprint
        root='/downloads/ad-'+'c'*32
        options=DownloadOptions('http://localhost:8080',root,'anime',10)
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN',
                            _fingerprint(4,HASH,'c'*32,options,4))
        probe=Mock(major=5,metadata_strategy='fetch')
        owned=Mock()
        owned.inspect.return_value=Observation(
            HASH,'pausedDL',root,'anime',('ad-'+'c'*32,),False,())
        owned.poll_legacy_metadata.return_value=None
        journal=Mock()
        journal.metadata_expired.return_value=False
        journal.existing.return_value=intent
        journal.reconcile.return_value='UNCERTAIN'
        factory=Mock(side_effect=[probe,owned])
        cb=make_bootstrap(self.secrets,client_factory=factory,
                          journal_factory=lambda db:journal)
        with patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity',
                   return_value=self.identity):
            result=await cb(self.source,self.context)
        self.assertEqual(result.kind,'defer')
        owned.poll_legacy_metadata.assert_called_once()
        journal.authorize.assert_not_called()
        owned.add_legacy_magnet.assert_not_called()

    async def test_42_owned_metadata_defers_without_false_manifest(self):
        from src.nextgen.storage.magnet_intake import _fingerprint
        root='/downloads/ad-'+'c'*32
        options=DownloadOptions('http://localhost:8080',root,'anime',10)
        intent=MagnetIntent(4,21,HASH,'c'*32,options,4,'UNCERTAIN',
                            _fingerprint(4,HASH,'c'*32,options,4))
        probe=Mock(major=4,metadata_strategy='guarded_export')
        probe.inspect.return_value=None
        owned=Mock()
        observation=Observation(HASH,'pausedDL',root,'anime',
                                ('ad-'+'c'*32,),False,())
        owned.inspect.return_value=observation
        owned.poll_legacy_metadata.return_value=None
        journal=Mock()
        journal.metadata_expired.return_value=False
        journal.existing.return_value=None
        journal.authorize.return_value=(intent,MagnetPermit(intent.fingerprint),())
        journal.reconcile.return_value='UNCERTAIN'
        factory=Mock(side_effect=[probe,owned])
        cb=make_bootstrap(self.secrets,client_factory=factory,
                          journal_factory=lambda db:journal)
        with patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity',
                   return_value=self.identity), \
             patch('src.nextgen.acquisition.legacy_magnet.source_magnet',return_value=MAGNET):
            result=await cb(self.source,self.context)
        self.assertEqual((result.kind,result.reason_code),
                         ('defer','TORRENT_METADATA_WAITING'))
        journal.guard.assert_called_once()
        owned.add_legacy_magnet.assert_called_once_with(
            MAGNET,intent,journal.authorize.return_value[1])
        owned.poll_legacy_metadata.assert_called_once()
        journal.reconcile.assert_called_once()
