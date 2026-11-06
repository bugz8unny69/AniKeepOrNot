"""Tests for series-scoped batch candidate controller and candidate pooling (WI-01, WI-02)."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.nextgen.acquisition.metadata import TorrentFile, ObservedBatchInventory
from src.nextgen.batch.provider_worker import (
    BatchCandidatePooled, BatchVerificationCompletion, make_batch_verifier,
)
from src.nextgen.configuration import ConfigurationSnapshot
from src.nextgen.jobs.verify_release_job import KIND as RELEASE_KIND, registration as release_registration, ReleaseCompletion
from src.nextgen.providers.releases import make_verifier
from src.nextgen.release_contracts import ReleaseInput, VerifiedRelease
from src.nextgen.storage.batch_candidates import (
    BatchCandidateConflict,
    get_or_create_scope,
    add_candidate,
    discover_existing_candidates,
    rank_candidates,
    reorder_candidates,
    next_candidate,
    activate_candidate,
    get_active_candidate,
    get_candidate_pool,
    fail_candidate,
    succeed_candidate,
)
from src.nextgen.storage.job_queue import JobQueue, Claim
from src.nextgen.storage.sqlite import accept_event_and_job, enqueue_job, initialize, transaction

HASH_1 = '1' * 40
HASH_2 = '2' * 40
HASH_3 = '3' * 40
HASH_META = 'd' * 40


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
        },
        "episodes": [
            {
                "anidb_episode_id": 1001,
                "anidb_episode_number": 1,
                "title": None,
                "type": "REGULAR",
                "tvdb_placement": {
                    "season": 1,
                    "episode": 1,
                },
            }
        ],
        "attributes": {},
    })


class BatchCandidateControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "test.sqlite"
        db = sqlite3.connect(self.db_path)
        initialize(db)
        db.executescript("""
            INSERT INTO identified_anime(anidb_id, tvdb_series_id, title)
                VALUES(16999, 393400, 'Cyberpunk: Edgerunners');
        """)
        db.commit()
        db.close()

    def connect(self):
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        self.addCleanup(db.close)
        return db

    def test_single_active_controller_per_scope(self):
        """WI-01: One durable batch candidate controller per identified anime and season."""
        with self.connect() as db, transaction(db):
            s1 = get_or_create_scope(db, 16999, 393400, 1)
            s1_again = get_or_create_scope(db, 16999, 393400, 1)
            self.assertEqual(s1.scope_id, s1_again.scope_id)

            # Different seasons have different scopes and can run concurrently
            s2 = get_or_create_scope(db, 16999, 393400, 2)
            self.assertNotEqual(s1.scope_id, s2.scope_id)
            self.assertEqual(s2.tvdb_season, 2)

    def test_candidate_pooling_and_deduplication(self):
        """WI-01, WI-02: Deduplicate releases by infohash while preserving each incoming event."""
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)

            # Insert two incoming events with the same infohash
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms) VALUES(1, 'autobrr', X'61', 1000)")
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms) VALUES(2, 'prowlarr', X'62', 2000)")

            c1 = add_candidate(db, scope.scope_id, HASH_1, 'Cyberpunk S01 [1080p]', source_event_id=1, seeders=10)
            c2 = add_candidate(db, scope.scope_id, HASH_1, 'Cyberpunk S01 [1080p]', source_event_id=2, seeders=15)

            # Same candidate_id (deduplicated)
            self.assertEqual(c1.candidate_id, c2.candidate_id)

            # Both events preserved in batch_candidate_events for provenance
            events = db.execute("SELECT event_id FROM batch_candidate_events WHERE scope_id=? AND info_hash=?",
                                (scope.scope_id, HASH_1)).fetchall()
            self.assertEqual([e[0] for e in events], [1, 2])

    def test_serialized_candidate_activation(self):
        """WI-01: Exactly one candidate active at a time; serialized attempts."""
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            add_candidate(db, scope.scope_id, HASH_1, 'Cyberpunk S01 Release 1')
            add_candidate(db, scope.scope_id, HASH_2, 'Cyberpunk S01 Release 2')

            self.assertTrue(activate_candidate(db, scope.scope_id, HASH_1))
            active = get_active_candidate(db, scope.scope_id)
            self.assertEqual(active.info_hash, HASH_1)

            # Attempting to activate HASH_2 while HASH_1 is ACTIVE fails closed
            with self.assertRaises(BatchCandidateConflict):
                activate_candidate(db, scope.scope_id, HASH_2)

            # Fail candidate 1 and advance to candidate 2
            fail_candidate(db, scope.scope_id, HASH_1, 'TORRENT_METADATA_TIMEOUT')
            self.assertIsNone(get_active_candidate(db, scope.scope_id))

            nxt = next_candidate(db, scope.scope_id)
            self.assertEqual(nxt.info_hash, HASH_2)
            self.assertTrue(activate_candidate(db, scope.scope_id, HASH_2))
            self.assertEqual(get_active_candidate(db, scope.scope_id).info_hash, HASH_2)

    def test_metadata_ready_candidate_ranked_ahead(self):
        """WI-02: Usable metadata-ready candidates rank ahead of discovery-needed candidates."""
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)

            c_nodata = add_candidate(db, scope.scope_id, HASH_1, 'Cyberpunk S01 [1080p] Dual Audio',
                                     seeders=50, has_metadata=0)
            c_meta = add_candidate(db, scope.scope_id, HASH_META, 'Cyberpunk S01 [1080p] Dual Audio',
                                   seeders=20, has_metadata=1, metadata_json='{"files":[]}')

            ranked = rank_candidates([c_nodata, c_meta], audio_check='japanese_english')
            # Metadata-ready candidate must be ranked first even with lower seeders
            self.assertEqual(ranked[0].info_hash, HASH_META)
            self.assertEqual(ranked[1].info_hash, HASH_1)

    def test_audio_suitability_ranking(self):
        """WI-06: Dual/Multi Audio candidates preferred when audio_check is japanese_english."""
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)

            c_single = add_candidate(db, scope.scope_id, HASH_1, 'Cyberpunk S01 [1080p]', seeders=100)
            c_dual = add_candidate(db, scope.scope_id, HASH_2, 'Cyberpunk S01 [1080p] [Dual-Audio]', seeders=10)

            ranked = rank_candidates([c_single, c_dual], audio_check='japanese_english')
            self.assertEqual(ranked[0].info_hash, HASH_2)
            self.assertEqual(ranked[1].info_hash, HASH_1)

    def test_discovery_from_existing_events_and_metadata_admissions(self):
        """WI-02: Discover existing candidates from incoming_events and metadata admissions."""
        with self.connect() as db, transaction(db):
            # Create earlier incoming event and identity result
            body = json.dumps({
                "release_name": "[SubsPlease] Cyberpunk Edgerunners - S01 (Batch) [1080p]",
                "torrent_infohash": HASH_META,
                "seeders": 42,
                "size": 5000000000,
            }).encode('utf-8')
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms) VALUES(10, 'autobrr', ?, 1000)", (body,))
            db.execute("""
                INSERT INTO identity_results(result_id, event_id, anidb_id, schema_version, series_json, validated_at_ms)
                    VALUES(1, 10, 16999, 2, ?, 1000)
            """, (make_series_json(),))
            db.execute("""
                INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms)
                    VALUES(101, 10, 'VERIFY_RELEASE', '{}', 'DONE', 0, 1000, 1000)
            """)
            db.execute("""
                INSERT INTO batch_metadata_admissions(
                    event_id, verification_job_id, info_hash, ownership_marker,
                    base_url, save_path, category, timeout_seconds, api_major,
                    state, inventory_json, created_at_ms, updated_at_ms
                ) VALUES(10, 101, ?, '0123456789abcdef0123456789abcdef',
                         'http://127.0.0.1:8080', '/downloads', 'anime', 30, 5,
                         'OWNED_STOPPED', '{"files":[{"index":0,"path":"ep1.mkv","size":100}]}',
                         1000, 1000)
            """, (HASH_META,))

            scope = get_or_create_scope(db, 16999, 393400, 1)
            discovered = discover_existing_candidates(db, scope.scope_id, 16999, 1)
            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].info_hash, HASH_META)
            self.assertEqual(discovered[0].has_metadata, 1)

    async def test_fifty_incoming_releases_produce_one_active_operation_and_candidate_pool(self):
        """WI-01 Acceptance: 50 incoming season-pack releases do not result in 50 operations."""
        # Create 50 incoming releases for Cyberpunk S01
        with self.connect() as db, transaction(db):
            for i in range(1, 51):
                h = f"{i:040x}"
                body = json.dumps({
                    "release_name": f"[Group{i}] Cyberpunk Edgerunners S01 (Batch) [1080p]",
                    "torrent_infohash": h,
                    "seeders": 50 - i,
                }).encode('utf-8')
                db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(?, 'autobrr', ?, ?, 'IDENTIFIED')",
                           (i, body, 1000 + i))
                db.execute("INSERT INTO identity_results(result_id, event_id, anidb_id, schema_version, series_json, validated_at_ms) "
                           "VALUES(?, ?, 16999, 2, ?, ?)",
                           (i, i, make_series_json(), 1000 + i))

        # Build mock batch verifier and verifier
        from src.nextgen.providers.http import Options, Credentials
        from src.nextgen.batch.verification import VerifiedBatch, VerifiedBatchFile
        from src.nextgen.acquisition.metadata import BatchInventory, TorrentFile

        async def mock_read_batch(expected, configuration, cancelled, *, claim=None, event_id=None):
            return b"dummy_metadata"

        # Mock verify_batch_files inside make_batch_verifier
        with patch('src.nextgen.batch.provider_worker.read_batch_inventory') as mock_inv, \
             patch('src.nextgen.batch.provider_worker.verify_batch_files') as mock_vf:

            def make_inv(raw, digest):
                return BatchInventory(digest, 'b' * 64, (TorrentFile(0, 'ep1.mkv', 100),))
            def make_vb(inventory, *args, **kwargs):
                vbf = VerifiedBatchFile(0, 'ep1.mkv', 1, 1, 1001, 1, 1, 2001)
                return VerifiedBatch(inventory.info_hash, 'b' * 64, 16999, 393400, 1, (vbf,))
            mock_inv.side_effect = make_inv
            mock_vf.side_effect = make_vb

            batch_v = make_batch_verifier(Options('registeredclient', 1), lambda: Credentials('private'),
                                          mock_read_batch, client_factory=lambda *a: None)
            combined = make_verifier(Options('registeredclient', 1), lambda: Credentials('private'),
                                     batch_verifier=batch_v, client_factory=lambda *a: None)
            reg = release_registration(combined, batch_kind='EVALUATE_BATCH')

            # Create context and run for release 1
            config = ConfigurationSnapshot(1, {
                'storage': {'database_path': str(self.db_path)},
                'acquisition': {'audio_check': 'japanese_english'},
            })

            from types import SimpleNamespace
            import threading

            from src.nextgen.handlers import HandlerDefinition
            from src.nextgen.job_contracts import Registration, Registry
            async def dummy_exec(c): pass
            dummy_eval = Registration('EVALUATE_BATCH', HandlerDefinition('EVALUATE_BATCH', 'Evaluate batch'), dummy_exec)
            registry = Registry((reg, dummy_eval))
            results = []
            queue = JobQueue(str(self.db_path))

            for i in range(1, 51):
                h = f"{i:040x}"
                # Enqueue and claim VERIFY_RELEASE job
                with queue.connection() as conn, transaction(conn):
                    job_id = enqueue_job(conn, component='worker', kind='VERIFY_RELEASE',
                                         payload={'anidb_id': 16999}, available_at_ms=0, event_id=i)
                claim, _ = queue.claim()
                inv = f"{i:032x}"
                queue.start(claim, spec=reg, invocation=inv, registry=registry)
                context = SimpleNamespace(
                    configuration=config,
                    claim=claim,
                    cancelled=threading.Event(),
                    data=lambda: {'anidb_id': 16999},
                    logger=None,
                )
                outcome = await reg.execute(context)
                results.append((claim.job_id, outcome))
                # Transition job
                queue.finish(claim, outcome, spec=reg, invocation=inv, registry=registry)

            # Exactly ONE release (Release 1) was activated and returned BatchVerificationCompletion
            # which routes to EVALUATE_BATCH
            for r in results[:5]:
                print(f"JOB {r[0]}: kind={r[1].kind}, reason={r[1].reason_code}, value={r[1].value}")
            active_outcomes = [r for r in results if r[1].value and type(r[1].value.verified) is BatchVerificationCompletion]
            pooled_outcomes = [r for r in results if type(r[1].value.verified) is BatchCandidatePooled]

            self.assertEqual(len(active_outcomes), 1)
            self.assertEqual(len(pooled_outcomes), 49)

            # Routing for pooled candidates returns NO successor jobs!
            self.assertEqual(reg.route(pooled_outcomes[0][1].value), ())
            # Routing for the active candidate returns EVALUATE_BATCH successor!
            self.assertEqual(len(reg.route(active_outcomes[0][1].value)), 1)
            self.assertEqual(reg.route(active_outcomes[0][1].value)[0].kind, 'EVALUATE_BATCH')

            # The candidate pool in SQLite shows all 50 candidates preserved
            with self.connect() as db:
                scope = get_or_create_scope(db, 16999, 393400, 1)
                pool = get_candidate_pool(db, scope.scope_id)
                self.assertEqual(len(pool), 50)
                active = get_active_candidate(db, scope.scope_id)
                self.assertIsNotNone(active)
                self.assertEqual(active.state, 'ACTIVE')
                ready = [c for c in pool if c.state == 'READY']
                self.assertEqual(len(ready), 49)

    async def test_single_episode_unaffected_by_batch_controller(self):
        """WI-01: Preserve independent single-episode processing; do not pool single episodes."""
        with self.connect() as db, transaction(db):
            body = json.dumps({
                "release_name": "[SubsPlease] Cyberpunk Edgerunners - 01 [1080p]",
                "torrent_infohash": HASH_1,
            }).encode('utf-8')
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'autobrr', ?, 1000, 'IDENTIFIED')", (body,))
            db.execute("INSERT INTO identity_results(result_id, event_id, anidb_id, schema_version, series_json, validated_at_ms) "
                       "VALUES(1, 1, 16999, 2, ?, 1000)", (make_series_json(),))
            enqueue_job(db, component='worker', kind=RELEASE_KIND,
                        payload={"anidb_id": 16999}, available_at_ms=0, event_id=1)

        queue = JobQueue(str(self.db_path))
        claim, _ = queue.claim()
        self.assertIsNotNone(claim)

        config = ConfigurationSnapshot(1, {
            'storage': {'database_path': str(self.db_path)},
            'acquisition': {'audio_check': 'japanese_english'},
        })

        async def mock_single_verify(source, configuration, cancelled, claim=None):
            return VerifiedRelease(source, 1, 1001, 1, 1, 2001)

        reg = release_registration(mock_single_verify, user_evaluation_kind='EVALUATE_EPISODE', batch_kind='EVALUATE_BATCH')
        context = SimpleNamespace(
            claim=claim,
            configuration=config,
            cancelled=threading.Event(),
            data=lambda: {"anidb_id": 16999},
            logger=None,
        )
        outcome = await reg.execute(context)
        self.assertIsNotNone(outcome.value)
        self.assertIsInstance(outcome.value.verified, VerifiedRelease)
        route_successors = reg.route(outcome.value)
        self.assertEqual(len(route_successors), 1)
        self.assertEqual(route_successors[0].kind, 'EVALUATE_EPISODE')

        # Ensure batch candidate scopes are completely empty for this single episode
        with self.connect() as db:
            scopes = db.execute("SELECT count(*) FROM batch_candidate_scopes").fetchone()[0]
            self.assertEqual(scopes, 0)

    def test_cancel_candidate_and_distinct_disposition(self):
        """Candidate-level cancellation marks state CANCELED and clears active slot."""
        from src.nextgen.storage.batch_candidates import cancel_candidate, get_candidate
        with self.connect() as db, transaction(db):
            scope = get_or_create_scope(db, 16999, 393400, 1)
            c1 = add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]")
            activate_candidate(db, scope.scope_id, HASH_1)
            active = get_active_candidate(db, scope.scope_id)
            self.assertEqual(active.info_hash, HASH_1)

            cancel_candidate(db, scope.scope_id, HASH_1, "USER_CANCELED")
            active_after = get_active_candidate(db, scope.scope_id)
            self.assertIsNone(active_after)

            c1_after = get_candidate(db, scope.scope_id, HASH_1)
            self.assertEqual(c1_after.state, "CANCELED")
            self.assertEqual(c1_after.reason_code, "USER_CANCELED")

            # Never reactivate the manually canceled candidate during the current cycle
            add_candidate(db, scope.scope_id, HASH_1, "[Group] Cyberpunk S01 [1080p]")
            c1_readded = get_candidate(db, scope.scope_id, HASH_1)
            self.assertEqual(c1_readded.state, "CANCELED")

    async def test_pooling_failure_does_not_silently_enable_independent_path(self):
        """Coordination error must raise and never silently fall through to independent paths."""
        from src.nextgen.batch.provider_worker import make_batch_verifier
        from src.nextgen.providers.http import Credentials, Options
        from src.nextgen.release_contracts import ReleaseInput

        source = ReleaseInput(
            1, 1, 16999, 393400,
            json.dumps({"release_name": "[SubsPlease] Cyberpunk - S01 [1080p]", "torrent_infohash": HASH_1}).encode()
        )
        # Invalid database path causing sqlite error during coordination
        bad_config = ConfigurationSnapshot(1, {
            'storage': {'database_path': '/nonexistent/dir/bad.sqlite'},
            'acquisition': {'audio_check': 'japanese_english'},
        })
        called_independent = False
        async def mock_read_metainfo(*args, **kwargs):
            nonlocal called_independent
            called_independent = True
            return b"dummy_metainfo"

        verifier = make_batch_verifier(Options('registeredclient', 1), lambda: Credentials('private'),
                                       mock_read_metainfo, client_factory=lambda *a: None)
        from src.nextgen.storage.job_queue import QueueUnavailable
        with self.assertRaises(QueueUnavailable):
            await verifier(source, bad_config, threading.Event(), claim=SimpleNamespace(job_id=1))
        self.assertFalse(called_independent)

