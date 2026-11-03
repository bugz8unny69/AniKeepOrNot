"""Waiting for dub: audio-only exhaustion holds instead of review; Search again."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.nextgen.acquisition.prowlarr_search import DiscoveryPolicy, Release, SearchResult
from src.nextgen.job_contracts import Registry, Policy
from src.nextgen.jobs.discover_sonarr_backfill_job import (
    KIND as DISCOVER_KIND, registration as discovery_registration,
)
from src.nextgen.jobs.evaluate_trial_job import registration as trial_registration
from src.nextgen.storage.series_intake import detail, retry_episode
from src.nextgen.storage.sonarr_cached_retry import CachedTorrent
from src.nextgen.storage.sqlite import connect, enqueue_job, transaction
from src.nextgen.tests import test_backfill_discovery as backfill
from src.nextgen.worker import Worker

TRIED = ('a' * 40, 'b' * 40)
DUBBED = 'c' * 40


def release(title, digest):
    return Release(title, 'Nyaa', 1080, 50, 400_000_000, 2002, digest,
                   'magnet:?xt=urn:btih:' + digest, True, 'Test - 02')


class FakeSearch(backfill.FakeDiscovery):
    def __init__(self, releases=()):
        super().__init__()
        self.policy = DiscoveryPolicy(prefer_audio_tag=True)
        self.releases = tuple(releases)
        self.searches = 0

    def search(self, anidb_id, episode, identity):
        self.searches += 1
        return SearchResult(identity.tvdb_series_id, identity.tvdb_episode_id,
                            self.policy.rank(self.releases))


class WaitingForDubTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = backfill.BackfillWorkerTests.asyncSetUp
    rows = backfill.BackfillWorkerTests.rows
    source = backfill.BackfillWorkerTests.source

    async def exhausted(self, reasons=('MEDIA_AUDIO_REQUIRED', 'MEDIA_AUDIO_REQUIRED'), audio=None):
        """Plan for E10; episode 2's two ranked releases all failed; exhausted job queued."""
        if audio is not None:
            self.store.update({'acquisition': {'audio_check': audio}}, expected_revision=1)
        self.source(7)
        self.fake = FakeSearch()
        await self.run_worker(self.fake)        # EVALUATE_TRIAL -> first page job.
        db = connect(self.database)
        try:
            with transaction(db):
                event = db.execute('SELECT event_id FROM jobs WHERE kind=?', (DISCOVER_KIND,)).fetchone()[0]
                db.execute("UPDATE jobs SET state='DONE' WHERE kind=?", (DISCOVER_KIND,))
                db.execute('INSERT INTO sonarr_episode_selections VALUES(1,2,1,2,2002,?,1)', (TRIED[-1],))
                for rank, (digest, reason) in enumerate(zip(TRIED, reasons), 1):
                    db.execute("INSERT INTO sonarr_episode_candidates VALUES(1,2,?,?,'FAILED',?)",
                               (rank, digest, reason))
                self.job = enqueue_job(db, kind=DISCOVER_KIND, component='worker', event_id=event,
                                       payload={'plan_id': 1, 'start': 1, 'exhausted_episode': 2},
                                       available_at_ms=0)
        finally:
            db.close()

    async def run_worker(self, fake, attempts=1):
        registry = Registry((trial_registration(discovery_kind=DISCOVER_KIND),
                             discovery_registration(None, client_factory=lambda c, s: fake,
                                                    select_candidates=True, acquire_candidates=True)))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000))
        try:
            for _ in range(attempts):
                await worker.attempt()
        finally:
            await worker.close()

    def state(self):
        return self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?', (self.job,))[0]

    def series(self):
        db = connect(self.database)
        try: return detail(db, 123)
        finally: db.close()

    async def test_no_eligible_releases_show_waiting_and_search_again(self):
        from src.nextgen.acquisition.prowlarr_search import BackfillObservation

        self.source(7)
        fake = backfill.FakeDiscovery()
        original = fake.backfill

        def no_dub(*args, **kwargs):
            return tuple(
                BackfillObservation(n, 2000+n, 'WAITING_FOR_DUB', 4, 0)
                if n == 2 else item
                for n, item in zip(args[2], original(*args, **kwargs)))

        fake.backfill = no_dub
        worker = backfill.BackfillWorkerTests.worker(self, fake)
        await worker.attempt()  # Evaluate -> page 1
        await worker.attempt()  # Page 1 -> page 2
        await worker.attempt()  # Page 2
        self.assertEqual(self.rows(
            "SELECT status,candidates,approved FROM sonarr_backfill_results "
            "WHERE anidb_episode_number=2"), [('WAITING_FOR_DUB', 4, 0)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM jobs WHERE kind=? AND state='ON_HOLD'",
            (DISCOVER_KIND,)), [(0,)])
        episode = next(x for x in self.series()['missing'] if x['anidb_episode'] == 2)
        self.assertEqual((episode['status'], episode['can_retry']), ('WAITING_FOR_DUB', True))
        fake.backfill = original
        retried = retry_episode(self.database, 123, 2)
        self.assertFalse(retried['reused'])
        self.assertEqual(retry_episode(self.database, 123, 2)['reused'], True)
        await worker.attempt()
        self.assertEqual(self.rows(
            "SELECT status,approved FROM sonarr_backfill_results WHERE anidb_episode_number=2"),
            [('CANDIDATES', 1)])
        episode = next(x for x in self.series()['missing'] if x['anidb_episode'] == 2)
        self.assertEqual(episode['status'], 'CANDIDATES')

    async def test_audio_only_failures_wait_for_dub_instead_of_review(self):
        await self.exhausted()
        await self.run_worker(self.fake)
        self.assertEqual(self.state(), ('ON_HOLD', 'WAITING_FOR_DUB'))
        self.assertEqual(self.fake.searches, 1)
        episode = next(x for x in self.series()['missing'] if x['anidb_episode'] == 2)
        self.assertEqual((episode['status'], episode['can_retry']), ('WAITING_FOR_DUB', True))

    async def test_mixed_failures_with_one_audio_failure_wait_for_dub(self):
        await self.exhausted(reasons=('TORRENT_METADATA_TIMEOUT', 'MEDIA_AUDIO_REQUIRED'))
        await self.run_worker(self.fake)
        self.assertEqual(self.state(), ('ON_HOLD', 'WAITING_FOR_DUB'))

    async def test_no_audio_failure_still_needs_review(self):
        await self.exhausted(reasons=('TORRENT_METADATA_TIMEOUT', 'EPISODE_RESERVATION_CONFLICT'))
        await self.run_worker(self.fake)
        self.assertEqual(self.state(), ('NEEDS_REVIEW', 'SONARR_CANDIDATES_EXHAUSTED'))
        self.assertEqual(self.fake.searches, 0)

    async def test_check_only_runs_when_english_is_required(self):
        await self.exhausted(audio='japanese')
        await self.run_worker(self.fake)
        self.assertEqual(self.state(), ('NEEDS_REVIEW', 'SONARR_CANDIDATES_EXHAUSTED'))
        self.assertEqual(self.fake.searches, 0)

    async def test_search_again_tries_only_new_dual_audio_releases(self):
        await self.exhausted()
        await self.run_worker(self.fake)
        self.fake.releases = (release('[Old] Test - 02 (1080p)', TRIED[0]),
                              release('[Raw] Test - 02 (1080p)', 'd' * 40),
                              release('[Group] Test - 02 (1080p) [Dual Audio]', DUBBED))
        attempt = self.rows('SELECT attempt_count FROM jobs WHERE job_id=?', (self.job,))[0][0]
        result = retry_episode(self.database, 123, 2)
        self.assertEqual(result, {'job_id': self.job, 'reused': False})
        await self.run_worker(self.fake)
        self.assertEqual(self.state()[0], 'DONE')
        self.assertEqual(self.rows('SELECT rank,info_hash,state FROM sonarr_episode_candidates '
                                   'WHERE anidb_episode_number=2 AND rank=3'), [(3, DUBBED, 'READY')])
        payloads = self.rows('SELECT payload_json FROM jobs WHERE created_by_job_id=?', (self.job,))
        self.assertEqual([json.loads(p) for p, in payloads],
                         [{'plan_id': 1, 'start': 1, 'fallback_episode': 2}])
        self.assertGreater(attempt, 0)

    async def test_search_again_prefers_eligible_cached_magnet_without_prowlarr(self):
        await self.exhausted()
        await self.run_worker(self.fake)
        before = self.fake.searches
        cached = CachedTorrent(TRIED[0], '[Group] Test - 02 [Dual Audio]',
                               'Test - 02', 'magnet:?xt=urn:btih:' + TRIED[0])
        retry_episode(self.database, 123, 2, delete_cache=False)
        # Verify only branch selection; cache journal safety has its own tests.
        with patch('src.nextgen.jobs.discover_sonarr_backfill_job.load_cached',
                   return_value=cached), patch(
                   'src.nextgen.jobs.discover_sonarr_backfill_job.persist_cached',
                   return_value=()):
            await self.run_worker(self.fake)
        self.assertEqual(self.fake.searches, before)
        self.assertEqual(self.state()[0], 'DONE')

    async def test_search_again_without_cache_searches_prowlarr_for_new_dub(self):
        # Regression: job #5775 (plan #19, episode 1) previously went straight
        # back to WAITING_FOR_DUB without making any Prowlarr request.
        await self.exhausted()
        await self.run_worker(self.fake)
        before = self.fake.searches
        self.fake.releases = (release('[Group] Test - 02 [Dual Audio]', DUBBED),)
        receipt = retry_episode(self.database, 123, 2, delete_cache=False)
        self.assertFalse(receipt['reused'])
        await self.run_worker(self.fake)
        self.assertEqual(self.fake.searches, before + 1)
        self.assertEqual(self.state()[0], 'DONE')
        self.assertEqual(self.rows(
            "SELECT rank,info_hash,state FROM sonarr_episode_candidates "
            "WHERE plan_id=1 AND anidb_episode_number=2 AND rank=3"
        ), [(3, DUBBED, 'READY')])

    async def test_search_again_without_cache_and_without_new_release_did_search(self):
        await self.exhausted()
        await self.run_worker(self.fake)
        before = self.fake.searches
        retry_episode(self.database, 123, 2, delete_cache=False)
        await self.run_worker(self.fake)
        self.assertEqual(self.fake.searches, before + 1)
        self.assertEqual(self.state(), ('ON_HOLD', 'WAITING_FOR_DUB'))

    async def test_search_again_with_nothing_new_waits_again(self):
        await self.exhausted()
        await self.run_worker(self.fake)
        retry_episode(self.database, 123, 2)
        await self.run_worker(self.fake)
        self.assertEqual(self.state(), ('ON_HOLD', 'WAITING_FOR_DUB'))
        self.assertEqual(self.fake.searches, 2)


fix_spec = importlib.util.spec_from_file_location(
    'requeue_waiting_for_dub',
    Path(__file__).resolve().parents[3] / 'fixes' / 'requeue_waiting_for_dub.py')
fix = importlib.util.module_from_spec(fix_spec)
fix_spec.loader.exec_module(fix)


class RequeueFixTests(WaitingForDubTests):
    async def stuck(self, **kwargs):
        """Old behavior: the exhausted job stopped in review."""
        await self.exhausted(**kwargs)
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='NEEDS_REVIEW',attempt_count=1,"
                           "last_reason_code='SONARR_CANDIDATES_EXHAUSTED' WHERE job_id=?", (self.job,))
        finally:
            db.close()

    async def test_preview_then_apply_moves_stuck_jobs_to_waiting(self):
        await self.stuck()
        preview = fix.execute(self.database, 'japanese_english')
        self.assertEqual((preview['eligible_jobs'], preview['requeued']), ([self.job], 0))
        self.assertEqual(self.state()[0], 'NEEDS_REVIEW')
        applied = fix.execute(self.database, 'japanese_english', apply=True)
        self.assertEqual(applied['requeued'], 1)
        self.assertTrue(Path(applied['backup']).is_file())
        await self.run_worker(self.fake)
        self.assertEqual(self.state(), ('ON_HOLD', 'WAITING_FOR_DUB'))
        self.assertEqual(fix.execute(self.database, 'japanese_english', apply=True)['requeued'], 0)

    async def test_mixed_failures_are_requeued(self):
        await self.stuck(reasons=('TORRENT_METADATA_TIMEOUT', 'MEDIA_AUDIO_REQUIRED'))
        self.assertEqual(fix.execute(self.database, 'japanese_english')['eligible_jobs'], [self.job])

    async def test_no_audio_failure_is_left_in_review(self):
        await self.stuck(reasons=('TORRENT_METADATA_TIMEOUT', 'STAGING_REVIEW'))
        report = fix.execute(self.database, 'japanese_english', apply=True)
        self.assertEqual((report['eligible_jobs'], report['left_in_review']), ([], 1))

    async def test_refuses_when_english_is_not_required(self):
        await self.stuck()
        with self.assertRaises(ValueError):
            fix.execute(self.database, 'japanese', apply=True)

    # The worker tests above also run for this subclass; skip the duplicates.
    test_audio_only_failures_wait_for_dub_instead_of_review = None
    test_mixed_failures_with_one_audio_failure_wait_for_dub = None
    test_no_audio_failure_still_needs_review = None
    test_check_only_runs_when_english_is_required = None
    test_search_again_tries_only_new_dual_audio_releases = None
    test_search_again_with_nothing_new_waits_again = None


if __name__ == '__main__':
    unittest.main()
