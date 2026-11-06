"""Tests for unified metadata acquisition service, timeouts, and restart recovery (WI-03)."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.nextgen.acquisition.metadata import (
    ObservedBatchInventory, TorrentFile, infohash, parse_batch_metainfo,
)
from src.nextgen.acquisition.metadata_service import (
    METADATA_TIMEOUT_MS, MetadataConcurrencyLimiter, MetadataSourceTimeout,
    is_metadata_expired, poll_or_stop_metadata, shared_limiter, verify_torrent_ownership,
)
from src.nextgen.acquisition.metainfo_source import (
    MetadataSourceWaiting, make_batch_reader, make_reader,
)
from src.nextgen.acquisition.legacy_magnet import make_bootstrap
from src.nextgen.acquisition.settings import DownloadCredentials, DownloadOptions
from src.nextgen.acquisition.submission import Observation
from src.nextgen.configuration import ConfigurationSnapshot
from src.nextgen.jobs.verify_release_job import registration as release_registration, KIND as RELEASE_KIND
from src.nextgen.release_contracts import ReleaseInput
from src.nextgen.storage.batch_metadata_admission import (
    BatchMetadataJournal, EarlyBatchIntent, EarlyBatchPermit,
)
from src.nextgen.storage.job_queue import Claim, JobQueue
from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent, MagnetPermit
from src.nextgen.storage.sqlite import initialize, transaction, enqueue_job
from src.nextgen.tests.test_acquisition_metadata import metadata

HASH = 'a' * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}"


class UnifiedMetadataServiceOwnershipTests(unittest.TestCase):
    def test_verify_torrent_ownership_success(self):
        marker = 'b' * 32
        obs = SimpleNamespace(
            save_path='/downloads/ad-' + marker,
            category='anime',
            auto_tmm=False,
            tags=['ad-' + marker, 'other-tag'],
        )
        self.assertTrue(verify_torrent_ownership(obs, marker, '/downloads/ad-' + marker, 'anime'))

    def test_verify_torrent_ownership_rejects_foreign_or_invalid(self):
        marker = 'b' * 32
        root = '/downloads/ad-' + marker
        # None observation
        self.assertFalse(verify_torrent_ownership(None, marker, root, 'anime'))

        # Wrong category
        obs_cat = SimpleNamespace(save_path=root, category='movies', auto_tmm=False, tags=['ad-' + marker])
        self.assertFalse(verify_torrent_ownership(obs_cat, marker, root, 'anime'))

        # Wrong save path
        obs_path = SimpleNamespace(save_path='/other/path', category='anime', auto_tmm=False, tags=['ad-' + marker])
        self.assertFalse(verify_torrent_ownership(obs_path, marker, root, 'anime'))

        # Auto TMM enabled
        obs_tmm = SimpleNamespace(save_path=root, category='anime', auto_tmm=True, tags=['ad-' + marker])
        self.assertFalse(verify_torrent_ownership(obs_tmm, marker, root, 'anime'))

        # Missing marker tag
        obs_missing = SimpleNamespace(save_path=root, category='anime', auto_tmm=False, tags=['anime'])
        self.assertFalse(verify_torrent_ownership(obs_missing, marker, root, 'anime'))

        # Conflicting foreign ad-* tag
        obs_foreign = SimpleNamespace(save_path=root, category='anime', auto_tmm=False,
                                      tags=['ad-' + marker, 'ad-' + ('c' * 32)])
        self.assertFalse(verify_torrent_ownership(obs_foreign, marker, root, 'anime'))

    def test_is_metadata_expired(self):
        now = 1000000
        # Less than 10 minutes (600,000 ms)
        self.assertFalse(is_metadata_expired(now - 599000, now, timeout_ms=600000))
        # Exactly 10 minutes
        self.assertTrue(is_metadata_expired(now - 600000, now, timeout_ms=600000))
        # More than 10 minutes
        self.assertTrue(is_metadata_expired(now - 601000, now, timeout_ms=600000))


class UnifiedMetadataConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrency_limiter_bounds_parallel_acquisitions(self):
        limiter = MetadataConcurrencyLimiter(limit=2)
        active_count = 0
        peak_count = 0

        async def worker():
            nonlocal active_count, peak_count
            async with limiter.slot():
                active_count += 1
                peak_count = max(peak_count, active_count)
                await asyncio.sleep(0.01)
                active_count -= 1

        await asyncio.gather(*(worker() for _ in range(6)))
        self.assertEqual(peak_count, 2)


class UnifiedMetadataTimeoutAndRestartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'metadata_test.sqlite'
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)
        self.config = ConfigurationSnapshot(1, {
            'storage': {'database_path': str(self.db_path), 'download_root': '/downloads'},
            'qbittorrent': {
                'base_url': 'http://localhost:8080',
                'category': 'anime',
                'timeout_seconds': 10,
                'metadata_poll_seconds': 1,
                'metadata_poll_attempts': 2,
            },
        })
        self.secrets = SimpleNamespace(qbittorrent_credentials=lambda: DownloadCredentials('user', 'pass'))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_batch_metadata_timeout_fails_job_with_reason_code(self):
        """WI-03: When a batch torrent cannot retrieve metadata within 10 minutes, timeout fails the job."""
        marker = 'b' * 32
        event_id = 1

        # Insert incoming event and batch metadata admission with created_at_ms = 0 (expired)
        import sqlite3
        with sqlite3.connect(self.db_path) as db, transaction(db):
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) "
                       "VALUES(1, 'autobrr', X'61', 0, 'IDENTIFIED')")
            job_id = enqueue_job(db, component='worker', kind=RELEASE_KIND,
                                 payload={'anidb_id': 16999}, available_at_ms=0, event_id=1)
            db.execute("INSERT INTO batch_metadata_admissions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (event_id, job_id, HASH, marker, 'http://localhost:8080',
                        '/downloads/ad-' + marker, 'anime', 10, 4, 'UNCERTAIN', None, 0, 0))

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        # Target client has no files and remains paused (metadata still pending)
        target = Mock()
        target.major = 4
        target.metadata_strategy = 'legacy_poll'
        target.inspect.return_value = Observation(
            HASH, 'pausedDL', '/downloads/ad-' + marker, 'anime', ('ad-' + marker,), False, ()
        )
        target.poll_legacy_metadata.return_value = target.inspect.return_value

        reader = make_batch_reader(
            client_factory=Mock(return_value=target),
            secrets=self.secrets,
        )

        with patch('src.nextgen.batch.qb_client.BatchClient', return_value=target), \
             self.assertRaises(MetadataSourceTimeout):
            await reader(HASH, self.config, asyncio.Event(), claim=claim, event_id=event_id)

    async def test_batch_restart_recovers_admitted_torrent_without_resending(self):
        """WI-03: Worker restart recovers an existing batch admission without issuing a second magnet send."""
        marker = 'c' * 32
        event_id = 2

        files = (TorrentFile(0, 'Show - 01.mkv', 100),)
        stored_inv = ObservedBatchInventory(HASH, files)

        import sqlite3
        with sqlite3.connect(self.db_path) as db, transaction(db):
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) "
                       "VALUES(2, 'autobrr', X'61', 1000, 'IDENTIFIED')")
            job_id = enqueue_job(db, component='worker', kind=RELEASE_KIND,
                                 payload={'anidb_id': 16999}, available_at_ms=0, event_id=2)
            # Already admitted stopped torrent
            db.execute("INSERT INTO batch_metadata_admissions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (event_id, job_id, HASH, marker, 'http://localhost:8080',
                        '/downloads/ad-' + marker, 'anime', 10, 4, 'OWNED_STOPPED', stored_inv.json(), 1000, 1000))

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        target = Mock()
        target.metadata_strategy = 'legacy_poll'
        target.inspect_owned_batch_metadata.return_value = stored_inv

        reader = make_batch_reader(
            client_factory=Mock(return_value=target),
            secrets=self.secrets,
        )

        with patch('src.nextgen.batch.qb_client.BatchClient', return_value=target):
            result = await reader(HASH, self.config, asyncio.Event(), claim=claim, event_id=event_id)
        self.assertEqual(result, stored_inv)
        # Verify add_early_batch_magnet was never called (restart reused existing admission!)
        target.add_early_batch_magnet.assert_not_called()

    async def test_single_episode_metadata_timeout_fails_with_reason_code(self):
        """WI-03: Single episode legacy bootstrap returns TORRENT_METADATA_TIMEOUT after deadline."""
        marker = 'd' * 32
        options = DownloadOptions('http://localhost:8080', '/downloads/ad-' + marker, 'anime', 10)
        from src.nextgen.storage.sonarr_magnet_intake import _fingerprint
        intent = MagnetIntent(1, 1, HASH, marker, options, 4, 'UNCERTAIN',
                              _fingerprint(1, HASH, marker, options, 4))
        probe = Mock(major=4, metadata_strategy='guarded_export')
        owned = Mock()
        owned.inspect.return_value = None

        journal = Mock()
        journal.existing.return_value = intent
        # Metadata expired is True!
        journal.metadata_expired.return_value = True

        cb = make_bootstrap(self.secrets, client_factory=Mock(side_effect=[probe, owned]),
                            journal_factory=lambda _: journal)

        source = SimpleNamespace(event_id=1, info_hash=HASH)
        import logging
        context = SimpleNamespace(
            claim=Claim(1, 1, 'PREPARE_ACQUISITION', '{}', 1, 'lease'),
            configuration=self.config,
            cancelled=asyncio.Event(),
            logger=logging.getLogger('test'),
        )

        with patch('src.nextgen.acquisition.legacy_magnet.acquisition_identity', return_value=None), \
             patch('src.nextgen.acquisition.legacy_magnet.source_magnet', return_value=MAGNET), \
             patch('src.nextgen.storage.manual_release.metainfo_for', return_value=None):
            outcome = await cb(source, context)

        self.assertEqual(outcome.kind, 'fail')
        self.assertEqual(outcome.reason_code, 'TORRENT_METADATA_TIMEOUT')

    async def test_batch_fetch_metadata_timeout(self):
        """WI-03: Web API fetchMetadata timeout when pending beyond 10 minutes."""
        import sqlite3
        with sqlite3.connect(self.db_path) as db, transaction(db):
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) "
                       "VALUES(3, 'autobrr', X'61', 0, 'IDENTIFIED')")
            # Created 11 minutes ago
            job_id = enqueue_job(db, component='worker', kind=RELEASE_KIND,
                                 payload={'anidb_id': 16999}, available_at_ms=0, event_id=3)
            db.execute("UPDATE jobs SET created_at_ms=? WHERE job_id=?", (0, job_id))

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        target = Mock()
        target.metadata_strategy = 'fetch'
        target.fetch_metadata.return_value = None  # Pending resolution

        reader = make_batch_reader(
            client_factory=Mock(return_value=target),
            secrets=self.secrets,
        )

        with self.assertRaises(MetadataSourceTimeout):
            await reader(HASH, self.config, asyncio.Event(), claim=claim, event_id=3)

    async def test_verify_release_job_returns_failure_outcome_on_metadata_timeout(self):
        """WI-03: verify_release_job catches MetadataSourceTimeout and marks the job failed."""
        async def mock_timeout_verify(source, configuration, cancelled, claim=None):
            raise MetadataSourceTimeout()

        reg = release_registration(mock_timeout_verify, trial_kind='EVALUATE_EPISODE', batch_kind='EVALUATE_BATCH')
        context = SimpleNamespace(
            claim=Claim(1, 1, RELEASE_KIND, '{"anidb_id": 16999}', 1, 'lease'),
            configuration=self.config,
            cancelled=asyncio.Event(),
            data=lambda: {'anidb_id': 16999},
            logger=None,
        )

        with patch('src.nextgen.jobs.verify_release_job.load_release', return_value=SimpleNamespace(event_id=1, anidb_id=16999)):
            outcome = await reg.execute(context)

        self.assertEqual(outcome.kind, 'fail')
        self.assertEqual(outcome.reason_code, 'TORRENT_METADATA_TIMEOUT')

