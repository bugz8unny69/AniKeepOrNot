"""Tests for backlog reconciliation dry-run, dashboard visibility, and end-to-end regression (WI-08, WI-09, WI-10)."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.nextgen.acquisition.metadata import infohash
from src.nextgen.acquisition.qbittorrent import DownloadCredentials
from src.nextgen.api.app import Application
from src.nextgen.batch.backlog_audit import apply_batch_consolidation, audit_batch_backlog
from src.nextgen.configuration import ConfigurationSnapshot
from src.nextgen.storage.batch_candidates import (
    BatchCandidate, BatchScope, activate_candidate, add_candidate,
    auto_cleanup_failed_batch_candidate, exhaust_scope, fail_candidate,
    get_active_candidate, get_batch_scope_operation_view, get_candidate_pool,
    get_or_create_scope, list_batch_scopes, next_candidate, rank_candidates,
    reorder_candidates, schedule_after_cleanup_batch,
)
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction

HASH_1 = '1' * 40
HASH_2 = '2' * 40
HASH_3 = '3' * 40
HASH_FOREIGN = 'f' * 40
HASH_UNCERTAIN = 'a' * 40


class BacklogAuditAndReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'batch_audit.sqlite'
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return sqlite3.connect(self.db_path)

    def test_audit_dry_run_identifies_jobs_duplicates_and_torrent_ownership(self):
        """WI-08: Audit dry-run discovers duplicate workflows, candidates, and owned/foreign torrents."""
        now = int(time.time() * 1000)
        marker_hex = 'b' * 32
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")

            # Insert batch events
            raw_1 = json.dumps({'release_name': '[GroupA] Cyberpunk Edgerunners - S01 [1080p]', 'torrent_infohash': HASH_1}).encode()
            raw_2 = json.dumps({'release_name': '[GroupB] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]', 'torrent_infohash': HASH_2}).encode()
            raw_foreign = json.dumps({'release_name': '[ForeignGroup] Cyberpunk Edgerunners - S01 [1080p]', 'torrent_infohash': HASH_FOREIGN}).encode()

            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', ?, ?, 'IDENTIFIED')", (raw_1, now))
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(2, 'autobrr', ?, ?, 'IDENTIFIED')", (raw_2, now))
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(3, 'autobrr', ?, ?, 'IDENTIFIED')", (raw_foreign, now))

            # Insert operations (simulate 2 competing active workflows for same series)
            op1 = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(?,?)", (now, now)).lastrowid
            op2 = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(?,?)", (now, now)).lastrowid

            j1 = enqueue_job(db, component='worker', kind='VERIFY_RELEASE', payload={'anidb_id': 16999}, available_at_ms=0, event_id=1, operation_id=op1)
            enqueue_job(db, component='worker', kind='EVALUATE_BATCH', payload={'anidb_id': 16999, 'season': 1}, available_at_ms=0, event_id=2, operation_id=op2)

            # Insert metadata admission with ownership marker for HASH_1
            db.execute(
                "INSERT INTO batch_metadata_admissions("
                "event_id, verification_job_id, info_hash, ownership_marker, "
                "base_url, save_path, category, timeout_seconds, api_major, state, created_at_ms, updated_at_ms) "
                "VALUES(1, ?, ?, ?, 'http://localhost:8080', '/downloads/Cyberpunk', 'anime', 30, 5, 'OWNED_STOPPED', ?, ?)",
                (j1, HASH_1, marker_hex, now, now))

        # Simulated qB torrents map
        qb_torrents = {
            # HASH_1 is OWNED (matching marker tag, category, and save_path)
            HASH_1: SimpleNamespace(
                hash=HASH_1, name='Cyberpunk Edgerunners S01', category='anime',
                tags=['ad-' + marker_hex], auto_tmm=False, save_path="/downloads/Cyberpunk",
                has_metadata=True, files=[SimpleNamespace(name="E01.mkv", size=1000)]
            ),
            # HASH_FOREIGN is FOREIGN (different category, foreign torrent, must not be touched)
            HASH_FOREIGN: SimpleNamespace(
                hash=HASH_FOREIGN, name='Cyberpunk S01 Other', category='movies',
                tags=['foreign-group'], auto_tmm=False, save_path="/downloads/movies",
                has_metadata=True, files=[]
            ),
            # HASH_UNCERTAIN has category anime but lacks durable ownership marker
            HASH_UNCERTAIN: SimpleNamespace(
                hash=HASH_UNCERTAIN, name='Cyberpunk S01 Uncertain', category='anime',
                tags=['anime'], auto_tmm=False, save_path="/downloads/anime",
                has_metadata=True, files=[]
            ),
        }

        with self.connect() as db:
            audit = audit_batch_backlog(db, qb_torrents=qb_torrents)

        self.assertEqual(audit['total_batch_jobs'], 2)
        self.assertEqual(audit['unique_scopes_count'], 1)
        self.assertEqual(audit['duplicate_workflows_count'], 1)
        self.assertEqual(audit['owned_torrents'], 1)
        self.assertEqual(audit['foreign_torrents'], 1)

        plan = audit['consolidation_plan'][0]
        self.assertEqual(plan['anidb_id'], 16999)
        self.assertEqual(plan['season'], 1)
        self.assertTrue(plan['is_duplicate_workflow'])
        self.assertGreaterEqual(plan['candidates_count'], 2)

    def test_consolidation_migrates_candidates_into_controller_without_deleting_foreign(self):
        """WI-08: Consolidation migrates candidate facts safely into controller without altering foreign torrents."""
        now = int(time.time() * 1000)
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
            raw_1 = json.dumps({'release_name': '[GroupA] Cyberpunk Edgerunners - S01 [1080p]', 'torrent_infohash': HASH_1}).encode()
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', ?, ?, 'IDENTIFIED')", (raw_1, now))
            op1 = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(?,?)", (now, now)).lastrowid
            enqueue_job(db, component='worker', kind='VERIFY_RELEASE', payload={'anidb_id': 16999}, available_at_ms=0, event_id=1, operation_id=op1)

            audit = audit_batch_backlog(db)
            result = apply_batch_consolidation(db, audit)

            self.assertEqual(result['migrated_scopes'], 1)
            self.assertGreaterEqual(result['migrated_candidates'], 1)

            scope = get_or_create_scope(db, 16999, 393400, 1)
            self.assertEqual(scope.state, 'ACTIVE')
            pool = get_candidate_pool(db, scope.scope_id)
            self.assertEqual(len(pool), 1)
            self.assertEqual(pool[0].info_hash, HASH_1)


class DashboardBatchVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'batch_dashboard.sqlite'
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return sqlite3.connect(self.db_path)

    def test_get_batch_scope_operation_view_structure(self):
        """WI-09: Operation view exposes active candidate, ranked candidates, rejected candidates, and Prowlarr state."""
        now = int(time.time() * 1000)
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
            scope = get_or_create_scope(db, 16999, 393400, 1)

            # Failed candidate (activate and fail first so active slot becomes clear)
            c_failed = add_candidate(db, scope.scope_id, HASH_3, "[Group] Cyberpunk S01 [French 1080p]")
            activate_candidate(db, scope.scope_id, HASH_3)
            fail_candidate(db, scope.scope_id, HASH_3, "MEDIA_AUDIO_REQUIRED")

            # Active candidate
            c_active = add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [Dual Audio 1080p]", seeders=20, size_bytes=8000000)
            op_id = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(?,?)", (now, now)).lastrowid
            activate_candidate(db, scope.scope_id, HASH_1, operation_id=op_id)

            # Ready candidate
            c_ready = add_candidate(db, scope.scope_id, HASH_2, "[Group] Cyberpunk S01 [1080p]", seeders=15, size_bytes=7500000)

            # Prowlarr search snapshot
            db.execute(
                "INSERT INTO prowlarr_search_snapshots(job_id, attempt, captured_at_ms, tvdb_series_id, season, search_mode, status, snapshot_json) "
                "VALUES(1, 0, ?, 393400, 1, 'batch_fallback', 'COMPLETE', '{}')",
                (now - 120_000,))

            view = get_batch_scope_operation_view(db, 16999, 1)

        self.assertIsNotNone(view)
        self.assertEqual(view['anidb_id'], 16999)
        self.assertEqual(view['season'], 1)
        self.assertEqual(view['title'], 'Cyberpunk Edgerunners')
        self.assertEqual(view['state'], 'ACTIVE')

        # Active candidate details
        self.assertIsNotNone(view['active_candidate'])
        self.assertEqual(view['active_candidate']['info_hash'], HASH_1)

        # Ranked candidates in pool
        self.assertEqual(len(view['ranked_candidates']), 1)
        self.assertEqual(view['ranked_candidates'][0]['info_hash'], HASH_2)

        # Rejected candidates with failure reason
        self.assertEqual(len(view['rejected_candidates']), 1)
        self.assertEqual(view['rejected_candidates'][0]['info_hash'], HASH_3)
        self.assertEqual(view['rejected_candidates'][0]['reason_code'], 'MEDIA_AUDIO_REQUIRED')

        # Prowlarr search diagnostics
        self.assertIsNotNone(view['last_prowlarr_search'])
        self.assertEqual(view['last_prowlarr_search']['status'], 'COMPLETE')

    def test_list_batch_scopes(self):
        """WI-09: list_batch_scopes returns overview of all batch controllers."""
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
            scope = get_or_create_scope(db, 16999, 393400, 1)
            add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]")
            activate_candidate(db, scope.scope_id, HASH_1)

            scopes = list_batch_scopes(db)

        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0]['anidb_id'], 16999)
        self.assertEqual(scopes[0]['season'], 1)
        self.assertEqual(scopes[0]['total_candidates'], 1)


class EndToEndBatchRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'batch_e2e.sqlite'
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return sqlite3.connect(self.db_path)

    async def test_full_candidate_lifecycle_fallback_exhaustion_regression(self):
        """WI-10: End-to-end test simulating multiple incoming releases, fallback on failure, Prowlarr search, and exhaustion."""
        now = int(time.time() * 1000)
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
            scope = get_or_create_scope(db, 16999, 393400, 1)

            # Candidate 1: 1080p release
            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]", source_event_id=1, seeders=10)
            # Candidate 2: Dual Audio release
            c2 = add_candidate(db, scope.scope_id, HASH_2, "[Group] Cyberpunk S01 [Dual Audio 1080p]", source_event_id=2, seeders=20)

            # Rank pool (c2 Dual Audio is ranked #1, c1 is #2)
            ranked = rank_candidates([c1, c2], audio_check='japanese_english')
            reorder_candidates(db, scope.scope_id, ranked)
            self.assertEqual(ranked[0].info_hash, HASH_2)

            # Activate #1 candidate (c2)
            op1 = db.execute("INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(?,?)", (now, now)).lastrowid
            activate_candidate(db, scope.scope_id, HASH_2, operation_id=op1)

            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', X'61', ?, 'IDENTIFIED')", (now,))
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(2, 'autobrr', X'62', ?, 'IDENTIFIED')", (now,))
            enqueue_job(db, component='worker', kind='VERIFY_RELEASE', payload={'anidb_id': 16999}, available_at_ms=0, event_id=2, operation_id=op1)

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()

        # Step 1: Candidate 2 fails (e.g. metadata timeout). Cleanup runs and schedule_after_cleanup_batch advances to Candidate 1.
        with self.connect() as db, transaction(db):
            auto_cleanup_failed_batch_candidate(db, claim)
            receipts = schedule_after_cleanup_batch(db, op1)
            self.assertEqual(len(receipts), 1)

            # Candidate 2 is FAILED, Candidate 1 is ACTIVE
            active = get_active_candidate(db, scope.scope_id)
            self.assertEqual(active.info_hash, HASH_1)
            pool = get_candidate_pool(db, scope.scope_id)
            failed = [c for c in pool if c.info_hash == HASH_2][0]
            self.assertEqual(failed.state, 'FAILED')

        # Step 2: Candidate 1 also fails. Candidate pool is now exhausted.
        # schedule_after_cleanup_batch enqueues DISCOVER_BATCH_PROWLARR.
        claim2, _ = queue.claim()
        with self.connect() as db, transaction(db):
            op2 = db.execute("SELECT operation_id FROM batch_candidate_scopes WHERE scope_id=?", (scope.scope_id,)).fetchone()[0]
            auto_cleanup_failed_batch_candidate(db, claim2)
            receipts = schedule_after_cleanup_batch(db, op2)
            self.assertEqual(len(receipts), 1)

            prowlarr_job = db.execute("SELECT job_id, kind FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'").fetchone()
            self.assertIsNotNone(prowlarr_job)

        # Step 3: Prowlarr fallback search runs with 0 results -> scope becomes EXHAUSTED cleanly without infinite loop.
        mock_client = Mock()
        mock_client.snapshots = []
        mock_client.search_batch.return_value = ()

        with self.connect() as db, transaction(db):
            from src.nextgen.storage.batch_candidates import execute_batch_prowlarr_fallback
            receipts = execute_batch_prowlarr_fallback(db, scope.scope_id, mock_client)
            self.assertEqual(receipts, ())

            scope_after = get_or_create_scope(db, 16999, 393400, 1)
            self.assertEqual(scope_after.state, 'EXHAUSTED')
            self.assertIsNone(scope_after.active_candidate_id)
