"""Tests for FAILED vs NEEDS_REVIEW candidate failure lifecycle and failure reason preservation (AG-002, AG-003)."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.nextgen.job_contracts import Outcome, Policy
from src.nextgen.storage.cancellation import CancellationStore, cancel_job
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.release_candidates import (
    activate_candidate, fail_candidate, next_candidate,
    auto_cleanup_failed_candidate, schedule_after_cleanup,
)
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction

FIRST = 'a' * 40
SECOND = 'b' * 40


class CandidateFailureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "test.sqlite"
        db = sqlite3.connect(self.db_path)
        initialize(db)
        raw_event = json.dumps({"torrent_infohash": FIRST}).encode('utf-8')
        db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'release-discovery',?,1)", (raw_event,))
        db.executescript(f"""
            INSERT INTO identified_anime VALUES(50,100,'Test Anime');
            INSERT INTO identity_results VALUES(1,1,50,2,'{{"anime":{{"anidb_id":50,"tvdb_series_id":100}}}}',1);
            INSERT INTO user_evaluation_plans(plan_id,anidb_id,tvdb_series_id,tvdb_season,
              anchor_verification_id,anchor_episode,backfill_count,following_count,
              configuration_revision,created_at_ms) VALUES(1,50,100,1,1,1,6,3,1,0);
            INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,
              tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms)
              VALUES(1,2,1,2,9002,'{FIRST}',1);
            INSERT INTO episode_release_candidates(plan_id,anidb_episode_number,rank,info_hash,state)
              VALUES(1,2,1,'{FIRST}','READY'),
                    (1,2,2,'{SECOND}','READY');
        """)
        db.commit()
        db.close()

    def connect(self):
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA busy_timeout=5000")
        self.addCleanup(db.close)
        return db

    async def test_job_queue_outcome_fail_transitions_to_failed(self):
        from src.nextgen.handlers import HandlerDefinition
        from src.nextgen.job_contracts import Registration, Registry
        async def mock_handler(c): pass
        spec = Registration('PREPARE_ACQUISITION', HandlerDefinition('PREPARE', 'Acquisition work'), mock_handler)
        registry = Registry((spec,))
        queue = JobQueue(str(self.db_path), policy=Policy(lease_ms=5000))
        # Enqueue job
        with queue.connection() as conn, transaction(conn):
            job_id = enqueue_job(conn, component='worker', kind='PREPARE_ACQUISITION',
                                 payload={'info_hash': FIRST}, available_at_ms=0, event_id=1)
        # Claim job
        claim, _ = queue.claim()
        self.assertEqual(claim.job_id, job_id)

        invocation = 'a' * 32
        queue.start(claim, spec, invocation, registry)
        # Handler returns Outcome('fail', ...)
        outcome = Outcome('fail', 'TORRENT_METADATA_TIMEOUT')
        queue.finish(claim, outcome, spec=spec, invocation=invocation, registry=registry)

        # Verify job is FAILED (NOT NEEDS_REVIEW!)
        with self.connect() as db:
            row = db.execute("SELECT state,last_reason_code FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self.assertEqual(row, ('FAILED', 'TORRENT_METADATA_TIMEOUT'))

    async def test_auto_cleanup_enqueued_on_failed_and_preserves_reason(self):
        from src.nextgen.handlers import HandlerDefinition
        from src.nextgen.job_contracts import Registration, Registry
        async def mock_handler(c): pass
        spec = Registration('PREPARE_ACQUISITION', HandlerDefinition('PREPARE', 'Acquisition work'), mock_handler)
        registry = Registry((spec,))
        queue = JobQueue(str(self.db_path), policy=Policy(lease_ms=5000))
        with queue.connection() as conn, transaction(conn):
            # Create discover backfill parent with future available_at_ms so prepare is claimed first
            parent_id = enqueue_job(conn, component='worker', kind='DISCOVER_EPISODE_RELEASES',
                                    payload={'plan_id': 1}, available_at_ms=9999999, event_id=1)
            # Create verify release job (which starts an operation)
            verify_id = enqueue_job(conn, component='worker', kind='VERIFY_RELEASE',
                                    payload={'info_hash': FIRST}, available_at_ms=9999999, event_id=1,
                                    created_by_job_id=parent_id)
            op_id = conn.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?", (verify_id,)).fetchone()[0]
            # Create prepare acquisition child in the same operation
            job_id = enqueue_job(conn, component='worker', kind='PREPARE_ACQUISITION',
                                 payload={'info_hash': FIRST}, available_at_ms=0, event_id=1,
                                 created_by_job_id=verify_id, operation_id=op_id)
            # Activate candidate 1
            activate_candidate(conn, plan_id=1, episode=2, info_hash=FIRST)

        claim, _ = queue.claim()
        self.assertEqual(claim.job_id, job_id)

        invocation = 'a' * 32
        queue.start(claim, spec, invocation, registry)
        # Finish with fail outcome
        outcome = Outcome('fail', 'ACQUISITION_SIZE_LIMIT')
        queue.finish(claim, outcome, spec=spec, invocation=invocation, registry=registry)

        with self.connect() as db:
            # When auto-cleanup is enqueued, the operation is canceled,
            # and the failing job preserves its exact failure reason (not overwritten with JOB_CANCELED!)
            self.assertEqual(
                db.execute("SELECT state,last_reason_code FROM jobs WHERE job_id=?", (job_id,)).fetchone(),
                ('CANCELED', 'ACQUISITION_SIZE_LIMIT')
            )

            # Candidate is STILL ACTIVE during cleanup (not yet FAILED!)
            self.assertEqual(
                db.execute("SELECT rank,state,reason_code FROM episode_release_candidates WHERE rank=1").fetchone(),
                (1, 'ACTIVE', None)
            )

            # A cleanup job was automatically scheduled!
            cleanup_row = db.execute(
                "SELECT job_id,kind,payload_json FROM jobs WHERE kind='CLEANUP_CANCELED_OPERATION'"
            ).fetchone()
            self.assertIsNotNone(cleanup_row)
            cleanup_payload = json.loads(cleanup_row[2])
            # Reason is preserved in payload!
            self.assertEqual(cleanup_payload.get('target_job_id'), job_id)
            self.assertEqual(cleanup_payload.get('failure_reason'), 'ACQUISITION_SIZE_LIMIT')

            # Management action recorded
            action_row = db.execute("SELECT operation_id,action,target_job_id,job_id FROM management_actions").fetchone()
            self.assertEqual(action_row[1], 'CLEANUP')
            self.assertEqual(action_row[2], job_id)
            self.assertEqual(action_row[3], cleanup_row[0])
            op_id = action_row[0]

            # Now cleanup completes, calling schedule_after_cleanup
            with db:
                db.execute("BEGIN IMMEDIATE")
                schedule_after_cleanup(db, op_id)

            # Candidate 1 is now marked FAILED with exact failure reason!
            cand1 = db.execute("SELECT rank,state,reason_code FROM episode_release_candidates WHERE rank=1").fetchone()
            self.assertEqual(cand1, (1, 'FAILED', 'ACQUISITION_SIZE_LIMIT'))

            # Candidate 2 is ready and next fallback discovery job is scheduled
            cand2 = db.execute("SELECT rank,state,reason_code FROM episode_release_candidates WHERE rank=2").fetchone()
            self.assertEqual(cand2, (2, 'READY', None))

            fallback_job = db.execute(
                "SELECT kind,payload_json FROM jobs WHERE kind='DISCOVER_EPISODE_RELEASES' AND job_id != ?",
                (parent_id,)
            ).fetchone()
            self.assertIsNotNone(fallback_job)
            self.assertEqual(json.loads(fallback_job[1]), {'plan_id': 1, 'start': 1, 'fallback_episode': 2})

    async def test_cancellation_preserves_failed_job_reason(self):
        with self.connect() as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                parent_id = enqueue_job(db, component='worker', kind='DISCOVER_EPISODE_RELEASES',
                                        payload={'plan_id': 1}, available_at_ms=0, event_id=1)
                job_id = enqueue_job(db, component='worker', kind='PREPARE_ACQUISITION',
                                     payload={'info_hash': FIRST}, available_at_ms=0, event_id=1,
                                     created_by_job_id=parent_id)
                # Manually set job to FAILED with reason
                db.execute("UPDATE jobs SET state='FAILED', last_reason_code='ACQUISITION_AUDIO_MISSING' WHERE job_id=?", (job_id,))
                cancel_job(db, job_id, now=1000)

            # Verify that cancelling the job does NOT overwrite FAILED last_reason_code with JOB_CANCELED
            row = db.execute("SELECT state,last_reason_code FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self.assertEqual(row[0], 'CANCELED')
            self.assertEqual(row[1], 'ACQUISITION_AUDIO_MISSING')

    async def test_prepare_acquisition_size_limit_returns_fail_outcome(self):
        from src.nextgen.jobs.prepare_acquisition_job import registration as prepare_registration
        from src.nextgen.acquisition.settings import SizeLimitExceeded
        async def mock_read_metainfo(source, config, cancelled):
            raise SizeLimitExceeded({'video_mb': 500, 'limit_mb': 10})

        spec = prepare_registration(mock_read_metainfo)
        context = SimpleNamespace(
            claim=SimpleNamespace(job_id=1, attempt=1, event_id=1),
            data=lambda: {'evaluation_job_id': 1},
            configuration=SimpleNamespace(value=lambda k: 10 if 'max_episode_mb' in k else '/unused'),
            cancelled=None,
        )
        with patch('src.nextgen.jobs.prepare_acquisition_job.load_acquisition_input',
                   return_value=SimpleNamespace(info_hash=FIRST)):
            outcome = await spec.execute(context)
            self.assertEqual(outcome.kind, 'fail')
            self.assertEqual(outcome.reason_code, 'ACQUISITION_SIZE_LIMIT')

    async def test_verify_media_audio_missing_returns_fail_outcome(self):
        from src.nextgen.jobs.verify_media_job import registration as verify_registration
        from src.nextgen.acquisition.media import AudioMissing

        def faulty_verify(intent, record, download, cancelled, protected_roots=None, audio=None):
            raise AudioMissing('No audio stream', observed=(), missing=('jpn',))

        spec = verify_registration(verify=faulty_verify, load_input=lambda *a, **k: (SimpleNamespace(), SimpleNamespace()))
        context = SimpleNamespace(
            claim=SimpleNamespace(job_id=1, attempt=1, event_id=1),
            data=lambda: {'preparation_job_id': 1, 'download_job_id': 2},
            configuration=SimpleNamespace(value=lambda k: '/unused'),
            cancelled=None,
        )
        outcome = await spec.execute(context)
        self.assertEqual(outcome.kind, 'fail')
        self.assertEqual(outcome.reason_code, 'MEDIA_AUDIO_REQUIRED')

    async def test_failed_cleanup_enters_needs_review_and_halts_progression(self):
        from src.nextgen.handlers import HandlerDefinition
        from src.nextgen.job_contracts import Registration, Registry
        async def mock_handler(c): pass
        spec = Registration('CLEANUP_CANCELED_OPERATION', HandlerDefinition('CLEANUP', 'Cleanup work'), mock_handler)
        registry = Registry((spec,))
        queue = JobQueue(str(self.db_path), policy=Policy(lease_ms=5000))
        with queue.connection() as conn, transaction(conn):
            cleanup_job_id = enqueue_job(conn, component='worker', kind='CLEANUP_CANCELED_OPERATION',
                                         payload={'target_job_id': 999}, available_at_ms=0, event_id=1)
        claim, _ = queue.claim()
        self.assertEqual(claim.job_id, cleanup_job_id)

        invocation = 'a' * 32
        queue.start(claim, spec, invocation, registry)
        # Cleanup fails with operational review outcome
        outcome = Outcome('review', 'DECISION_DOWNLOAD_CLEANUP_REVIEW')
        queue.finish(claim, outcome, spec=spec, invocation=invocation, registry=registry)

        with self.connect() as db:
            # Cleanup job enters NEEDS_REVIEW
            state = db.execute("SELECT state FROM jobs WHERE job_id=?", (cleanup_job_id,)).fetchone()[0]
            self.assertEqual(state, 'NEEDS_REVIEW')

            # Progression remains halted: candidate 2 is still READY (not activated)
            cand2 = db.execute("SELECT rank,state FROM episode_release_candidates WHERE rank=2").fetchone()
            self.assertEqual(cand2, (2, 'READY'))


if __name__ == '__main__':
    unittest.main()
