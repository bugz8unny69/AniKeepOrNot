"""Phase 4: required services at startup, the AniDB/TVDB startup checks and shared status."""
import io
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import Outcome, Policy, Registration, Registry
from src.nextgen.services import (
    AUTH_FAILED, DISABLED, DOWN, NOT_CONFIGURED, UNKNOWN, UP, ServiceAdapter, ServiceHealth, ServiceRegistry,
    status_store,
)
from src.nextgen.services.adapters.metadata import AnidbAdapter, TvdbAdapter
from src.nextgen.services.startup import EXIT_CODE, StartupRefused, refusals
from src.nextgen.storage.sqlite import initialize
from src.nextgen.worker import Worker

ANIME_1 = (b'<anime id="1"><titles><title type="main">Seikai no Monshou</title></titles>'
           b'<episodes><episode id="1"><epno type="1">1</epno></episode></episodes></anime>')


def now_ms():
    return time.time_ns() // 1000000


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.headers = payload, status, {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.payload[:limit]


class Network:
    """Records every request; answers from a queue. An empty queue fails the test."""
    def __init__(self, test):
        self.test, self.requests, self.answers = test, [], []

    def __call__(self, request, timeout):
        self.requests.append(request.full_url)
        if not self.answers:
            self.test.fail(f'unexpected request to {request.full_url}')
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Config:
    def __init__(self, database, **values):
        self.values = {'storage.database_path': str(database), 'anidb.client_name': 'animediscovery',
                       'anidb.client_version': 1, 'providers.timeout_seconds': 10, **values}

    def value(self, key):
        return self.values[key]


class Secrets:
    def __init__(self, key='tvdb-key'):
        self.key = key

    def tvdb_credentials(self):
        from src.nextgen.providers.errors import ProviderConfigurationError
        from src.nextgen.providers.http import Credentials
        if not self.key:
            raise ProviderConfigurationError()
        return Credentials(self.key)

    def status(self):
        return SimpleNamespace(revision=1)


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / 'q.sqlite'
        db = sqlite3.connect(self.database)
        try:
            initialize(db)
        finally:
            db.close()
        self.network = Network(self)

    def sql(self, statement, args=()):
        db = sqlite3.connect(self.database)
        try:
            with db:
                return db.execute(statement, args).fetchall()
        finally:
            db.close()


class AnidbStartupTests(DatabaseCase):
    def check(self, previous=None, **values):
        return AnidbAdapter(opener=self.network).startup_probe(
            Config(self.database, **values), None, now_ms=now_ms(), previous=previous)

    def test_a_working_client_is_confirmed_once_then_reused_for_a_day(self):
        self.network.answers.append(Response(ANIME_1))
        result = self.check()
        self.assertEqual((result.status, result.detail), (UP, 'a test request succeeded'))
        self.assertEqual(len(self.network.requests), 1)
        self.assertIn('client=animediscovery', self.network.requests[0])
        # A restart sends nothing: the cached answer proves the setup.
        again = self.check()
        self.assertEqual(again.status, UP)
        self.assertTrue(again.detail.startswith('a request succeeded'))
        self.assertEqual(len(self.network.requests), 1)

    def test_a_rejected_client_stops_startup_and_starts_a_cooldown(self):
        self.network.answers.append(Response(b'<error code="302">client version missing or invalid</error>'))
        result = self.check()
        self.assertEqual(result.status, AUTH_FAILED)
        self.assertIn('AniDB did not accept the client "animediscovery"', result.detail)
        self.assertIn('client version missing or invalid', result.detail)
        (until, failures), = self.sql("SELECT next_allowed_at_ms,failures FROM provider_limits WHERE provider='anidb'")
        self.assertEqual(failures, 1)
        self.assertGreaterEqual(until - now_ms(), 9 * 60000)   # At least ten minutes.
        # A restart during the cooldown sends nothing and keeps the verdict.
        stored = status_store.StoredStatus('anidb', 'AniDB', AUTH_FAILED, result.detail, 1, 1, 1)
        again = self.check(previous=stored)
        self.assertEqual((again.status, again.detail), (AUTH_FAILED, result.detail))
        self.assertEqual(len(self.network.requests), 1)

    def test_a_ban_or_outage_does_not_count_as_a_rejected_client(self):
        self.network.answers.append(Response(b'<error>Banned</error>'))
        banned = self.check()
        self.assertEqual(banned.status, DOWN)
        self.assertIn('banned this client', banned.detail)
        self.sql("UPDATE provider_limits SET next_allowed_at_ms=0")   # Cooldown over.
        self.network.answers.append(urllib.error.URLError(ConnectionRefusedError()))
        down = self.check()
        self.assertEqual((down.status, down.detail), (DOWN, 'the AniDB HTTP API did not answer'))
        # During the new cooldown, with no earlier verdict: down, and no request.
        cooling = self.check()
        self.assertEqual(cooling.status, DOWN)
        self.assertIn('next request allowed in', cooling.detail)
        self.assertEqual(len(self.network.requests), 2)

    def test_a_missing_client_name_never_reaches_the_network(self):
        result = self.check(**{'anidb.client_name': ''})
        self.assertEqual(result.status, NOT_CONFIGURED)
        self.assertEqual(self.network.requests, [])

    def test_the_regular_check_still_never_contacts_anidb(self):
        result = AnidbAdapter(opener=self.network).probe(Config(self.database), None, now_ms=now_ms())
        self.assertEqual(result.status, UNKNOWN)
        self.assertEqual(self.network.requests, [])


class TvdbStartupTests(DatabaseCase):
    def check(self, secrets=None):
        return TvdbAdapter(opener=self.network).startup_probe(Config(self.database), secrets or Secrets(),
                                                              now_ms=now_ms())

    def test_login_proves_the_api_key(self):
        self.network.answers.append(Response(json.dumps({'data': {'token': 'abc'}}).encode()))
        self.assertEqual(self.check().status, UP)
        self.assertTrue(self.network.requests[0].endswith('/login'))

    def test_a_rejected_key_and_a_missing_key(self):
        self.network.answers.append(urllib.error.HTTPError('https://x', 401, '', {}, io.BytesIO()))
        rejected = self.check()
        self.assertEqual((rejected.status, rejected.detail),
                         (AUTH_FAILED, 'TVDB rejected the API key; check it in Settings'))
        missing = self.check(Secrets(key=''))
        self.assertEqual((missing.status, missing.detail), (NOT_CONFIGURED, 'set the TVDB API key in Settings'))
        self.assertEqual(len(self.network.requests), 1)

    def test_an_unreachable_tvdb_is_down_not_rejected(self):
        self.network.answers.append(urllib.error.URLError(TimeoutError()))
        self.assertEqual(self.check().status, DOWN)


class Fixed(ServiceAdapter):
    def __init__(self, name, status, *, required=False, detail='test detail'):
        self.name, self.label, self.status, self.required, self.detail = name, name.title(), status, required, detail
        self.startup_calls, self.previous = 0, 'unset'

    def probe(self, configuration, secrets, *, now_ms=0):
        return self.result(self.status, self.detail, now_ms=now_ms)

    def startup_probe(self, configuration, secrets, *, now_ms=0, previous=None):
        self.startup_calls += 1
        self.previous = previous
        return self.probe(configuration, secrets, now_ms=now_ms)


class PolicyTests(unittest.TestCase):
    def health(self, *adapters):
        health = ServiceHealth(ServiceRegistry(adapters), clock=lambda: 1000)
        health.startup(None, None)
        return health

    def test_only_misconfigured_required_services_refuse(self):
        health = self.health(Fixed('qbittorrent', AUTH_FAILED, required=True),
                             Fixed('anidb', NOT_CONFIGURED, required=True),
                             Fixed('tvdb', DOWN, required=True),          # Only unavailable: jobs wait.
                             Fixed('sonarr', NOT_CONFIGURED),             # Optional: never refuses.
                             Fixed('prowlarr', DISABLED))
        self.assertEqual([s.service for s in refusals(health)], ['qbittorrent', 'anidb'])
        lines = StartupRefused(refusals(health)).lines()
        self.assertEqual(lines[0], 'Qbittorrent rejected the login or API key: test detail')
        self.assertIn('adm services check', lines[-1])

    def test_startup_passes_the_saved_status_to_each_adapter(self):
        adapter = Fixed('anidb', UP, required=True)
        health = ServiceHealth(ServiceRegistry([adapter]), clock=lambda: 1000)
        saved = status_store.StoredStatus('anidb', 'AniDB', AUTH_FAILED, 'x', 1, 1, 1)
        health.startup(None, None, {'anidb': saved})
        self.assertIs(adapter.previous, saved)

    def test_a_turned_off_service_is_not_marked_down_by_a_leftover_job(self):
        adapter = Fixed('prowlarr', DISABLED)
        adapter.reasons = {'SERVICE_SETUP_INVALID': NOT_CONFIGURED}
        health = self.health(adapter)
        self.assertIsNone(health.mark_reason('prowlarr', 'SERVICE_SETUP_INVALID'))
        self.assertIsNone(health.mark_failed('prowlarr', ConnectionError()))
        self.assertEqual(health.state('prowlarr').status, DISABLED)


class StatusStoreTests(DatabaseCase):
    def test_save_load_and_what_blocks_a_feature(self):
        health = ServiceHealth(ServiceRegistry([Fixed('sonarr', DOWN, detail='HTTP 503 at http://s:8989'),
                                                Fixed('tvdb', UP)]), clock=lambda: 5000)
        health.startup(None, None)
        status_store.save(self.database, health.snapshot(), now_ms=6000)
        stored = status_store.load(self.database)
        self.assertEqual(stored['sonarr'].message(), 'Sonarr is unavailable: HTTP 503 at http://s:8989')
        self.assertEqual((stored['sonarr'].checked_at_ms, stored['sonarr'].updated_at_ms), (5000, 6000))
        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(status_store.blocking(db, 'sonarr', now_ms=7000).status, DOWN)
            self.assertIsNone(status_store.blocking(db, 'tvdb', now_ms=7000))          # Up.
            self.assertIsNone(status_store.blocking(db, 'jellyfin', now_ms=7000))      # Never written.
            stale = 6000 + status_store.STALE_MS + 1                                   # Worker stopped.
            self.assertIsNone(status_store.blocking(db, 'sonarr', now_ms=stale))
        finally:
            db.close()

    def test_load_never_raises(self):
        self.assertEqual(status_store.load(self.database.with_name('missing.sqlite')), {})


class WorkerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.database = root / 'q.sqlite'
        self.store = ConfigurationStore(root)
        self.startup = self.store.initialize({'storage': {'database_path': str(self.database)},
                                              'worker': {'concurrency': 1}}).snapshot
        db = sqlite3.connect(self.database)
        try:
            initialize(db)
        finally:
            db.close()

    def worker(self, *adapters):
        async def handler(context):
            return Outcome('complete')
        registry = Registry((Registration('JOB', HandlerDefinition('JOB', 'Job'), handler, mode='async'),))
        health = ServiceHealth(ServiceRegistry(adapters))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000, poll_ms=10),
                        service_health=health, secrets=Secrets())
        self.addAsyncCleanup(worker.close)
        return worker

    async def test_a_misconfigured_required_service_stops_startup_with_a_clear_log(self):
        worker = self.worker(Fixed('qbittorrent', NOT_CONFIGURED, required=True,
                                   detail='set the qBittorrent address in Settings'),
                             Fixed('sonarr', DOWN))
        ready = []
        with self.assertLogs(worker.logger, logging.ERROR) as logs, self.assertRaises(StartupRefused) as refused:
            await worker.run(on_ready=lambda: ready.append(True))
        self.assertEqual(ready, [])                                   # Never reported ready to systemd.
        self.assertEqual([s.service for s in refused.exception.states], ['qbittorrent'])
        self.assertTrue(any('service qbittorrent' in line
                            and 'qBittorrent address' in line for line in logs.output))
        # The dashboard can still show why: the statuses were saved first.
        self.assertEqual(status_store.load(self.database)['qbittorrent'].status, NOT_CONFIGURED)

    async def test_unavailable_or_optional_services_do_not_stop_startup(self):
        worker = self.worker(Fixed('qbittorrent', DOWN, required=True), Fixed('sonarr', NOT_CONFIGURED),
                             Fixed('prowlarr', DISABLED))
        worker.run_task = None

        def ready():
            worker.stop()
        await worker.run(on_ready=ready)
        stored = status_store.load(self.database)
        self.assertEqual({k: v.status for k, v in stored.items()},
                         {'qbittorrent': DOWN, 'sonarr': NOT_CONFIGURED, 'prowlarr': DISABLED})


class ExitCodeTests(unittest.TestCase):
    def test_main_exits_78_with_one_line_per_service(self):
        from src.nextgen import __main__ as entry
        state = SimpleNamespace(message=lambda: 'AniDB is not set up: set the registered AniDB client name in Settings')

        def refuse(*args, **kwargs):
            raise StartupRefused((state,))
        stderr = io.StringIO()
        with mock.patch.object(entry, 'check_local'), \
                mock.patch('src.nextgen.providers.configuration.run_identity_worker', refuse), \
                mock.patch('asyncio.run', lambda value: value), mock.patch('sys.stderr', stderr):
            code = entry.main(['worker', '--config-dir', '/nonexistent'])
        self.assertEqual(code, EXIT_CODE)
        self.assertEqual(stderr.getvalue().splitlines()[:2],
                         ['SERVICE_SETUP_INVALID: The worker did not start.',
                          'AniDB is not set up: set the registered AniDB client name in Settings'])


if __name__ == '__main__':
    unittest.main()
