"""Tests for shared candidate lifecycle, audio ranking, and per-episode size validation (WI-04, WI-06, WI-07)."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.nextgen.acquisition.audio import dual_audio_tagged, is_audio_incompatible
from src.nextgen.acquisition.metadata import (
    BatchInventory, ObservedBatchInventory, TorrentFile, infohash,
)
from src.nextgen.acquisition.settings import SizeLimitExceeded
from src.nextgen.batch.provider_worker import (
    BatchVerificationCompletion, BatchCandidatePooled, make_batch_verifier,
)
from src.nextgen.batch.verification import VerifiedBatch, VerifiedBatchFile
from src.nextgen.configuration import ConfigurationSnapshot
from src.nextgen.jobs.verify_release_job import (
    KIND as RELEASE_KIND, registration as release_registration,
)
from src.nextgen.providers.http import Credentials, Options
from src.nextgen.providers.identity import delivery_title
from src.nextgen.release_contracts import ReleaseInput
from src.nextgen.storage.batch_candidates import (
    BatchCandidate, BatchScope, activate_candidate, add_candidate,
    auto_cleanup_failed_batch_candidate, exhaust_scope, fail_candidate,
    get_active_candidate, get_candidate_pool, get_or_create_scope,
    next_candidate, rank_candidates, reorder_candidates, schedule_after_cleanup_batch,
)
from src.nextgen.storage.job_queue import Claim, JobQueue
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction

HASH_1 = '1' * 40
HASH_2 = '2' * 40
HASH_3 = '3' * 40


def make_series_json(anidb_id=16999, tvdb_series_id=393400):
    return json.dumps({
        "schema_version": 2,
        "anime": {
            "anidb_id": anidb_id,
            "tvdb_series_id": tvdb_series_id,
            "title": "Cyberpunk: Edgerunners",
            "english_title": None,
            "original_title": None,
            "type": "TV",
            "year": None,
            "season": "FALL",
            "episode_count": 10,
            "air_date": "2022-09-13",
            "end_date": "2022-09-13",
            "synopsis": "A street kid trying to survive.",
            "tags": []
        },
        "episodes": [
            {
                "anidb_episode_id": 257000 + i,
                "episode_number": i,
                "episode_type": "REGULAR",
                "air_date": "2022-09-13",
                "length_minutes": 25,
                "title": f"Episode {i}",
                "original_title": None,
                "romaji_title": None,
                "tvdb_placement": {
                    "tvdb_series_id": tvdb_series_id,
                    "tvdb_season": 1,
                    "tvdb_episode": i,
                    "tvdb_episode_id": 9000 + i,
                }
            } for i in range(1, 11)
        ],
        "sources": {"anidb": True, "tvdb": True}
    })


class BatchAudioRankingTests(unittest.TestCase):
    def test_audio_incompatibility_detection(self):
        """WI-06: Explicit foreign dubs are detected as incompatible; Dual Audio is preserved."""
        # Known incompatible: French only, German only, Italian only
        self.assertTrue(is_audio_incompatible("[Group] Show - S01 [French 1080p]", "japanese_english"))
        self.assertTrue(is_audio_incompatible("[Group] Show - S01 [VOSTFR]", "japanese_english"))
        self.assertTrue(is_audio_incompatible("[Group] Show - S01 [German Dub]", "japanese_english"))
        self.assertTrue(is_audio_incompatible("[Group] Show - S01 [ITA]", "japanese_english"))

        # Dual Audio or Multi Audio overrides foreign tags
        self.assertFalse(is_audio_incompatible("[Group] Show - S01 [Dual Audio] [VF]", "japanese_english"))
        self.assertFalse(is_audio_incompatible("[Group] Show - S01 [Dual Audio 1080p]", "japanese_english"))
        self.assertFalse(is_audio_incompatible("[Group] Show - S01 [Multi Audio]", "japanese_english"))

        # Untagged or Multi-Subs is not incompatible (allowed to proceed to verification/probing)
        self.assertFalse(is_audio_incompatible("[Group] Show - S01 [Multi-Subs 1080p]", "japanese_english"))
        self.assertFalse(is_audio_incompatible("[SubsPlease] Show - S01 [1080p]", "japanese_english"))

    def test_audio_ranking_prefers_dual_audio_and_demotes_incompatible(self):
        """WI-06: Rank candidates prioritizing Dual Audio and demoting incompatible foreign dubs."""
        c_incompat = BatchCandidate(1, 10, HASH_1, "[Group] Show - S01 [French 1080p]", 1, 'READY', seeders=100)
        c_untagged = BatchCandidate(2, 10, HASH_2, "[Group] Show - S01 [1080p]", 2, 'READY', seeders=50)
        c_dual = BatchCandidate(3, 10, HASH_3, "[Group] Show - S01 [Dual Audio 1080p]", 3, 'READY', seeders=10)

        ranked = rank_candidates([c_incompat, c_untagged, c_dual], audio_check='japanese_english')
        # Dual Audio first, then untagged, then incompatible last
        self.assertEqual(ranked[0].info_hash, HASH_3)
        self.assertEqual(ranked[1].info_hash, HASH_2)
        self.assertEqual(ranked[2].info_hash, HASH_1)


class BatchLifecycleAndFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'batch_lifecycle.sqlite'
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)
        self.config = ConfigurationSnapshot(1, {
            'storage': {'database_path': str(self.db_path), 'download_root': '/downloads'},
            'acquisition': {
                'audio_check': 'japanese_english',
                'max_episode_mb': 1000,
                'max_plan_mb': 15000,
            },
            'qbittorrent': {
                'base_url': 'http://localhost:8080',
                'category': 'anime',
                'timeout_seconds': 10,
                'metadata_poll_seconds': 1,
                'metadata_poll_attempts': 2,
            },
        })

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return sqlite3.connect(self.db_path)

    async def test_candidate_failure_advances_to_next_candidate_after_cleanup(self):
        """WI-04: Failed candidate is cleaned up and controller advances to Candidate 2."""
        with self.connect() as db, transaction(db):
            # Insert Scope
            scope = get_or_create_scope(db, 16999, 393400, 1)
            # Add Candidate 1 (active) and Candidate 2 (ready)
            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Show S01 [1080p]", source_event_id=1)
            c2 = add_candidate(db, scope.scope_id, HASH_2, "[Group] Show S01 [Dual Audio 1080p]", source_event_id=2)

            op_id = db.execute(
                "INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(1000, 1000)"
            ).lastrowid
            activate_candidate(db, scope.scope_id, HASH_1, operation_id=op_id)
            reorder_candidates(db, scope.scope_id, [c1, c2])

            # Enqueue VERIFY_RELEASE for Candidate 1
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) "
                       "VALUES(1, 'autobrr', X'61', 1000, 'IDENTIFIED')")
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) "
                       "VALUES(2, 'autobrr', X'62', 1000, 'IDENTIFIED')")
            job_id = enqueue_job(db, component='worker', kind=RELEASE_KIND,
                                 payload={'anidb_id': 16999}, available_at_ms=0,
                                 event_id=1, operation_id=op_id)

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        # Simulate job failure with TORRENT_METADATA_TIMEOUT
        with self.connect() as db, transaction(db):
            # auto_cleanup_failed_batch_candidate initiates cleanup
            actions = auto_cleanup_failed_batch_candidate(db, claim)
            self.assertEqual(len(actions), 2)

            # Fast-forward cleanup completion: schedule_after_cleanup_batch advances to Candidate 2
            receipts = schedule_after_cleanup_batch(db, op_id)
            self.assertEqual(len(receipts), 1)

            # Candidate 1 is FAILED with reason
            pool = get_candidate_pool(db, scope.scope_id)
            failed_cand = [c for c in pool if c.info_hash == HASH_1][0]
            self.assertEqual(failed_cand.state, 'FAILED')

            # Candidate 2 is now ACTIVE
            active = get_active_candidate(db, scope.scope_id)
            self.assertIsNotNone(active)
            self.assertEqual(active.info_hash, HASH_2)
            self.assertEqual(active.state, 'ACTIVE')

    async def test_exhaustion_when_no_eligible_candidate_remains(self):
        """WI-04: When all candidates fail, scope becomes EXHAUSTED."""
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Show S01 [1080p]", source_event_id=1)
            op_id = db.execute(
                "INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(1000, 1000)"
            ).lastrowid
            activate_candidate(db, scope.scope_id, HASH_1, operation_id=op_id)

            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) "
                       "VALUES(1, 'autobrr', X'61', 1000, 'IDENTIFIED')")
            job_id = enqueue_job(db, component='worker', kind=RELEASE_KIND,
                                 payload={'anidb_id': 16999}, available_at_ms=0,
                                 event_id=1, operation_id=op_id)

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        with self.connect() as db, transaction(db):
            auto_cleanup_failed_batch_candidate(db, claim)
            receipts = schedule_after_cleanup_batch(db, op_id, prowlarr_fallback=False)
            self.assertEqual(receipts, ())

            scope_after = get_or_create_scope(db, 16999, 393400, 1)
            self.assertEqual(scope_after.state, 'EXHAUSTED')
            self.assertIsNone(scope_after.active_candidate_id)


class BatchSizeValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'batch_size.sqlite'
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)
            conn.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk')")
            conn.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', X'61', 1000, 'IDENTIFIED')")
            conn.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(2, 'autobrr', X'62', 1000, 'IDENTIFIED')")
            conn.commit()
        self.config = ConfigurationSnapshot(1, {
            'storage': {'database_path': str(self.db_path), 'download_root': '/downloads'},
            'acquisition': {
                'audio_check': 'japanese_english',
                'max_episode_mb': 1000,   # 1 GB per episode limit
                'max_plan_mb': 15000,    # 15 GB total plan limit
            },
            'qbittorrent': {'base_url': 'http://localhost:8080'},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return sqlite3.connect(self.db_path)

    async def test_per_episode_size_limit_rejection_without_downloading_probe(self):
        """WI-07: A season pack with an oversized episode (>1000MB) is rejected before downloading video probe."""
        # 10 episodes: 9 episodes are 800MB (valid), but 1 episode is 1200MB (>1000MB limit)
        files = [
            TorrentFile(i, f"Cyberpunk Edgerunners - S01E{i+1:02d}.mkv", 800 * 1024 * 1024 if i != 4 else 1200 * 1024 * 1024)
            for i in range(10)
        ]
        # Cumulative size is ~8400MB (well below max_plan_mb 15000), but episode 5 exceeds max_episode_mb
        inv = ObservedBatchInventory(HASH_1, tuple(files))

        verified_files = tuple(
            VerifiedBatchFile(
                i, f"Cyberpunk Edgerunners - S01E{i+1:02d}.mkv",
                i + 1, 257000 + i + 1, 257000 + i + 1, 1, i + 1, 9000 + i + 1
            )
            for i in range(10)
        )
        verified = VerifiedBatch(HASH_1, None, 16999, 393400, 1, verified_files)

        async def mock_read_batch(*a, **kw):
            return inv

        batch_v = make_batch_verifier(
            Options('registeredclient', 1),
            lambda: Credentials('private'),
            mock_read_batch,
            client_factory=lambda *a: None,
        )

        with patch('src.nextgen.batch.provider_worker.verify_batch_files', return_value=verified), \
             patch('src.nextgen.batch.provider_worker.read_batch_inventory', return_value=inv):
            raw = json.dumps({
                'release_name': '[SubsPlease] Cyberpunk Edgerunners - Season 1 [1080p]',
                'torrent_infohash': HASH_1,
            }).encode('utf-8')
            source = ReleaseInput(1, 1, 16999, 393400, raw)
            with self.assertRaises(SizeLimitExceeded) as ctx:
                await batch_v(source, self.config, threading.Event())
            self.assertEqual(ctx.exception.reason_code, 'ACQUISITION_SIZE_LIMIT')

    async def test_season_pack_within_per_episode_limit_is_accepted(self):
        """WI-07: A 8GB season pack where each episode is 800MB (<1000MB limit) is accepted."""
        files = [
            TorrentFile(i, f"Cyberpunk Edgerunners - S01E{i+1:02d}.mkv", 800 * 1024 * 1024)
            for i in range(10)
        ]
        inv = ObservedBatchInventory(HASH_1, tuple(files))
        verified_files = tuple(
            VerifiedBatchFile(
                i, f"Cyberpunk Edgerunners - S01E{i+1:02d}.mkv",
                i + 1, 257000 + i + 1, 257000 + i + 1, 1, i + 1, 9000 + i + 1
            )
            for i in range(10)
        )
        verified = VerifiedBatch(HASH_1, None, 16999, 393400, 1, verified_files)

        async def mock_read_batch(*a, **kw):
            return inv

        batch_v = make_batch_verifier(
            Options('registeredclient', 1),
            lambda: Credentials('private'),
            mock_read_batch,
            client_factory=lambda *a: None,
        )

        with patch('src.nextgen.batch.provider_worker.verify_batch_files', return_value=verified), \
             patch('src.nextgen.batch.provider_worker.read_batch_inventory', return_value=inv):
            raw = json.dumps({
                'release_name': '[SubsPlease] Cyberpunk Edgerunners - Season 1 [1080p]',
                'torrent_infohash': HASH_1,
            }).encode('utf-8')
            source = ReleaseInput(1, 1, 16999, 393400, raw)
            result = await batch_v(source, self.config, threading.Event())
            self.assertIsInstance(result, BatchVerificationCompletion)
            self.assertEqual(result.source, source)
            self.assertEqual(result.inventory, inv)
            self.assertEqual(result.verified, verified)

    def test_manual_cancellation_advances_to_next_candidate(self):
        """Manual cancellation of active batch candidate marks CANCELED and activates next candidate."""
        from src.nextgen.storage.batch_candidates import (
            cancel_candidate, get_candidate, schedule_after_cleanup_batch,
        )
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]", source_event_id=1)
            c2 = add_candidate(db, scope.scope_id, HASH_2, "[Group] Cyberpunk S01 [Dual Audio 1080p]", source_event_id=2)

            op1 = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(1000, 1000)").lastrowid
            activate_candidate(db, scope.scope_id, HASH_1, operation_id=op1)
            active = get_active_candidate(db, scope.scope_id)
            self.assertEqual(active.info_hash, HASH_1)

            # Mark operation as canceled by user
            db.execute("UPDATE job_operations SET state='CANCELED', cleanup_state='COMPLETE', reason_code='CANCELLATION_COMPLETE' WHERE operation_id=?", (op1,))

            # schedule_after_cleanup_batch runs after manual cancellation cleanup
            receipts = schedule_after_cleanup_batch(db, op1)
            self.assertEqual(len(receipts), 1)

            # Candidate 1 is marked CANCELED, not FAILED
            c1_cand = get_candidate(db, scope.scope_id, HASH_1)
            self.assertEqual(c1_cand.state, "CANCELED")
            self.assertEqual(c1_cand.reason_code, "JOB_CANCELED")

            # Candidate 2 is now ACTIVE
            active_after = get_active_candidate(db, scope.scope_id)
            self.assertIsNotNone(active_after)
            self.assertEqual(active_after.info_hash, HASH_2)

            # Candidate 1 cannot be reactivated
            add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]")
            c1_still = get_candidate(db, scope.scope_id, HASH_1)
            self.assertEqual(c1_still.state, "CANCELED")

    def test_batch_staging_success_transitions_candidate_and_scope_to_succeeded(self):
        """Successful batch staging transitions active candidate to SUCCEEDED and scope to SUCCEEDED."""
        from src.nextgen.storage.batch_candidates import succeed_candidate, get_candidate
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]", source_event_id=1)
            op1 = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(1000, 1000)").lastrowid
            activate_candidate(db, scope.scope_id, HASH_1, operation_id=op1)

            # Direct success transition (as called by mark_staged)
            succeed_candidate(db, scope.scope_id, HASH_1)

            c1_cand = get_candidate(db, scope.scope_id, HASH_1)
            self.assertEqual(c1_cand.state, "SUCCEEDED")

            scope_after = get_or_create_scope(db, 16999, 393400, 1)
            self.assertEqual(scope_after.state, "SUCCEEDED")
