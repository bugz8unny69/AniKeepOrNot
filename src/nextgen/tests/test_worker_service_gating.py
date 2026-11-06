"""Worker integration of service health: jobs wait while a service they need is unavailable."""
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import Outcome, Policy, Registration, Registry
from src.nextgen.services import (
    AUTH_FAILED, DOWN, UP, ServiceAdapter, ServiceHealth, ServiceRegistry, default_registry,
)
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction
from src.nextgen.worker import Worker


class Clock:
    def __init__(self):
        self.now = 1_000_000

    def __call__(self):
        return self.now


class Switch(ServiceAdapter):
    """A service whose health the test controls; counts probes."""
    def __init__(self, name, status=UP):
        self.name, self.label, self.status, self.probes = name, name.title(), status, 0
        self.reasons = {}

    def probe(self, configuration, secrets, *, now_ms=0):
        self.probes += 1
        return self.result(self.status, f'{self.label} test status', now_ms=now_ms)


class Secrets:
    def __init__(self):
        self.revision = 1

    def status(self):
        return type('Status', (), {'revision': self.revision})()


def real_service_kinds():
    """Real job modules and the services they declare, read from their own SERVICES."""
    from src.nextgen import decision_download_cleanup, post_keep_cleanup
    from src.nextgen.jobs import (
        cleanup_batch_download_job, cleanup_batch_kept_media_job, cleanup_canceled_operation_job,
        discover_episode_releases_job, download_torrent_job, enrich_shoko_job, keep_batch_job,
        keep_media_job, prepare_acquisition_job, refresh_jellyfin_job, submit_batch_job,
        submit_torrent_job, transfer_batch_job, unlock_batch_job,
    )
    modules = (prepare_acquisition_job, submit_torrent_job, download_torrent_job, submit_batch_job,
               transfer_batch_job, unlock_batch_job, cleanup_batch_download_job,
               cleanup_canceled_operation_job, decision_download_cleanup, discover_episode_releases_job,
               keep_media_job, keep_batch_job, post_keep_cleanup, cleanup_batch_kept_media_job,
               refresh_jellyfin_job, enrich_shoko_job)
    return {m.KIND: m.SERVICES for m in modules}


class WorkerServiceGatingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.database = root / 'queue.sqlite'
        self.store = ConfigurationStore(root)
        self.startup = self.store.initialize({'storage': {'database_path': str(self.database)},
                                              'worker': {'concurrency': 1}}).snapshot
        db = sqlite3.connect(self.database)
        try:
            initialize(db)
        finally:
            db.close()
        self.clock = Clock()
        self.qb = Switch('qbittorrent', DOWN)
        self.prowlarr = Switch('prowlarr')
        self.health = ServiceHealth(ServiceRegistry([self.qb, self.prowlarr]), clock=self.clock,
                                    recheck_ms=60000, min_backoff_ms=15000, max_backoff_ms=60000)
        self.secrets = Secrets()
        self.ran = []
        self.results = {}

    def worker(self, kinds):
        async def handler(context):
            self.ran.append(context.claim.kind)
            return self.results.get(context.claim.kind, Outcome('complete'))
        registry = Registry(tuple(
            Registration(kind, HandlerDefinition(kind, kind.title()), handler, mode='async', services=services)
            for kind, services in kinds.items()))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000, poll_ms=10),
                        service_health=self.health, secrets=self.secrets)
        self.addAsyncCleanup(worker.close)
        return worker

    def enqueue(self, kind):
        db = sqlite3.connect(self.database)
        try:
            with transaction(db):
                return enqueue_job(db, kind=kind, payload={}, component='intake', available_at_ms=0)
        finally:
            db.close()

    def states(self):
        db = sqlite3.connect(self.database)
        try:
            return dict(db.execute('SELECT kind,state FROM jobs'))
        finally:
            db.close()

    async def drain(self, worker):
        while await worker.attempt():
            pass

    async def test_jobs_wait_while_their_service_is_down_and_run_after_recovery(self):
        worker = self.worker({'QB_JOB': ('qbittorrent',), 'SEARCH_JOB': ('prowlarr',), 'LOCAL_JOB': ()})
        for kind in ('QB_JOB', 'SEARCH_JOB', 'LOCAL_JOB'):
            self.enqueue(kind)
        with self.assertLogs(worker.logger, logging.WARNING) as logs:
            await self.drain(worker)
        self.assertEqual(sorted(self.ran), ['LOCAL_JOB', 'SEARCH_JOB'])
        self.assertEqual(self.states()['QB_JOB'], 'PENDING')  # Never claimed: no retry spent.
        self.assertEqual(sum('event_code=SERVICE_DOWN' in line and 'service=qbittorrent' in line
                             for line in logs.output), 1)
        self.assertTrue(any('Qbittorrent is unavailable: Qbittorrent test status' in line for line in logs.output))

        await self.drain(worker)              # Still inside the backoff: no new probe.
        self.assertEqual(self.qb.probes, 1)

        self.qb.status = UP
        self.clock.now += 15000                # Backoff elapsed.
        with self.assertLogs(worker.logger, logging.INFO) as logs:
            await self.drain(worker)
        self.assertIn('QB_JOB', self.ran)
        self.assertEqual(self.states()['QB_JOB'], 'DONE')
        self.assertTrue(any('event_code=SERVICE_RESTORED' in line for line in logs.output))

    async def test_a_failing_job_marks_its_service_down_without_a_probe(self):
        self.qb.status = UP
        self.results['QB_FIRST'] = Outcome('retry', 'SERVICE_UNAVAILABLE', facts={'service': 'qbittorrent'})
        worker = self.worker({'QB_FIRST': ('qbittorrent',), 'QB_SECOND': ('qbittorrent',)})
        self.enqueue('QB_FIRST')
        await self.drain(worker)
        probes = self.qb.probes
        self.assertEqual(self.health.state('qbittorrent').status, DOWN)
        self.enqueue('QB_SECOND')
        await self.drain(worker)
        # The outage that began after startup is noticed at once: the second
        # job waits instead of failing, and no extra probe was needed for it.
        self.assertNotIn('QB_SECOND', self.ran)
        self.assertEqual(self.states()['QB_SECOND'], 'PENDING')
        self.assertEqual(self.qb.probes, probes)

    async def test_settings_change_rechecks_at_once(self):
        self.qb.status = AUTH_FAILED
        worker = self.worker({'QB_JOB': ('qbittorrent',)})
        self.enqueue('QB_JOB')
        await self.drain(worker)
        self.assertEqual(self.states()['QB_JOB'], 'PENDING')
        self.qb.status = UP                    # The operator fixed the password...
        await self.drain(worker)
        self.assertEqual(self.states()['QB_JOB'], 'PENDING')  # ...but the backoff still applies.
        self.secrets.revision += 1             # ...and saving credentials changes the revision.
        await self.drain(worker)
        self.assertEqual(self.states()['QB_JOB'], 'DONE')

    async def test_first_success_is_not_logged_as_a_recovery(self):
        self.qb.status = UP
        worker = self.worker({'QB_JOB': ('qbittorrent',)})
        self.enqueue('QB_JOB')
        with self.assertNoLogs(worker.logger, logging.INFO):
            await self.drain(worker)
        self.assertEqual(self.states()['QB_JOB'], 'DONE')

    async def test_unknown_service_requirement_fails_before_any_claim(self):
        with self.assertRaises(ValueError):
            self.worker({'BAD': ('no_such_service',)})


class ServiceDeclarationTests(unittest.TestCase):
    def test_real_jobs_declare_the_services_they_contact(self):
        declared = real_service_kinds()
        qbittorrent = {'PREPARE_ACQUISITION', 'SUBMIT_TORRENT', 'DOWNLOAD_TORRENT', 'SUBMIT_BATCH',
                       'TRANSFER_BATCH', 'UNLOCK_BATCH', 'CLEANUP_BATCH_DOWNLOAD',
                       'CLEANUP_CANCELED_OPERATION', 'CLEANUP_DECIDED_DOWNLOAD'}
        expected = {**dict.fromkeys(qbittorrent, ('qbittorrent',)),
                    'DISCOVER_EPISODE_RELEASES': ('prowlarr',),
                    **dict.fromkeys(('KEEP_MEDIA', 'KEEP_BATCH', 'CLEANUP_KEPT_MEDIA',
                                     'CLEANUP_BATCH_KEPT_MEDIA'), ('sonarr',)),
                    'REFRESH_JELLYFIN': ('jellyfin',), 'ENRICH_SHOKO_METADATA': ('shoko',)}
        self.assertEqual(declared, expected)

    def test_every_declared_service_has_an_adapter(self):
        names = set(default_registry().names())
        for kind, services in real_service_kinds().items():
            with self.subTest(kind=kind):
                self.assertTrue(set(services) <= names)

    def test_registrations_carry_their_declaration(self):
        from src.nextgen.jobs.refresh_jellyfin_job import registration
        self.assertEqual(registration(object()).services, ('jellyfin',))
        with self.assertRaises(ValueError):
            Registration('X', HandlerDefinition('X', 'X'), lambda c: None, mode='thread', services=['qb'])
        with self.assertRaises(ValueError):
            Registration('X', HandlerDefinition('X', 'X'), lambda c: None, mode='thread',
                         services=('qb', 'qb'))


if __name__ == '__main__':
    unittest.main()
