"""Authoritative qBittorrent readiness validation and snapshot caching tests (AG-001)."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock

from src.nextgen.acquisition.readiness import QbittorrentReadiness, error_reason, ACQUISITION_KINDS
from src.nextgen.acquisition.settings import DownloadCredentials, DownloadOptions
from src.nextgen.acquisition.submission import (
    ClientAuthenticationFailed, ClientInvalid, ClientUnavailable, ClientUnsupported,
)
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import Outcome, Policy, Registration, Registry
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction
from src.nextgen.worker import Worker


class MockConfig:
    def __init__(self, values=None):
        self._values = {
            'qbittorrent.base_url': 'http://127.0.0.1:8080',
            'storage.download_root': '/media/downloads',
            'qbittorrent.category': 'anime',
            'qbittorrent.timeout_seconds': 10,
        }
        if values:
            self._values.update(values)

    def value(self, key):
        return self._values[key]


class MockSecrets:
    def __init__(self, user='admin', password='secretpassword'):
        self.user = user
        self.password = password

    def qbittorrent_credentials(self):
        return DownloadCredentials(self.user, self.password)


class FakeQbClient:
    def __init__(self, options, credentials, *, strategy='fetch', error=None):
        self.options = options
        self.credentials = credentials
        self.metadata_strategy = strategy
        self.error = error
        self.login_called = False

    def login(self):
        self.login_called = True
        if self.error:
            raise self.error


class QbittorrentReadinessTests(unittest.TestCase):
    def test_readiness_valid_credentials_and_caching(self):
        config = MockConfig()
        secrets = MockSecrets()
        created_clients = []

        def factory(options, credentials):
            c = FakeQbClient(options, credentials, strategy='fetch')
            created_clients.append(c)
            return c

        readiness = QbittorrentReadiness(client_factory=factory)
        self.assertEqual(readiness.check_count, 0)

        # First check performs login and returns strategy
        strategy = readiness.check(config, secrets, now_ms=1000)
        self.assertEqual(strategy, 'fetch')
        self.assertEqual(readiness.check_count, 1)
        self.assertTrue(created_clients[0].login_called)

        # Second check with same config/secrets hits cache (zero new calls)
        strategy2 = readiness.check(config, secrets, now_ms=2000)
        self.assertEqual(strategy2, 'fetch')
        self.assertEqual(readiness.check_count, 1)
        self.assertEqual(len(created_clients), 1)

        # is_ready also hits cache
        self.assertTrue(readiness.is_ready(config, secrets, now_ms=3000))
        self.assertEqual(readiness.check_count, 1)

    def test_readiness_invalid_credentials_auth_failed(self):
        config = MockConfig()
        secrets = MockSecrets()
        created_clients = []

        def factory(options, credentials):
            c = FakeQbClient(options, credentials, error=ClientAuthenticationFailed())
            created_clients.append(c)
            return c

        readiness = QbittorrentReadiness(client_factory=factory, min_backoff_ms=2000)
        with self.assertRaises(ClientAuthenticationFailed) as ctx:
            readiness.check(config, secrets, now_ms=1000)
        self.assertEqual(error_reason(ctx.exception), 'DOWNLOAD_CLIENT_AUTH_FAILED')
        self.assertEqual(readiness.check_count, 1)

        # is_ready returns False without raising
        self.assertFalse(readiness.is_ready(config, secrets, now_ms=1500))
        # Within backoff window, factory is not called again
        self.assertEqual(readiness.check_count, 1)

    def test_readiness_unsupported_strategy(self):
        config = MockConfig()
        secrets = MockSecrets()

        def factory(options, credentials):
            return FakeQbClient(options, credentials, strategy='unknown_legacy')

        readiness = QbittorrentReadiness(client_factory=factory)
        with self.assertRaises(ClientUnsupported) as ctx:
            readiness.check(config, secrets, now_ms=1000)
        self.assertEqual(error_reason(ctx.exception), 'DOWNLOAD_CLIENT_UNSUPPORTED')
        self.assertFalse(readiness.is_ready(config, secrets, now_ms=1000))

    def test_readiness_temporary_outage_and_exponential_backoff(self):
        config = MockConfig()
        secrets = MockSecrets()
        client_state = {'error': ClientUnavailable(), 'strategy': 'fetch'}
        call_count = [0]

        def factory(options, credentials):
            call_count[0] += 1
            return FakeQbClient(options, credentials, strategy=client_state['strategy'], error=client_state['error'])

        readiness = QbittorrentReadiness(client_factory=factory, min_backoff_ms=1000, max_backoff_ms=8000)

        # t=1000: Initial failure. Backoff doubles from 1000 -> 2000ms.
        with self.assertRaises(ClientUnavailable) as ctx:
            readiness.check(config, secrets, now_ms=1000)
        self.assertEqual(error_reason(ctx.exception), 'DOWNLOAD_CLIENT_UNAVAILABLE')
        self.assertEqual(call_count[0], 1)

        # t=1500: Within 2000ms backoff, raises without calling factory
        with self.assertRaises(ClientUnavailable):
            readiness.check(config, secrets, now_ms=1500)
        self.assertEqual(call_count[0], 1)

        # t=3100: Backoff (2000ms from 1000 = 3000ms) elapsed! Retries remote check.
        # Still failing -> backoff doubles to 4000ms.
        with self.assertRaises(ClientUnavailable):
            readiness.check(config, secrets, now_ms=3100)
        self.assertEqual(call_count[0], 2)

        # t=5000: Within 4000ms backoff (3100 + 4000 = 7100ms)
        with self.assertRaises(ClientUnavailable):
            readiness.check(config, secrets, now_ms=5000)
        self.assertEqual(call_count[0], 2)

        # Remote server recovers!
        client_state['error'] = None

        # t=7200: Backoff elapsed, attempts check and succeeds!
        strategy = readiness.check(config, secrets, now_ms=7200)
        self.assertEqual(strategy, 'fetch')
        self.assertEqual(call_count[0], 3)
        self.assertTrue(readiness.is_ready(config, secrets, now_ms=7300))
        # Subsequent check hits cache
        self.assertEqual(call_count[0], 3)

    def test_readiness_configuration_change_invalidates_cache(self):
        config1 = MockConfig({'qbittorrent.base_url': 'http://127.0.0.1:8080'})
        config2 = MockConfig({'qbittorrent.base_url': 'http://127.0.0.1:9090'})
        secrets = MockSecrets()
        created = []

        def factory(options, credentials):
            c = FakeQbClient(options, credentials, strategy='fetch')
            created.append(c)
            return c

        readiness = QbittorrentReadiness(client_factory=factory)
        readiness.check(config1, secrets, now_ms=1000)
        self.assertEqual(len(created), 1)

        # Same config uses cache
        readiness.check(config1, secrets, now_ms=2000)
        self.assertEqual(len(created), 1)

        # Changed config invalidates cache and triggers new check
        readiness.check(config2, secrets, now_ms=3000)
        self.assertEqual(len(created), 2)

        # Changed password invalidates cache
        secrets2 = MockSecrets(password='new_secret_password')
        readiness.check(config2, secrets2, now_ms=4000)
        self.assertEqual(len(created), 3)


class WorkerReadinessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "queue.sqlite"
        self.store = ConfigurationStore(self.root)
        self.startup = self.store.initialize({
            "storage": {
                "database_path": str(self.database),
                "download_root": "/media/downloads",
            },
            "worker": {"concurrency": 2},
            "qbittorrent": {
                "base_url": "http://127.0.0.1:8080",
                "category": "anime",
                "timeout_seconds": 10,
            },
        }).snapshot
        db = sqlite3.connect(self.database)
        initialize(db)
        db.close()
        self.policy = Policy(lease_ms=30000, poll_ms=10)

    def enqueue(self, kind="TEST", payload=None):
        db = sqlite3.connect(self.database)
        try:
            with transaction(db):
                return enqueue_job(db, kind=kind, payload={} if payload is None else payload,
                                   component="intake", available_at_ms=0)
        finally:
            db.close()

    async def test_worker_claim_excludes_acquisition_when_not_ready(self):
        executed = []

        async def handler(context):
            executed.append(context.claim.kind)
            return Outcome("complete")

        registrations = (
            Registration("CLEANUP_CANCELED_OPERATION", HandlerDefinition("CLEANUP", "Cleanup work"), handler, mode="async"),
            Registration("PREPARE_ACQUISITION", HandlerDefinition("PREPARE", "Acquisition work"), handler, mode="async"),
        )
        registry = Registry(registrations)

        # Initially qB client is down (raising ClientUnavailable)
        qb_state = {'available': False}

        def factory(options, credentials):
            if not qb_state['available']:
                raise ClientUnavailable()
            return FakeQbClient(options, credentials, strategy='fetch')

        readiness = QbittorrentReadiness(client_factory=factory, min_backoff_ms=50)
        secrets = MockSecrets()

        worker = Worker(self.store, self.startup, registry, policy=self.policy,
                        qb_readiness=readiness, secrets=secrets)
        self.addAsyncCleanup(worker.close)

        # Enqueue one acquisition job and one non-acquisition job
        acq_job_id = self.enqueue("PREPARE_ACQUISITION", {"info_hash": "a" * 40})
        cleanup_job_id = self.enqueue("CLEANUP_CANCELED_OPERATION", {"target_job_id": 999})

        # Run an attempt while qBittorrent is unavailable
        worked = await worker.attempt()
        self.assertTrue(worked)
        # Verify only the CLEANUP job was claimed and executed!
        self.assertEqual(executed, ["CLEANUP_CANCELED_OPERATION"])

        # Check job queue states in DB
        db = sqlite3.connect(self.database)
        try:
            acq_state = db.execute("SELECT state FROM jobs WHERE job_id=?", (acq_job_id,)).fetchone()[0]
            cleanup_state = db.execute("SELECT state FROM jobs WHERE job_id=?", (cleanup_job_id,)).fetchone()[0]
            # Acquisition job was NEVER consumed - remains PENDING!
            self.assertEqual(acq_state, "PENDING")
            self.assertEqual(cleanup_state, "DONE")
        finally:
            db.close()

        # Another attempt while still unavailable returns False (no non-acquisition work)
        worked2 = await worker.attempt()
        self.assertFalse(worked2)
        self.assertEqual(len(executed), 1)

        # Now qBittorrent recovers!
        qb_state['available'] = True
        # Sleep slightly so min_backoff_ms elapses
        await asyncio.sleep(0.06)

        # Attempt now claims the acquisition job!
        worked3 = await worker.attempt()
        self.assertTrue(worked3)
        self.assertEqual(executed, ["CLEANUP_CANCELED_OPERATION", "PREPARE_ACQUISITION"])

        # Verify acquisition job is now DONE
        db = sqlite3.connect(self.database)
        try:
            acq_state = db.execute("SELECT state FROM jobs WHERE job_id=?", (acq_job_id,)).fetchone()[0]
            self.assertEqual(acq_state, "DONE")
        finally:
            db.close()

    async def test_every_qbittorrent_job_kind_waits_while_not_ready(self):
        """Regression: batch kinds were misspelled (BATCH_SUBMIT etc.) and still claimed."""
        executed = []

        async def handler(context):
            executed.append(context.claim.kind)
            return Outcome("complete")

        qb_kinds = _real_qbittorrent_kinds()  # Real KIND constants, not the list under test.
        kinds = (*qb_kinds, "CLEANUP_CANCELED_OPERATION")
        registry = Registry(tuple(
            Registration(kind, HandlerDefinition(kind, kind.title()), handler, mode="async")
            for kind in kinds))
        qb_state = {'available': False}

        def factory(options, credentials):
            if not qb_state['available']:
                raise ClientUnavailable()
            return FakeQbClient(options, credentials, strategy='fetch')

        worker = Worker(self.store, self.startup, registry, policy=self.policy,
                        qb_readiness=QbittorrentReadiness(client_factory=factory, min_backoff_ms=50),
                        secrets=MockSecrets())
        self.addAsyncCleanup(worker.close)
        jobs = {kind: self.enqueue(kind) for kind in kinds}

        while await worker.attempt():
            pass
        self.assertEqual(executed, ["CLEANUP_CANCELED_OPERATION"])
        db = sqlite3.connect(self.database)
        try:
            states = {kind: db.execute("SELECT state FROM jobs WHERE job_id=?", (job,)).fetchone()[0]
                      for kind, job in jobs.items()}
        finally:
            db.close()
        self.assertEqual({kind: states[kind] for kind in qb_kinds},
                         dict.fromkeys(qb_kinds, "PENDING"))

        qb_state['available'] = True
        await asyncio.sleep(0.06)
        while await worker.attempt():
            pass
        self.assertEqual(sorted(executed), sorted(kinds))


def _real_qbittorrent_kinds():
    """KIND constants of the handlers that contact qBittorrent."""
    from src.nextgen.jobs import (
        download_torrent_job, prepare_acquisition_job, submit_batch_job,
        submit_torrent_job, transfer_batch_job, unlock_batch_job,
    )
    return tuple(module.KIND for module in (
        prepare_acquisition_job, submit_torrent_job, download_torrent_job,
        submit_batch_job, transfer_batch_job, unlock_batch_job))


class AcquisitionKindsTests(unittest.TestCase):
    def test_every_entry_is_a_real_job_kind(self):
        """The list is spelled out by hand; tie each entry to its handler's KIND."""
        self.assertEqual(ACQUISITION_KINDS, _real_qbittorrent_kinds())


if __name__ == '__main__':
    unittest.main()
