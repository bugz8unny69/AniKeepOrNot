"""Built-in service adapters, with fake transports and a temporary database. No network."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from src.nextgen.acquisition.submission import (
    ClientAuthenticationFailed, ClientUnavailable, ClientUnsupported,
)
from src.nextgen.services import AUTH_FAILED, DISABLED, DOWN, NOT_CONFIGURED, UNKNOWN, UNSUPPORTED, UP
from src.nextgen.services.adapters.arr import ProwlarrAdapter, SonarrAdapter
from src.nextgen.services.adapters.media_servers import JellyfinAdapter, ShokoAdapter
from src.nextgen.services.adapters.metadata import AnidbAdapter, AnimeListsAdapter, TvdbAdapter
from src.nextgen.services.adapters.qbittorrent import QbittorrentAdapter


class Config:
    def __init__(self, **values):
        self.values = {
            'prowlarr.base_url': 'http://prowlarr:9696', 'prowlarr.timeout_seconds': 10,
            'sonarr.base_url': 'http://sonarr:8989', 'sonarr.timeout_seconds': 10,
            'jellyfin.base_url': '', 'jellyfin.timeout_seconds': 10,
            'shoko.base_url': '', 'shoko.timeout_seconds': 10,
            'qbittorrent.base_url': 'http://qb:8080', 'qbittorrent.category': 'anime',
            'qbittorrent.timeout_seconds': 10, 'storage.download_root': '/media/downloads',
            'storage.database_path': '', 'anidb.client_name': 'animediscovery',
        }
        self.values.update({k.replace('__', '.'): v for k, v in values.items()})

    def value(self, key):
        return self.values[key]


class Secrets:
    def __init__(self, missing=(), broken=False):
        self.missing, self.broken = set(missing), broken

    def _get(self, name, error):
        if self.broken:
            from src.nextgen.secrets import SecretStorageError
            raise SecretStorageError()
        if name in self.missing:
            raise error
        return 'k' * 32

    def prowlarr_api_key(self): return self._get('prowlarr', ValueError())
    def sonarr_api_key(self): return self._get('sonarr', ValueError())
    def jellyfin_api_key(self): return self._get('jellyfin', ValueError())
    def shoko_api_key(self): return self._get('shoko', ValueError())

    def tvdb_credentials(self):
        from src.nextgen.providers.errors import ProviderConfigurationError
        return self._get('tvdb', ProviderConfigurationError())

    def qbittorrent_credentials(self):
        from src.nextgen.acquisition.settings import DownloadCredentials
        self._get('qbittorrent', ValueError())
        return DownloadCredentials('user', 'pass')


class Response:
    def __init__(self, code, body):
        self.code, self.body = code, body

    def getcode(self): return self.code
    def read(self, n): return self.body[:n]
    def __enter__(self): return self
    def __exit__(self, *exc): return False


class Transport:
    """Records each request; answers with a status code and JSON, or raises."""
    def __init__(self, code=200, body=None, error=None):
        self.code, self.error, self.requests = code, error, []
        self.body = json.dumps(body if body is not None else {}).encode()

    def __call__(self, request, timeout):
        self.requests.append((request.full_url, dict(request.header_items()), timeout))
        if self.error is not None:
            raise self.error
        if self.code >= 400:
            raise urllib.error.HTTPError(request.full_url, self.code, 'x', {}, io.BytesIO(b''))
        return Response(self.code, self.body)


class ArrAdapterTests(unittest.TestCase):
    def test_prowlarr_up_reports_version_and_uses_the_key(self):
        transport = Transport(body={'version': '1.24.3'})
        result = ProwlarrAdapter().probe(Config(), Secrets(), transport=transport)
        self.assertEqual((result.status, result.detail), (UP, 'version 1.24.3'))
        url, headers, timeout = transport.requests[0]
        self.assertEqual(url, 'http://prowlarr:9696/api/v1/system/status')
        self.assertEqual((headers['X-api-key'], timeout), ('k' * 32, 10))

    def test_prowlarr_outcomes_are_descriptive(self):
        cases = (
            (Transport(code=401), AUTH_FAILED, 'HTTP 401; check the Prowlarr API key'),
            (Transport(code=503), DOWN, 'HTTP 503 at http://prowlarr:9696'),
            (Transport(error=urllib.error.URLError(ConnectionRefusedError())), DOWN,
             'could not connect (ConnectionRefusedError) at http://prowlarr:9696'),
        )
        for transport, status, detail in cases:
            with self.subTest(status=status, detail=detail):
                result = ProwlarrAdapter().probe(Config(), Secrets(), transport=transport)
                self.assertEqual((result.status, result.detail), (status, detail))
        down = ProwlarrAdapter().probe(Config(), Secrets(), transport=Transport(code=503))
        self.assertEqual(down.message('Prowlarr'), 'Prowlarr is unavailable: HTTP 503 at http://prowlarr:9696')

    def test_missing_or_unreadable_settings_never_reach_the_network(self):
        transport = Transport()
        # Prowlarr and Sonarr are optional: a blank address turns their feature off.
        blank = ProwlarrAdapter().probe(Config(prowlarr__base_url=''), Secrets(), transport=transport)
        self.assertEqual((blank.status, blank.detail), (DISABLED, 'no address is set, so automatic search is off'))
        self.assertEqual(blank.message('Prowlarr'),
                         'Prowlarr is turned off (optional): no address is set, so automatic search is off')
        bad = ProwlarrAdapter().probe(Config(prowlarr__base_url='ftp://x'), Secrets(), transport=transport)
        self.assertEqual((bad.status, bad.detail), (NOT_CONFIGURED, 'the Prowlarr address is not valid'))
        no_key = ProwlarrAdapter().probe(Config(), Secrets(missing={'prowlarr'}), transport=transport)
        self.assertEqual((no_key.status, no_key.detail), (NOT_CONFIGURED, 'set the Prowlarr API key in Settings'))
        unreadable = ProwlarrAdapter().probe(Config(), Secrets(broken=True), transport=transport)
        self.assertEqual(unreadable.status, UNKNOWN)
        self.assertIn('secrets.json', unreadable.detail)
        self.assertEqual(transport.requests, [])

    def test_sonarr_requires_version_4(self):
        old = SonarrAdapter().probe(Config(), Secrets(), transport=Transport(body={'version': '3.0.10'}))
        self.assertEqual(old.status, UNSUPPORTED)
        self.assertIn('Sonarr v4 is required', old.detail)
        new = Transport(body={'version': '4.0.9.2244'})
        self.assertEqual(SonarrAdapter().probe(Config(), Secrets(), transport=new).status, UP)
        self.assertTrue(new.requests[0][0].endswith('/api/v3/system/status'))

    def test_job_reason_codes_map_to_their_service(self):
        from src.nextgen.reasons import get_reason
        from src.nextgen.services import default_registry
        registry = default_registry()
        self.assertEqual(registry.get('qbittorrent').status_for_reason('DOWNLOAD_CLIENT_UNAVAILABLE'), DOWN)
        self.assertEqual(registry.get('qbittorrent').status_for_reason('DOWNLOAD_CLIENT_AUTH_FAILED'), AUTH_FAILED)
        self.assertEqual(registry.get('prowlarr').status_for_reason('RELEASE_SEARCH_TIMEOUT'), DOWN)
        self.assertEqual(registry.get('sonarr').status_for_reason('KEEP_SERVICE_UNAVAILABLE'), DOWN)
        self.assertIsNone(registry.get('sonarr').status_for_reason('KEEP_IMPORT_REVIEW'))
        # Every mapped code is a real, registered reason.
        for adapter in registry:
            for code in adapter.reasons:
                with self.subTest(service=adapter.name, code=code):
                    self.assertIsNotNone(get_reason(code))

    def test_client_errors_classify_without_a_probe(self):
        from src.nextgen.acquisition.prowlarr_search import SearchUnauthorized, SearchUnavailable
        from src.nextgen.sonarr import KeepUnavailable
        self.assertEqual(ProwlarrAdapter().classify(SearchUnauthorized()), AUTH_FAILED)
        self.assertEqual(ProwlarrAdapter().classify(SearchUnavailable()), DOWN)
        self.assertEqual(SonarrAdapter().classify(KeepUnavailable()), DOWN)
        self.assertIsNone(SonarrAdapter().classify(KeyError()))


class OptionalAdapterTests(unittest.TestCase):
    def test_blank_address_means_turned_off(self):
        transport = Transport()
        for adapter in (JellyfinAdapter(), ShokoAdapter()):
            with self.subTest(adapter=adapter.name):
                self.assertEqual(adapter.probe(Config(), Secrets(), transport=transport).status, DISABLED)
        self.assertEqual(transport.requests, [])

    def test_jellyfin_checks_an_authenticated_endpoint(self):
        transport = Transport(body={'Version': '10.10.3'})
        result = JellyfinAdapter().probe(Config(jellyfin__base_url='http://jf:8096'), Secrets(),
                                         transport=transport)
        self.assertEqual((result.status, result.detail), (UP, 'version 10.10.3'))
        url, headers, _ = transport.requests[0]
        self.assertEqual((url, headers['X-emby-token']), ('http://jf:8096/System/Info', 'k' * 32))

    def test_shoko_auth_failure_and_invalid_address(self):
        rejected = ShokoAdapter().probe(Config(shoko__base_url='http://shoko:8111'), Secrets(),
                                        transport=Transport(code=403))
        self.assertEqual(rejected.status, AUTH_FAILED)
        bad = ShokoAdapter().probe(Config(shoko__base_url='ftp://nope'), Secrets(), transport=Transport())
        self.assertEqual((bad.status, bad.detail), (NOT_CONFIGURED, 'the Shoko address is not valid'))


class FakeQb:
    behaviour = 'ok'

    def __init__(self, options, credentials):
        self.major, self.api_version = 5, (2, 11, 9)

    def login(self):
        if self.behaviour == 'auth': raise ClientAuthenticationFailed()
        if self.behaviour == 'down': raise ClientUnavailable()
        if self.behaviour == 'old': raise ClientUnsupported()

    @property
    def metadata_strategy(self):
        return 'magic' if self.behaviour == 'strategy' else 'fetch'


class QbittorrentAdapterTests(unittest.TestCase):
    def probe(self, behaviour, config=None):
        FakeQb.behaviour = behaviour
        return QbittorrentAdapter(client_factory=FakeQb).probe(config or Config(), Secrets())

    def test_outcomes(self):
        cases = {'ok': UP, 'auth': AUTH_FAILED, 'down': DOWN, 'old': UNSUPPORTED, 'strategy': UNSUPPORTED}
        for behaviour, status in cases.items():
            with self.subTest(behaviour=behaviour):
                self.assertEqual(self.probe(behaviour).status, status)
        self.assertEqual(self.probe('ok').detail, 'version 5, Web API 2.11.9')
        self.assertIn('username and password', self.probe('auth').detail)
        self.assertIn('http://qb:8080', self.probe('down').detail)

    def test_not_configured_without_contacting_qbittorrent(self):
        result = self.probe('down', Config(qbittorrent__base_url=''))
        self.assertEqual(result.status, NOT_CONFIGURED)


class MetadataAdapterTests(unittest.TestCase):
    def setUp(self):
        from src.nextgen.storage.sqlite import connect, initialize
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = os.path.join(temp.name, 'db.sqlite')
        db = connect(self.database)
        try:
            initialize(db)
        finally:
            db.close()
        self.config = Config(storage__database_path=self.database)
        self.now = 10 * 86400000

    def write(self, sql, args=()):
        from src.nextgen.storage.sqlite import connect
        db = connect(self.database)
        try:
            with db:
                db.execute(sql, args)
        finally:
            db.close()

    def probe(self, adapter, secrets=None):
        # Any attempt to reach the network fails the test.
        with patch('urllib.request.urlopen', side_effect=AssertionError('network used')), \
                patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network used')):
            return adapter.probe(self.config, secrets or Secrets(), now_ms=self.now)

    def test_never_contacts_the_provider(self):
        for adapter in (AnidbAdapter(), AnimeListsAdapter(), TvdbAdapter()):
            self.assertFalse(adapter.network_probe)
            self.assertEqual(self.probe(adapter).status, UNKNOWN)  # No requests recorded yet.

    def test_recent_failures_mean_down_with_the_cooldown(self):
        self.write("INSERT INTO provider_limits(provider,next_allowed_at_ms,failures,reservation_token) "
                   "VALUES('anidb',?,3,?)", (self.now + 20 * 60000, 'a' * 32))
        result = self.probe(AnidbAdapter())
        self.assertEqual((result.status, result.detail),
                         (DOWN, '3 recent failed request(s); next request allowed in 20 min'))

    def test_expired_cooldown_is_unknown_so_requests_can_recover_it(self):
        # Only a successful request resets the failure count. If an expired
        # cooldown still counted as down, gated jobs would never send that request.
        self.write("INSERT INTO provider_limits(provider,next_allowed_at_ms,failures,reservation_token) "
                   "VALUES('anidb',?,3,?)", (self.now - 60000, 'a' * 32))
        result = self.probe(AnidbAdapter())
        self.assertEqual(result.status, UNKNOWN)
        self.assertTrue(result.usable)
        self.assertIn('next request will show', result.detail)

    def test_cached_data_means_up_with_its_age(self):
        self.write("INSERT INTO provider_limits(provider,next_allowed_at_ms,failures,reservation_token) "
                   "VALUES('anidb_titles',0,0,?)", ('b' * 32,))
        self.write("INSERT INTO provider_public_cache(provider,cache_key,fetched_at_ms,expires_at_ms,payload) "
                   "VALUES('anidb_titles','titles',?,?,X'00')", (self.now - 3 * 3600000, self.now + 3600000))
        result = self.probe(AnidbAdapter())
        self.assertEqual((result.status, result.detail), (UP, '1 cached item(s), newest 3 h old'))

    def test_configuration_is_checked_first(self):
        result = AnidbAdapter().probe(Config(storage__database_path=self.database, anidb__client_name=''),
                                      Secrets(), now_ms=self.now)
        self.assertEqual(result.status, NOT_CONFIGURED)
        self.assertEqual(self.probe(TvdbAdapter(), Secrets(missing={'tvdb'})).status, NOT_CONFIGURED)

    def test_unreadable_database_is_unknown_not_down(self):
        result = AnidbAdapter().probe(Config(storage__database_path='/nonexistent/db.sqlite'), Secrets(),
                                      now_ms=self.now)
        self.assertEqual(result.status, UNKNOWN)


if __name__ == '__main__':
    unittest.main()
