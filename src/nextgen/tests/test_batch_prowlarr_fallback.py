"""Tests for Prowlarr last-resort fallback after candidate exhaustion (WI-05)."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.nextgen.acquisition.audio import dual_audio_tagged, is_audio_incompatible
from src.nextgen.acquisition.prowlarr_search import (
    BatchSearchResult, Client, DiscoveryPolicy, Options, Release, SearchUnavailable,
)
from src.nextgen.acquisition.release_titles import batch_search_queries
from src.nextgen.configuration import ConfigurationSnapshot
from src.nextgen.jobs.discover_batch_prowlarr_job import (
    KIND as BATCH_PROWLARR_KIND, registration as batch_prowlarr_registration,
)
from src.nextgen.storage.batch_candidates import (
    BatchCandidate, BatchScope, activate_candidate, add_candidate,
    auto_cleanup_failed_batch_candidate, ingest_batch_prowlarr_results, prepare_batch_prowlarr_search,
    exhaust_scope, fail_candidate, get_active_candidate, get_candidate_pool,
    get_or_create_scope, next_candidate, rank_candidates, reorder_candidates,
    schedule_after_cleanup_batch,
)
from src.nextgen.storage.job_queue import Claim, JobQueue
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction

HASH_1 = '1' * 40
HASH_2 = '2' * 40
HASH_3 = '3' * 40
HASH_4 = '4' * 40
HASH_5 = '5' * 40


class BatchSearchQueriesTests(unittest.TestCase):
    def test_batch_search_queries_basic(self):
        """WI-05: Builds season pack queries with season markers and batch terms."""
        queries = batch_search_queries(["Cyberpunk Edgerunners"], 1)
        self.assertIn("Cyberpunk Edgerunners S01", queries)
        self.assertIn("Cyberpunk Edgerunners Season 1", queries)
        self.assertIn("Cyberpunk Edgerunners Batch", queries)
        self.assertLessEqual(len(queries), 6)

    def test_batch_search_queries_prefer_dual_audio(self):
        """WI-05: Builds Dual Audio queries when audio policy requires Japanese+English."""
        queries = batch_search_queries(["Cyberpunk Edgerunners"], 1, english_title="Cyberpunk: Edgerunners", prefer_audio=True)
        self.assertTrue(any("Dual Audio" in q for q in queries))
        self.assertLessEqual(len(queries), 6)


class ProwlarrBatchSearchMethodTests(unittest.TestCase):
    def test_client_search_batch_filtering_and_deduplication(self):
        """WI-05: Filters non-batch scopes, audio incompatibilities, previously tried hashes, and deduplicates."""
        client = Client(
            Options("http://localhost:9696", 10),
            "test_api_key",
            catalog=lambda *a: None,
            policy=DiscoveryPolicy(max_resolution=1080, min_seeders=1, prefer_audio_tag=True),
        )

        # Mock Prowlarr indexer results
        mock_indexer_results = [
            # 1. Single episode release (should be rejected as UNMATCHED_SCOPE)
            {"title": "[SubsPlease] Cyberpunk Edgerunners - 01 (1080p)", "infoHash": HASH_1, "seeders": 10, "size": 1000000},
            # 2. Season pack with previously tried hash (should be rejected as PREVIOUSLY_TRIED)
            {"title": "[SubsPlease] Cyberpunk Edgerunners - S01 [1080p]", "infoHash": HASH_2, "seeders": 10, "size": 8000000},
            # 3. Explicit foreign dub without dual audio (should be rejected as AUDIO_INCOMPATIBLE)
            {"title": "[Group] Cyberpunk Edgerunners - S01 [French 1080p]", "infoHash": HASH_3, "seeders": 10, "size": 8000000},
            # 4. Valid season pack with Dual Audio (PASSES_POLICY)
            {"title": "[Group] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]", "infoHash": HASH_4, "seeders": 15, "size": 8000000},
            # 5. Duplicate of #4 with same hash from another query (should be deduplicated)
            {"title": "[Group] Cyberpunk Edgerunners - Season 1 [Dual Audio 1080p]", "infoHash": HASH_4, "seeders": 15, "size": 8000000},
            # 6. Valid season pack (PASSES_POLICY)
            {"title": "[SubsPlease] Cyberpunk Edgerunners - S01 [1080p]", "infoHash": HASH_5, "seeders": 25, "size": 7500000},
        ]

        with patch.object(client, '_get', return_value=mock_indexer_results):
            # Tried contains HASH_2
            found = client.search_batch(["Cyberpunk Edgerunners"], 1, tried=[HASH_2])

        self.assertTrue(found.complete)
        self.assertEqual(len(found.releases), 2)
        hashes = [f.info_hash for f in found.releases]
        self.assertIn(HASH_4, hashes)
        self.assertIn(HASH_5, hashes)
        self.assertNotIn(HASH_1, hashes)  # single episode
        self.assertNotIn(HASH_2, hashes)  # previously tried
        self.assertNotIn(HASH_3, hashes)  # incompatible dub

        # Diagnostic snapshot was captured
        self.assertEqual(len(client.snapshots), 1)
        snap = client.snapshots[0]
        self.assertEqual(snap['search_mode'], 'batch_fallback')
        self.assertGreaterEqual(snap['counts']['PREVIOUSLY_TRIED'], 1)
        self.assertGreaterEqual(snap['counts']['AUDIO_INCOMPATIBLE'], 1)


class BatchProwlarrFallbackExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'batch_prowlarr.sqlite'
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)
        self.config = ConfigurationSnapshot(1, {
            'storage': {'database_path': str(self.db_path), 'download_root': '/downloads'},
            'acquisition': {'audio_check': 'japanese_english'},
            'prowlarr': {'base_url': 'http://localhost:9696', 'timeout_seconds': 10},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return sqlite3.connect(self.db_path)

    async def test_candidate_failure_does_not_trigger_prowlarr_when_pool_has_ready_candidates(self):
        """WI-05: Prowlarr is not searched when another candidate is already in the queue."""
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]", source_event_id=1)
            c2 = add_candidate(db, scope.scope_id, HASH_2, "[Group] Cyberpunk S01 [Dual Audio 1080p]", source_event_id=2)
            op_id = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(1000, 1000)").lastrowid
            activate_candidate(db, scope.scope_id, HASH_1, operation_id=op_id)
            reorder_candidates(db, scope.scope_id, [c1, c2])

            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', X'61', 1000, 'IDENTIFIED')")
            enqueue_job(db, component='worker', kind='VERIFY_RELEASE', payload={'anidb_id': 16999}, available_at_ms=0, event_id=1, operation_id=op_id)

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        with self.connect() as db, transaction(db):
            auto_cleanup_failed_batch_candidate(db, claim)
            receipts = schedule_after_cleanup_batch(db, op_id)
            self.assertEqual(len(receipts), 1)

            # Candidate 2 is activated; no DISCOVER_BATCH_PROWLARR job was enqueued
            active = get_active_candidate(db, scope.scope_id)
            self.assertEqual(active.info_hash, HASH_2)
            prowlarr_jobs = db.execute("SELECT count(*) FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'").fetchone()[0]
            self.assertEqual(prowlarr_jobs, 0)

    async def test_candidate_exhaustion_enqueues_batch_prowlarr_discovery(self):
        """WI-05: When all candidates fail, controller enqueues DISCOVER_BATCH_PROWLARR."""
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]", source_event_id=1)
            op_id = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(1000, 1000)").lastrowid
            activate_candidate(db, scope.scope_id, HASH_1, operation_id=op_id)

            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', X'61', 1000, 'IDENTIFIED')")
            enqueue_job(db, component='worker', kind='VERIFY_RELEASE', payload={'anidb_id': 16999}, available_at_ms=0, event_id=1, operation_id=op_id)

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        with self.connect() as db, transaction(db):
            auto_cleanup_failed_batch_candidate(db, claim)
            receipts = schedule_after_cleanup_batch(db, op_id)
            self.assertEqual(len(receipts), 1)

            # A DISCOVER_BATCH_PROWLARR job was enqueued for the scope
            prowlarr_job = db.execute("SELECT job_id, kind, payload_json FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'").fetchone()
            self.assertIsNotNone(prowlarr_job)
            payload = json.loads(prowlarr_job[2])
            self.assertEqual(payload['scope_id'], scope.scope_id)
            self.assertEqual(payload['anidb_id'], 16999)

    def seed_identity(self, db):
        """One identified delivery for the scope, whose verified identity Prowlarr finds reuse."""
        db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
        db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', X'61', 1000, 'IDENTIFIED')")
        db.execute("INSERT INTO identity_results(result_id, event_id, anidb_id, schema_version, series_json, validated_at_ms) "
                   "VALUES(1, 1, 16999, 2, ?, 1000)", (json.dumps({'schema_version': 2, 'anime': {
                       'anidb_id': 16999, 'tvdb_series_id': 393400, 'title': 'Cyberpunk Edgerunners'}, 'episodes': []}),))

    def exhausted_scope(self):
        with self.connect() as db, transaction(db):
            self.seed_identity(db)
            scope = get_or_create_scope(db, 16999, 393400, 1)
            add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]", source_event_id=1)
            activate_candidate(db, scope.scope_id, HASH_1)
            fail_candidate(db, scope.scope_id, HASH_1, "TORRENT_METADATA_TIMEOUT")
        return scope

    def ingest(self, scope, result):
        with self.connect() as db:
            params = prepare_batch_prowlarr_search(db, scope.scope_id)
        self.assertEqual(params.tried, frozenset({HASH_1}))
        with self.connect() as db, transaction(db):
            return ingest_batch_prowlarr_results(db, params, result, audio_check='japanese_english')

    async def test_found_releases_join_the_pool_and_the_best_one_starts(self):
        """WI-05: a search's releases become candidates and the best one is scheduled."""
        scope = self.exhausted_scope()
        release = Release(title="[Group] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]", indexer="Nyaa",
                          resolution=1080, seeders=20, size=8000000, tvdb_episode_id=None, info_hash=HASH_4,
                          magnet_url=f"magnet:?xt=urn:btih:{HASH_4}", eligible_for_handoff=True)
        receipts = self.ingest(scope, BatchSearchResult((release,)))
        self.assertTrue(receipts)
        with self.connect() as db:
            active = get_active_candidate(db, scope.scope_id)
            self.assertEqual((active.info_hash, active.state), (HASH_4, 'ACTIVE'))
            self.assertEqual(len(get_candidate_pool(db, scope.scope_id)), 2)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='VERIFY_RELEASE'").fetchone(), (1,))

    async def test_prowlarr_zero_results_exhausts_scope_cleanly(self):
        """WI-15: a successful search with nothing eligible marks the season EXHAUSTED."""
        scope = self.exhausted_scope()
        self.assertEqual(self.ingest(scope, BatchSearchResult(())), ())
        with self.connect() as db:
            self.assertEqual(get_or_create_scope(db, 16999, 393400, 1).state, 'EXHAUSTED')

    async def test_an_incomplete_search_never_exhausts(self):
        """WI-15: some queries failed and nothing was found: the season stays open."""
        scope = self.exhausted_scope()
        self.ingest(scope, BatchSearchResult((), complete=False, failed=(('q', 'SERVICE_UNAVAILABLE'),)))
        with self.connect() as db:
            self.assertEqual(get_or_create_scope(db, 16999, 393400, 1).state, 'ACTIVE')

    async def test_new_candidate_arrival_reactivates_exhausted_scope(self):
        """WI-04 / WI-05: Newly arriving candidate reactivates an EXHAUSTED scope."""
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            exhaust_scope(db, scope.scope_id)
            self.assertEqual(get_or_create_scope(db, 16999, 393400, 1).state, 'EXHAUSTED')

            # A new Autobrr candidate arrives
            c_new = add_candidate(db, scope.scope_id, HASH_3, "[SubsPlease] Cyberpunk Edgerunners S01 [1080p]")
            scope_reactivated = get_or_create_scope(db, 16999, 393400, 1)
            self.assertEqual(scope_reactivated.state, 'ACTIVE')
            self.assertEqual(c_new.state, 'READY')

    async def test_rate_limiting_avoids_infinite_repeated_searches(self):
        """WI-05/WI-15: a recent search delays the next one; one search job at a time."""
        now_ms = int(time.time() * 1000)
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            # Record a recent search snapshot 60 seconds ago
            db.execute(
                "INSERT INTO prowlarr_search_snapshots(job_id, attempt, captured_at_ms, tvdb_series_id, season, search_mode, status, snapshot_json) "
                "VALUES(1, 0, ?, 393400, 1, 'batch_fallback', 'COMPLETE', '{}')",
                (now_ms - 60_000,))

            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]", source_event_id=1)
            op_id = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(1000, 1000)").lastrowid
            activate_candidate(db, scope.scope_id, HASH_1, operation_id=op_id)

            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', X'61', 1000, 'IDENTIFIED')")
            enqueue_job(db, component='worker', kind='VERIFY_RELEASE', payload={'anidb_id': 16999}, available_at_ms=0, event_id=1, operation_id=op_id)

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        with self.connect() as db, transaction(db):
            auto_cleanup_failed_batch_candidate(db, claim)
            receipts = schedule_after_cleanup_batch(db, op_id)
            # Rate limiting delays the search; it never claims the season is exhausted.
            self.assertEqual(len(receipts), 1)
            self.assertEqual(get_or_create_scope(db, 16999, 393400, 1).state, 'ACTIVE')
            available = db.execute("SELECT available_at_ms FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'").fetchone()[0]
            self.assertGreaterEqual(available, now_ms - 60_000 + 5 * 60_000)
            # A second exhaustion (a restart) does not add another search job.
            self.assertEqual(schedule_after_cleanup_batch(db, op_id), ())
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'").fetchone(), (1,))

    async def test_discover_batch_prowlarr_real_claim_contracts_and_identity_handoff(self):
        """WI-05: Real worker execution of DISCOVER_BATCH_PROWLARR, Claim contracts, and identity handoff to VERIFY_RELEASE."""
        from src.nextgen.jobs.discover_batch_prowlarr_job import registration
        from src.nextgen.storage.releases import load_release

        now_ms = int(time.time() * 1000)
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
            # Create an existing identity_results row for this anime
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', X'61', 1000, 'IDENTIFIED')")
            db.execute(
                "INSERT INTO identity_results(result_id, event_id, anidb_id, schema_version, series_json, validated_at_ms) "
                "VALUES(1, 1, 16999, 2, '{\"schema_version\":2,\"anime\":{\"anidb_id\":16999,\"tvdb_series_id\":393400,\"title\":\"Cyberpunk Edgerunners\"},\"episodes\":[]}', 1000)")

            scope = get_or_create_scope(db, 16999, 393400, 1)
            add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]", source_event_id=1)
            activate_candidate(db, scope.scope_id, HASH_1)
            fail_candidate(db, scope.scope_id, HASH_1, "TORRENT_METADATA_TIMEOUT")
            # Enqueue real DISCOVER_BATCH_PROWLARR job
            enqueue_job(
                db, component='worker', kind=BATCH_PROWLARR_KIND,
                payload={'scope_id': scope.scope_id, 'anidb_id': 16999, 'tvdb_series_id': 393400, 'tvdb_season': 1},
                available_at_ms=0, event_id=1,
            )

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()
        self.assertIsNotNone(claim)
        self.assertEqual(claim.kind, BATCH_PROWLARR_KIND)

        # Mock Prowlarr client
        mock_rel = Release(
            title="[Group] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]",
            indexer="Nyaa",
            resolution=1080,
            seeders=20,
            size=8000000000,
            tvdb_episode_id=None,
            info_hash=HASH_4,
            magnet_url=f"magnet:?xt=urn:btih:{HASH_4}",
        )
        mock_client = Mock()
        mock_client.snapshots = []
        mock_client.search_batch.return_value = BatchSearchResult((mock_rel,))

        config = ConfigurationSnapshot(1, {
            'storage': {'database_path': str(self.db_path)},
            'acquisition': {'audio_check': 'japanese_english'},
            'prowlarr': {'base_url': 'http://localhost:9696'},
        })
        mock_secrets = Mock()

        reg = registration(mock_secrets, client_factory=lambda c, s: mock_client)
        context = SimpleNamespace(
            claim=claim,
            configuration=config,
            cancelled=threading.Event(),
            data=claim.data,
            logger=None,
        )

        outcome = await reg.execute(context)
        # Outcome contract: must be 'complete' (DONE), NOT 'pass'
        self.assertEqual(outcome.kind, 'complete')

        # Verify candidate was added and activated in scope
        with self.connect() as db:
            active = get_active_candidate(db, scope.scope_id)
            self.assertIsNotNone(active)
            self.assertEqual(active.info_hash, HASH_4)

        # Next job in queue is VERIFY_RELEASE
        verify_claim, _ = queue.claim()
        self.assertIsNotNone(verify_claim)
        self.assertEqual(verify_claim.kind, 'VERIFY_RELEASE')

        # Provider-verified identity handoff: load_release must succeed under worker contracts!
        rel_input = load_release(str(self.db_path), verify_claim.event_id, 16999)
        self.assertIsNotNone(rel_input)
        self.assertEqual(rel_input.anidb_id, 16999)
        self.assertEqual(rel_input.tvdb_series_id, 393400)
        self.assertEqual(rel_input.event_id, verify_claim.event_id)
