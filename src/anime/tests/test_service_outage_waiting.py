"""An outage means waiting, not failing: SERVICE_UNAVAILABLE spends no retry and gives up after 72 hours."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from src.anime.acquisition.submission import ClientUnavailable
from src.anime.configuration import ConfigurationStore
from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import Outcome, Policy, Registration, Registry
from src.anime.services import DOWN, UP, ServiceAdapter, ServiceHealth, ServiceRegistry
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import enqueue_job, initialize, transaction
from src.anime.worker import Worker

HOUR = 3600 * 1000


class Clock:
    def __init__(self):
        self.now = 10_000_000

    def __call__(self):
        return self.now


class Qb(ServiceAdapter):
    name, label = 'qbittorrent', 'qBittorrent'
    reasons = {'SERVICE_UNAVAILABLE': DOWN}

    def __init__(self):
        self.status = UP

    def probe(self, configuration, secrets, *, now_ms=0):
        return self.result(self.status, 'could not connect at http://qb:8080'
                           if self.status != UP else '', now_ms=now_ms)

    def classify(self, exc):
        return DOWN if isinstance(exc, ClientUnavailable) else None


class Secrets:
    def status(self):
        return type('S', (), {'revision': 1})()


class Base(unittest.IsolatedAsyncioTestCase):
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

    def enqueue(self, kind):
        db = sqlite3.connect(self.database)
        try:
            with transaction(db):
                return enqueue_job(db, kind=kind, payload={}, component='intake', available_at_ms=0)
        finally:
            db.close()

    def rows(self, sql, args=()):
        db = sqlite3.connect(self.database)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()


class WorkerWaitingTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.qb = Qb()
        self.health = ServiceHealth(ServiceRegistry([self.qb]), clock=lambda: 0)
        self.behaviour = 'outage'

    def worker(self):
        async def handler(context):
            if self.behaviour == 'outage':
                return Outcome('retry', 'SERVICE_UNAVAILABLE',
                               facts={'service': 'qbittorrent',
                                      'failed_check': 'src/anime/acquisition/qbittorrent.py:122 login CLIENTUNAVAILABLE'})
            if self.behaviour == 'unnamed':
                # For example an AniDB or TVDB outage inside a job that declares another service.
                return Outcome('retry', 'SERVICE_UNAVAILABLE')
            if self.behaviour == 'raise':
                raise ClientUnavailable()
            if self.behaviour == 'other':
                return Outcome('retry', 'JOB_HANDLER_FAILED')
            return Outcome('complete')
        registry = Registry((Registration('QB_JOB', HandlerDefinition('QB_JOB', 'Qb job'), handler,
                                          mode='async', services=('qbittorrent',)),))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000, poll_ms=10),
                        service_health=self.health, secrets=Secrets())
        self.addAsyncCleanup(worker.close)
        return worker

    def history(self, job):
        return [(code, reason, json.loads(details or '{}')) for code, reason, details in self.rows(
            'SELECT event_code,reason_code,details_json FROM job_history WHERE job_id=? ORDER BY history_id',
            (job,))]

    async def test_an_outage_is_a_wait_that_spends_no_retry(self):
        job = self.enqueue('QB_JOB')
        await self.worker().attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?', (job,)),
                         [('RETRY_WAIT', 'SERVICE_UNAVAILABLE')])
        events = self.history(job)
        self.assertNotIn('RETRY_SCHEDULED', [code for code, _, _ in events])  # No retry spent.
        code, reason, details = events[-1]
        self.assertEqual((code, reason), ('DEFERRED', 'SERVICE_UNAVAILABLE'))
        self.assertEqual(details['service'], 'qbittorrent')
        self.assertNotIn('cause_reason_code', details)  # The handler already reported the generic code.
        self.assertTrue(details['service_detail'].startswith('qBittorrent is unavailable'))
        self.assertIn('failed_check', details)
        self.assertIn('waiting_since_ms', details)
        self.assertEqual(self.health.state('qbittorrent').status, DOWN)

    async def test_an_outage_that_names_no_service_is_an_ordinary_retry(self):
        self.behaviour = 'unnamed'
        job = self.enqueue('QB_JOB')
        await self.worker().attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?', (job,)),
                         [('RETRY_WAIT', 'SERVICE_UNAVAILABLE')])
        self.assertIn('RETRY_SCHEDULED', [code for code, _, _ in self.history(job)])
        self.assertNotEqual(self.health.state('qbittorrent').status, DOWN)  # qBittorrent is not blamed.

    async def test_an_uncaught_client_error_also_waits(self):
        self.behaviour = 'raise'
        job = self.enqueue('QB_JOB')
        await self.worker().attempt()
        code, reason, details = self.history(job)[-1]
        self.assertEqual((code, reason, details['service']), ('DEFERRED', 'SERVICE_UNAVAILABLE', 'qbittorrent'))
        self.assertNotIn('cause_reason_code', details)
        self.assertEqual(self.health.state('qbittorrent').status, DOWN)

    async def test_unrelated_retries_are_unchanged(self):
        self.behaviour = 'other'
        job = self.enqueue('QB_JOB')
        await self.worker().attempt()
        self.assertEqual(self.rows('SELECT last_reason_code FROM jobs WHERE job_id=?', (job,)),
                         [('JOB_HANDLER_FAILED',)])
        self.assertIn('RETRY_SCHEDULED', [code for code, _, _ in self.history(job)])

    async def test_the_job_resumes_once_the_service_is_back(self):
        job = self.enqueue('QB_JOB')
        worker = self.worker()
        await worker.attempt()
        self.behaviour = 'ok'
        self.health.mark_succeeded('qbittorrent')
        db = sqlite3.connect(self.database)  # Make the waiting job available now.
        try:
            with db:
                db.execute('UPDATE jobs SET available_at_ms=0 WHERE job_id=?', (job,))
        finally:
            db.close()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?', (job,)), [('DONE',)])


class GiveUpTests(Base):
    """The 72-hour rule, driven through the job queue with a controlled clock."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clock = Clock()
        self.queue = JobQueue(self.database, policy=Policy(lease_ms=30000), clock=self.clock)
        self.spec = Registration('QB_JOB', HandlerDefinition('QB_JOB', 'Qb job'), self._noop, mode='async',
                                 services=('qbittorrent',))
        self.registry = Registry((self.spec,))

    async def _noop(self, context):
        return Outcome('complete')

    def wait_once(self, outcome=None):
        claim, _ = self.queue.claim()
        invocation = uuid.uuid4().hex
        self.queue.start(claim, self.spec, invocation, self.registry)
        self.queue.finish(claim, outcome or Outcome('defer', 'SERVICE_UNAVAILABLE', delay_ms=60000,
                                                    facts={'service': 'qbittorrent'}),
                          spec=self.spec, invocation=invocation, registry=self.registry)
        return claim.job_id

    def state(self, job):
        return self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?', (job,))[0]

    async def test_waits_until_72_hours_then_stops_for_review(self):
        job = self.enqueue('QB_JOB')
        self.wait_once()
        for hours in (1, 24, 71):
            self.clock.now = 10_000_000 + hours * HOUR
            self.wait_once()
            self.assertEqual(self.state(job), ('RETRY_WAIT', 'SERVICE_UNAVAILABLE'), hours)
        self.clock.now = 10_000_000 + 72 * HOUR
        self.wait_once()
        self.assertEqual(self.state(job), ('NEEDS_REVIEW', 'SERVICE_UNAVAILABLE'))
        details = json.loads(self.rows("SELECT details_json FROM job_history WHERE job_id=? "
                                       "AND event_code='NEEDS_REVIEW'", (job,))[0][0])
        self.assertEqual(details['waiting_since_ms'], 10_000_000)

    async def test_a_manual_retry_starts_a_new_waiting_period(self):
        job = self.enqueue('QB_JOB')
        self.wait_once()
        self.clock.now += 70 * HOUR
        db = sqlite3.connect(self.database)
        try:
            with db:
                db.execute("INSERT INTO job_history(job_id,occurred_at_ms,event_code,component) "
                           "VALUES(?,?,'RETRY_REQUESTED','api')", (job, self.clock.now))
        finally:
            db.close()
        self.clock.now += 10 * HOUR       # 80 h since the first wait, 10 h since the retry.
        self.wait_once()
        self.assertEqual(self.state(job), ('RETRY_WAIT', 'SERVICE_UNAVAILABLE'))

    async def test_an_unrelated_outcome_in_between_resets_the_period(self):
        job = self.enqueue('QB_JOB')
        self.wait_once()
        self.clock.now += 60 * HOUR
        self.wait_once(Outcome('defer', 'JOB_WAITING', delay_ms=60000))
        self.clock.now += 20 * HOUR       # 80 h total, but only 20 h of this outage.
        self.wait_once()
        self.assertEqual(self.state(job), ('RETRY_WAIT', 'SERVICE_UNAVAILABLE'))


class PresentationTests(unittest.TestCase):
    def test_dashboard_detail_names_the_service(self):
        from src.anime.api.app import failed_detail
        self.assertEqual(failed_detail({'service': 'prowlarr',
                                        'service_detail': 'Prowlarr is unavailable: HTTP 503 at http://p:9696',
                                        'cause_reason_code': 'SERVICE_UNAVAILABLE'}),
                         'Prowlarr is unavailable: HTTP 503 at http://p:9696 (cause: SERVICE_UNAVAILABLE)')

    def test_policy_bounds_and_default_is_72_hours(self):
        self.assertEqual(Policy().service_give_up_ms, 72 * HOUR)
        for bad in ({'service_wait_ms': 0}, {'service_give_up_ms': 1000}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Policy(**bad)

    def test_service_facts_are_validated(self):
        from src.anime.history import encode_details
        with self.assertRaises(ValueError):
            encode_details({'service': 'Bad Name'})
        with self.assertRaises(ValueError):
            encode_details({'service_detail': 'line\nbreak'})
        with self.assertRaises(ValueError):
            Outcome('defer', 'SERVICE_UNAVAILABLE', delay_ms=1000, facts={'service': 5})


if __name__ == '__main__':
    unittest.main()
