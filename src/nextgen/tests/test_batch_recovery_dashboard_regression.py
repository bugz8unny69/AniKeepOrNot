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
from src.nextgen.acquisition.qbittorrent import DownloadCredentials, Observation, TorrentFile
from src.nextgen.api.app import Application
from src.nextgen.batch.backlog_audit import apply_batch_consolidation, audit_batch_backlog
from src.nextgen.configuration import ConfigurationSnapshot
from src.nextgen.storage.batch_candidates import (
    BatchCandidate, BatchScope, activate_candidate, add_candidate,
    auto_cleanup_failed_batch_candidate, cancel_candidate, exhaust_scope, fail_candidate,
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


MARKER = 'b' * 32
SERIES = json.dumps({'schema_version': 2, 'anime': {'anidb_id': 16999, 'tvdb_series_id': 393400,
                                                    'title': 'Cyberpunk Edgerunners'}, 'episodes': []})
FILES = (TorrentFile(0, 'Cyberpunk.S01E01.mkv', 104857600), TorrentFile(1, 'Cyberpunk.S01E02.mkv', 104857600))


class BacklogAuditAndReconciliationTests(unittest.TestCase):
    """WI-08/WI-16: the audit is read-only and consolidation uses the established cancellation lifecycle."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'batch_audit.sqlite'
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)
        self.now = int(time.time() * 1000)
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        db = sqlite3.connect(self.db_path)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def delivery(self, db, event, digest, title, kinds=('VERIFY_RELEASE',)):
        """An identified season pack and its operation's jobs (one VERIFY_RELEASE plus successors)."""
        db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) "
                   "VALUES(?, 'autobrr', ?, ?, 'IDENTIFIED')",
                   (event, json.dumps({'release_name': title, 'torrent_infohash': digest}).encode(), self.now))
        db.execute("INSERT INTO identity_results(event_id, anidb_id, schema_version, series_json, validated_at_ms) "
                   "VALUES(?, 16999, 2, ?, ?)", (event, SERIES, self.now))
        first = enqueue_job(db, component='intake', kind=kinds[0], payload={'anidb_id': 16999},
                            available_at_ms=0, event_id=event)
        op = db.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?", (first,)).fetchone()[0]
        jobs = [first] + [enqueue_job(db, component='worker', kind=k, payload={'verification_job_id': first},
                                      available_at_ms=0, event_id=event, operation_id=op) for k in kinds[1:]]
        return op, jobs

    def rows(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).fetchall()

    def two_workflows(self):
        with self.connect() as db, transaction(db):
            op1, (j1,) = self.delivery(db, 1, HASH_1, '[GroupA] Cyberpunk Edgerunners - S01 [1080p]')
            op2, (j2, j3) = self.delivery(db, 2, HASH_2, '[GroupB] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]',
                                          kinds=('VERIFY_RELEASE', 'EVALUATE_BATCH'))
            db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?", (j2,))
        return op1, j1, op2, j2, j3

    def test_audit_dry_run_lists_exactly_what_would_change_and_changes_nothing(self):
        op1, j1, op2, j2, j3 = self.two_workflows()
        with self.connect() as db, transaction(db):
            db.execute(
                "INSERT INTO batch_metadata_admissions(event_id, verification_job_id, info_hash, ownership_marker, "
                "base_url, save_path, category, timeout_seconds, api_major, state, created_at_ms, updated_at_ms) "
                "VALUES(1, ?, ?, ?, 'http://qb:8080', '/downloads/ad-' || ?, 'anime', 30, 5, 'OWNED_STOPPED', ?, ?)",
                (j1, HASH_1, MARKER, MARKER, self.now, self.now))
        observations = {
            HASH_1: Observation(HASH_1, 'pausedDL', f'/downloads/ad-{MARKER}', 'anime', ('ad-' + MARKER,), False, FILES),
            HASH_2: Observation(HASH_2, 'uploading', '/downloads/other', 'tv', (), False, FILES),
        }
        before = self.rows("SELECT * FROM jobs ORDER BY job_id"), self.rows("SELECT * FROM job_operations")
        with self.connect() as db:
            audit = audit_batch_backlog(db, observations=observations)
        self.assertEqual((self.rows("SELECT * FROM jobs ORDER BY job_id"), self.rows("SELECT * FROM job_operations")),
                         before)                                                # Dry run: nothing changed.
        scope, = audit['scopes']
        self.assertEqual((scope['anidb_id'], scope['season']), (16999, 1))
        self.assertEqual({o['association'] for o in scope['operations']}, {'verified_identity'})
        self.assertEqual(scope['primary_operation_id'], op2)                    # The most recent active one.
        self.assertEqual([d['operation_id'] for d in scope['cancel_operations']], [op1])
        self.assertEqual([j['job_id'] for j in scope['cancel_operations'][0]['jobs']], [j1])
        ownership = {c['info_hash']: c['ownership'] for c in scope['register_candidates']}
        self.assertEqual(ownership, {HASH_1: 'OWNED', HASH_2: 'FOREIGN'})
        evidence = {c['info_hash']: c['has_metadata'] for c in scope['register_candidates']}
        self.assertEqual(evidence, {HASH_1: 1, HASH_2: 1})                     # Admission, then qB file list.

    def test_payload_fields_alone_never_associate_a_job(self):
        with self.connect() as db, transaction(db):
            enqueue_job(db, component='worker', kind='EVALUATE_BATCH', payload={'anidb_id': 16999, 'season': 1},
                        available_at_ms=0)
            audit = audit_batch_backlog(db)
        self.assertEqual(audit['scopes'], [])
        self.assertEqual(len(audit['unassociated_operations']), 1)

    def test_duplicates_are_cancelled_through_the_lifecycle_never_declared_clean(self):
        op1, j1, op2, j2, j3 = self.two_workflows()
        with self.connect() as db, transaction(db):           # The duplicate is running right now.
            db.execute("UPDATE jobs SET state='RUNNING', lease_token='t', lease_expires_at_ms=? WHERE job_id=?",
                       (self.now + 60000, j1))
        with self.connect() as db, transaction(db):
            audit = audit_batch_backlog(db)
            self.assertEqual(audit['scopes'][0]['primary_operation_id'], op1)   # Running work is kept...
            # ...so make the other one the running one instead and re-plan.
            db.execute("UPDATE jobs SET state='PENDING', lease_token=NULL, lease_expires_at_ms=NULL WHERE job_id=?", (j1,))
            db.execute("UPDATE jobs SET state='RUNNING', lease_token='t', lease_expires_at_ms=? WHERE job_id=?",
                       (self.now + 60000, j3))
            audit = audit_batch_backlog(db)
            self.assertEqual(audit['scopes'][0]['primary_operation_id'], op2)
            db.execute("UPDATE jobs SET state='RUNNING', lease_token='t1', lease_expires_at_ms=? WHERE job_id=?",
                       (self.now + 60000, j1))
            audit = audit_batch_backlog(db)
            result = apply_batch_consolidation(db, audit)
        self.assertEqual(result['cleanups_requested'], 1)
        duplicate = [o for o in (op1, op2) if o != audit['scopes'][0]['primary_operation_id']][0]
        # Not declared cleaned: cancellation was requested and the cleanup job is queued.
        self.assertEqual(self.rows("SELECT state, cleanup_state FROM job_operations WHERE operation_id=?", (duplicate,)),
                         [('CANCEL_REQUESTED', 'PENDING')])
        running = self.rows("SELECT j.job_id FROM jobs j JOIN job_operation_members m USING(job_id) "
                            "WHERE m.operation_id=? AND j.state='RUNNING'", (duplicate,))
        self.assertEqual(len(running), 1)                                       # Left to drain under its lease.
        self.assertEqual(self.rows("SELECT j.kind, j.state FROM management_actions a JOIN jobs j ON j.job_id=a.job_id "
                                   "WHERE a.operation_id=? AND a.action='CLEANUP'", (duplicate,)),
                         [('CLEANUP_CANCELED_OPERATION', 'PENDING')])
        # Candidates are registered, the primary is the active one, nothing failed.
        pool = {c.info_hash: c.state for c in get_candidate_pool(self.connect(), 1)}
        self.assertEqual(sorted(pool.values()), ['ACTIVE', 'READY'])

    def test_consolidation_is_idempotent_and_keeps_candidate_history(self):
        op1, j1, op2, j2, j3 = self.two_workflows()
        with self.connect() as db, transaction(db):
            first = apply_batch_consolidation(db, audit_batch_backlog(db))
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            db.execute("UPDATE batch_candidates SET state='FAILED', reason_code='TORRENT_METADATA_TIMEOUT', "
                       "updated_at_ms=updated_at_ms+1234 WHERE info_hash=?", (HASH_1,))
        history = self.rows("SELECT info_hash,state,reason_code,created_at_ms,updated_at_ms FROM batch_candidates "
                            "ORDER BY info_hash")
        with self.connect() as db, transaction(db):
            again = apply_batch_consolidation(db, audit_batch_backlog(db))
        self.assertEqual(first['cleanups_requested'], 1)
        self.assertEqual((again['cleanups_requested'], again['candidates_registered']), (0, 0))
        self.assertEqual(self.rows("SELECT info_hash,state,reason_code,created_at_ms,updated_at_ms FROM batch_candidates "
                                   "ORDER BY info_hash"), history)
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='CLEANUP_CANCELED_OPERATION'"), [(1,)])

    def test_a_failed_cleanup_leaves_the_duplicate_pending_or_in_review(self):
        from src.nextgen.cancellation_service import CancellationService
        from src.nextgen.tests.test_torrent_submission import Secrets
        op1, j1, op2, j2, j3 = self.two_workflows()
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO batch_metadata_admissions(event_id, verification_job_id, info_hash, ownership_marker, "
                       "base_url, save_path, category, timeout_seconds, api_major, state, created_at_ms, updated_at_ms) "
                       "VALUES(1, ?, ?, ?, 'http://qb:8080', '/downloads/ad-' || ?, 'anime', 30, 5, 'OWNED_STOPPED', ?, ?)",
                       (j1, HASH_1, MARKER, MARKER, self.now, self.now))
            apply_batch_consolidation(db, audit_batch_backlog(db))

        class Down:
            def __init__(self, *args):
                pass

            def login(self):
                from src.nextgen.acquisition.submission import ClientUnavailable
                raise ClientUnavailable()
        config = ConfigurationSnapshot(1, {'storage': {'database_path': str(self.db_path)}})
        try:
            CancellationService(config, Secrets(), client_factory=Down).cleanup(j1)
        except Exception:
            pass                                       # The cleanup itself reports the failure.
        state = self.rows("SELECT state, cleanup_state FROM job_operations WHERE operation_id=?", (op1,))[0]
        self.assertNotEqual(state[1], 'COMPLETE')
        self.assertIn(state[1], ('PENDING', 'REVIEW'))


class Recorder:
    """qBittorrent stand-in for cleanup: records every call; deleting is recorded, never real."""
    calls = []

    def __init__(self, *args):
        pass

    def login(self):
        Recorder.calls.append(('login',))

    def __getattr__(self, name):
        def record(*args, **kwargs):
            Recorder.calls.append((name, *args))
            return None
        return record


class ReconciliationLifecycleTests(BacklogAuditAndReconciliationTests):
    """WI-16 acceptance: reservations, external torrents, running work and repeats, with real cleanup."""
    # Run only the tests defined here, not the inherited ones again.
    for _name in [n for n in dir(BacklogAuditAndReconciliationTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def reserve(self, owner_job):
        db = sqlite3.connect(self.db_path)        # A bare reservation row; its plan is not needed here.
        with db:
            db.execute("INSERT INTO batch_episode_reservations(plan_id, owner_job_id, file_index, anidb_id, "
                   "anidb_episode_id, tvdb_series_id, tvdb_season, tvdb_episode, tvdb_episode_id, state, "
                       "reserved_at_ms) VALUES(1, ?, 0, 16999, 1001, 393400, 1, 1, 2001, 'ACTIVE', ?)",
                       (owner_job, self.now))
        db.close()

    def cleanup(self, job):
        from src.nextgen.cancellation_service import CancellationService
        from src.nextgen.tests.test_torrent_submission import Secrets
        Recorder.calls = []
        config = ConfigurationSnapshot(1, {'storage': {'database_path': str(self.db_path)}})
        return CancellationService(config, Secrets(), client_factory=Recorder).cleanup(job)

    def reservation(self):
        return self.rows("SELECT state, ended_by_job_id FROM batch_episode_reservations")

    def test_reservations_are_released_only_by_the_authorized_cleanup(self):
        with self.connect() as db, transaction(db):
            op1, (j1, r1) = self.delivery(db, 1, HASH_1, '[GroupA] Cyberpunk Edgerunners - S01 [1080p]',
                                          kinds=('VERIFY_RELEASE', 'RESERVE_BATCH'))
            db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?", (j1,))
            self.delivery(db, 2, HASH_2, '[GroupB] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]')
        self.reserve(r1)
        with self.connect() as db, transaction(db):
            audit = audit_batch_backlog(db)
            self.assertEqual([d['operation_id'] for d in audit['scopes'][0]['cancel_operations']], [op1])
            apply_batch_consolidation(db, audit)
        self.assertEqual(self.reservation(), [('ACTIVE', None)])        # Consolidation did not release it.
        status = self.cleanup(r1)                                       # The cleanup job does.
        self.assertEqual(status['cleanup_state'], 'COMPLETE')
        state, ended_by = self.reservation()[0]
        self.assertEqual(state, 'RELEASED')
        self.assertIsNotNone(ended_by)

    def test_external_torrents_are_never_touched(self):
        with self.connect() as db, transaction(db):
            op1, (j1,) = self.delivery(db, 1, HASH_FOREIGN, '[Other] Cyberpunk Edgerunners - S01 [1080p]')
            self.delivery(db, 2, HASH_2, '[GroupB] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]')
        external = {HASH_FOREIGN: Observation(HASH_FOREIGN, 'uploading', '/downloads/other', 'tv', (), False, FILES)}
        with self.connect() as db, transaction(db):
            audit = audit_batch_backlog(db, observations=external)
            ownership = {c['info_hash']: c['ownership'] for c in audit['scopes'][0]['register_candidates']}
            self.assertEqual(ownership[HASH_FOREIGN], 'FOREIGN')
            apply_batch_consolidation(db, audit)
        self.assertEqual(self.cleanup(j1)['cleanup_state'], 'COMPLETE')
        # Nothing the journals own, so cleanup never even talked to qBittorrent.
        self.assertEqual(Recorder.calls, [])

    def test_a_running_duplicate_is_not_cleaned_until_it_drains(self):
        with self.connect() as db, transaction(db):
            op1, (j1,) = self.delivery(db, 1, HASH_1, '[GroupA] Cyberpunk Edgerunners - S01 [1080p]')
            op2, (j2,) = self.delivery(db, 2, HASH_2, '[GroupB] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]')
        claim, _ = JobQueue(str(self.db_path)).claim()                  # j1 is running under its lease.
        self.assertEqual(claim.job_id, j1)
        with self.connect() as db, transaction(db):
            audit = audit_batch_backlog(db)
            primary = audit['scopes'][0]['primary_operation_id']
            self.assertEqual(primary, op1)                              # Running work is the one kept.
            # Force the running one to be the duplicate: the other was linked to the controller first.
            scope = get_or_create_scope(db, 16999, 393400, 1)
            add_candidate(db, scope.scope_id, HASH_2, '[GroupB] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]',
                          source_event_id=2)
            activate_candidate(db, scope.scope_id, HASH_2, operation_id=op2)
            audit = audit_batch_backlog(db)
            self.assertEqual(audit['scopes'][0]['primary_operation_id'], op2)
            apply_batch_consolidation(db, audit)
        # The worker running j1 holds the operation gate (worker.py), exactly as here.
        from src.nextgen.cancellation import OperationGate
        with OperationGate(str(self.db_path)).hold(op1):
            status = self.cleanup(j1)
        self.assertNotEqual(status['cleanup_state'], 'COMPLETE')        # Still running: not declared clean.
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE job_id=?", (j1,)), [('RUNNING',)])
        from src.nextgen.storage.cancellation import CancellationStore
        CancellationStore(str(self.db_path)).acknowledge(claim)         # The worker drains...
        self.assertEqual(self.cleanup(j1)['cleanup_state'], 'COMPLETE')  # ...then cleanup completes.

    def test_repeated_runs_after_cleanup_change_nothing(self):
        with self.connect() as db, transaction(db):
            op1, (j1,) = self.delivery(db, 1, HASH_1, '[GroupA] Cyberpunk Edgerunners - S01 [1080p]')
            self.delivery(db, 2, HASH_2, '[GroupB] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]')
        with self.connect() as db, transaction(db):
            apply_batch_consolidation(db, audit_batch_backlog(db))
        self.cleanup(j1)
        tables = ('batch_candidates', 'batch_candidate_scopes', 'management_actions', 'job_operations', 'jobs')
        before = {t: self.rows(f"SELECT * FROM {t} ORDER BY 1") for t in tables}
        for _ in range(3):
            with self.connect() as db, transaction(db):
                result = apply_batch_consolidation(db, audit_batch_backlog(db))
            self.assertEqual((result['cleanups_requested'], result['candidates_registered']), (0, 0))
        self.assertEqual({t: self.rows(f"SELECT * FROM {t} ORDER BY 1") for t in tables}, before)


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

    def test_get_batch_scope_operation_view_with_canceled_candidate(self):
        """WI-09 / review correction: Operation view preserves CANCELED candidate disposition distinct from FAILED."""
        now = int(time.time() * 1000)
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
            scope = get_or_create_scope(db, 16999, 393400, 1)

            # Add and cancel candidate 1
            add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]")
            activate_candidate(db, scope.scope_id, HASH_1)
            cancel_candidate(db, scope.scope_id, HASH_1, reason_code="USER_DISLIKES_RELEASE")

            # Add ready candidate 2
            add_candidate(db, scope.scope_id, HASH_2, "[Group] Cyberpunk S01 [Dual Audio 1080p]")

            view = get_batch_scope_operation_view(db, 16999, 1)

        self.assertIsNotNone(view)
        self.assertEqual(len(view['canceled_candidates']), 1)
        canceled = view['canceled_candidates'][0]
        self.assertEqual(canceled['info_hash'], HASH_1)
        self.assertEqual(canceled['state'], 'CANCELED')
        self.assertEqual(canceled['reason_code'], 'USER_DISLIKES_RELEASE')
        self.assertEqual(canceled['disposition'], 'Canceled by user: USER_DISLIKES_RELEASE')

        # Also appears in rejected_candidates for backward compatibility
        self.assertEqual(len(view['rejected_candidates']), 1)
        self.assertEqual(view['rejected_candidates'][0]['info_hash'], HASH_1)

    def test_list_batch_scopes(self):
        """WI-09: list_batch_scopes returns overview of all batch controllers including canceled candidates."""
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
            scope = get_or_create_scope(db, 16999, 393400, 1)
            add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]")
            activate_candidate(db, scope.scope_id, HASH_1)
            cancel_candidate(db, scope.scope_id, HASH_1, reason_code="USER_CANCEL")
            add_candidate(db, scope.scope_id, HASH_2, "[Group] Cyberpunk S01 [Dual Audio 1080p]")

            scopes = list_batch_scopes(db)

        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0]['anidb_id'], 16999)
        self.assertEqual(scopes[0]['season'], 1)
        self.assertEqual(scopes[0]['total_candidates'], 2)
        self.assertEqual(scopes[0]['rejected_candidates'], 1)


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

        # Step 3: a complete Prowlarr search finds nothing -> EXHAUSTED, no loop.
        from src.nextgen.acquisition.prowlarr_search import BatchSearchResult
        from src.nextgen.storage.batch_candidates import (
            ingest_batch_prowlarr_results, prepare_batch_prowlarr_search,
        )
        with self.connect() as db:
            params = prepare_batch_prowlarr_search(db, scope.scope_id)
        self.assertEqual(params.tried, frozenset({HASH_1, HASH_2}))
        with self.connect() as db, transaction(db):
            receipts = ingest_batch_prowlarr_results(db, params, BatchSearchResult(()))
            self.assertEqual(receipts, ())

            scope_after = get_or_create_scope(db, 16999, 393400, 1)
            self.assertEqual(scope_after.state, 'EXHAUSTED')
            self.assertIsNone(scope_after.active_candidate_id)


class ProductionCandidateDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'batch_prod_discovery.sqlite'
        with sqlite3.connect(self.db_path) as conn:
            initialize(conn)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return sqlite3.connect(self.db_path)

    async def test_production_candidate_discovery_examines_qb_file_inventories(self):
        """WI-02 / review item 6: Production candidate discovery actually queries qB client inspect read-only."""
        now = int(time.time() * 1000)
        raw_new = json.dumps({'release_name': '[GroupB] Cyberpunk Edgerunners - S01 [1080p]', 'torrent_infohash': HASH_2}).encode()
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(16999, 393400, 'Cyberpunk Edgerunners')")
            # Historical incoming event for HASH_1 already in DB linked to anidb_id 16999
            raw_1 = json.dumps({'release_name': '[GroupA] Cyberpunk Edgerunners - S01 [1080p]', 'torrent_infohash': HASH_1}).encode()
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', ?, ?, 'IDENTIFIED')", (raw_1, now))
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(2, 'autobrr', ?, ?, 'IDENTIFIED')", (raw_new, now))
            series_json = json.dumps({'anidb_id': 16999, 'tvdb_series_id': 393400, 'titles': ['Cyberpunk Edgerunners'], 'episodes': []})
            db.execute("INSERT INTO identity_results(event_id, anidb_id, schema_version, series_json, validated_at_ms) "
                       "VALUES(1, 16999, 2, ?, ?)", (series_json, now))

        from src.nextgen.batch.provider_worker import make_batch_verifier
        from src.nextgen.release_contracts import ReleaseInput

        mock_qb_client = Mock()
        files = (
            TorrentFile(0, "Cyberpunk.S01E01.mkv", 104857600),
            TorrentFile(1, "Cyberpunk.S01E02.mkv", 104857600),
        )
        obs_1 = Observation(
            info_hash=HASH_1, state='pausedDL', save_path='/downloads/Cyberpunk',
            category='anime', tags=(), auto_tmm=False, files=files
        )
        mock_qb_client.inspect.side_effect = lambda h, **kw: obs_1 if h == HASH_1 else None

        mock_secrets = Mock()
        mock_secrets.qbittorrent_credentials.return_value = Mock()

        async def dummy_reader(*args, **kwargs):
            return b""

        from src.nextgen.acquisition.existing_metadata import make_existing_observer
        verifier = make_batch_verifier(
            options=Mock(),
            credentials=lambda: Mock(),
            read_batch_metainfo=dummy_reader,
            observe_existing=make_existing_observer(mock_secrets, client_factory=lambda *a, **kw: mock_qb_client),
        )
        with self.connect() as db, transaction(db):
            job = enqueue_job(db, component='intake', kind='VERIFY_RELEASE', payload={'anidb_id': 16999},
                              available_at_ms=0, event_id=2)

        cfg_store = Mock()
        cfg_store.value.side_effect = lambda k: {
            'storage.database_path': str(self.db_path),
            'acquisition.audio_check': 'japanese_english',
            'qbittorrent.base_url': 'http://localhost:8080',
            'storage.download_root': '/downloads',
            'qbittorrent.category': 'anime',
            'qbittorrent.timeout_seconds': 30,
        }.get(k, None)

        source = ReleaseInput(2, 1, 16999, 393400, raw_new)

        cancelled = asyncio.Event()
        # Call verifier. Because HASH_1 (with files in qB) was discovered, it ranks ahead of HASH_2 (without files)
        # So HASH_2 gets pooled!
        with patch('src.nextgen.acquisition.settings.download_options', return_value=Mock()):
            pooled = await verifier(source, cfg_store, cancelled, claim=SimpleNamespace(job_id=job))
        self.assertIsNotNone(pooled)
        self.assertEqual(pooled.info_hash, HASH_2)
        self.assertIsNotNone(pooled.winner_candidate_id)        # HASH_1 is started, not left idle.
        # qBittorrent was only read.
        self.assertEqual({c[0] for c in mock_qb_client.method_calls}, {'login', 'inspect'})

        # Check that HASH_1 was discovered with metadata from qB!
        with self.connect() as db:
            scope = get_or_create_scope(db, 16999, 393400, 1)
            pool = get_candidate_pool(db, scope.scope_id)
            c1 = [c for c in pool if c.info_hash == HASH_1][0]
            self.assertEqual(c1.has_metadata, 1)
            self.assertIn("Cyberpunk.S01E01.mkv", c1.metadata_json)
            self.assertEqual(db.execute("SELECT count(*) FROM batch_metadata_admissions").fetchone(), (0,))
