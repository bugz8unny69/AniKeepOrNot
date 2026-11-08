"""Offline candidate state transitions; no qBittorrent calls."""
import sqlite3
import unittest

from src.nextgen.storage.release_candidates import (
    CandidateConflict, activate_candidate, fail_candidate, next_candidate,
    succeed_candidate, mark_published,
)

FIRST='a'*40
SECOND='b'*40


class CandidateStateTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.execute(
            "CREATE TABLE sonarr_episode_candidates("
            "plan_id INTEGER,anidb_episode_number INTEGER,rank INTEGER,info_hash TEXT,"
            "state TEXT DEFAULT 'READY',reason_code TEXT,"
            "PRIMARY KEY(plan_id,anidb_episode_number,rank))")
        self.db.executemany(
            "INSERT INTO sonarr_episode_candidates"
            "(plan_id,anidb_episode_number,rank,info_hash) VALUES(1,2,?,?)",
            ((1,FIRST),(2,SECOND)))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_failure_activates_next_candidate_without_reusing_first(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.assertEqual(next_candidate(self.db,plan_id=1,episode=2),(1,FIRST))
            self.assertTrue(activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST))
            self.assertFalse(activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST))
            with self.assertRaises(CandidateConflict):
                activate_candidate(self.db,plan_id=1,episode=2,info_hash=SECOND)
            fail_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST,
                           reason_code='TORRENT_METADATA_INVALID')
            self.assertEqual(next_candidate(self.db,plan_id=1,episode=2),(2,SECOND))
            self.assertTrue(activate_candidate(self.db,plan_id=1,episode=2,info_hash=SECOND))
            succeed_candidate(self.db,plan_id=1,episode=2,info_hash=SECOND)
            self.assertIsNone(next_candidate(self.db,plan_id=1,episode=2))
        self.assertEqual(self.db.execute(
            "SELECT state FROM sonarr_episode_candidates ORDER BY rank").fetchall(),
            [('FAILED',),('SUCCEEDED',)])

    def test_published_sonarr_event_marks_only_its_active_candidate(self):
        self.db.executescript("""
            CREATE TABLE incoming_events(event_id INTEGER,source TEXT);
            CREATE TABLE jobs(job_id INTEGER,event_id INTEGER,kind TEXT,
                              created_by_job_id INTEGER,payload_json TEXT);
            CREATE TABLE trial_plans(plan_id INTEGER,anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_episode_selections(
                plan_id INTEGER,anidb_episode_number INTEGER,info_hash TEXT);
            INSERT INTO incoming_events VALUES(25,'sonarr-discovery');
            INSERT INTO incoming_events VALUES(26,'other');
            INSERT INTO jobs VALUES(2,25,'DISCOVER_SONARR_BACKFILL',NULL,'{"plan_id":1}');
            INSERT INTO jobs VALUES(3,25,'VERIFY_RELEASE',2,'{}');
            INSERT INTO trial_plans VALUES(1,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER);
        """)
        self.db.execute(
            "INSERT INTO sonarr_episode_selections VALUES(1,2,?)",(FIRST,))
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
            self.assertFalse(mark_published(self.db,event_id=26,info_hash=FIRST))
            self.assertTrue(mark_published(self.db,event_id=25,info_hash=FIRST))
        self.assertEqual(self.db.execute(
            "SELECT state FROM sonarr_episode_candidates WHERE rank=1").fetchone(),
            ('SUCCEEDED',))

    def test_untried_candidate_cannot_fail_or_complete(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(CandidateConflict):
                fail_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST,
                               reason_code='TORRENT_METADATA_INVALID')
            with self.assertRaises(CandidateConflict):
                succeed_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
            with self.assertRaises(CandidateConflict):
                activate_candidate(self.db,plan_id=1,episode=2,info_hash=SECOND)


class FallbackWiringTests(unittest.TestCase):
    setUp=CandidateStateTests.setUp
    tearDown=CandidateStateTests.tearDown
    def test_cleanup_failure_schedules_existing_discovery_job_once(self):
        from unittest.mock import patch
        from src.nextgen.storage.release_candidates import schedule_after_cleanup
        self.db.executescript("""
            CREATE TABLE job_history(history_id INTEGER,job_id INTEGER,
                event_code TEXT,reason_code TEXT);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,
                target_job_id INTEGER,job_id INTEGER,created_at_ms INTEGER);
            CREATE TABLE jobs(job_id INTEGER,event_id INTEGER,kind TEXT,
                created_by_job_id INTEGER,payload_json TEXT);
            CREATE TABLE incoming_events(event_id INTEGER,source TEXT,raw_body TEXT);
            CREATE TABLE trial_plans(plan_id INTEGER,anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,
                anidb_episode_number INTEGER,tvdb_episode_id INTEGER,info_hash TEXT);
            CREATE TABLE episode_reservations(reservation_id INTEGER,
                state TEXT);
            CREATE TABLE acquisition_plans(reservation_id INTEGER,
                info_hash TEXT);
            INSERT INTO trial_plans VALUES(1,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER);
            INSERT INTO jobs VALUES(10,9,'DISCOVER_SONARR_BACKFILL',
                NULL,'{"plan_id":1}');
            INSERT INTO jobs VALUES(11,25,'VERIFY_RELEASE',10,'{}');
            INSERT INTO jobs VALUES(30,25,'CLEANUP_CANCELED_OPERATION',11,'{}');
            INSERT INTO management_actions VALUES(7,'CLEANUP',11,30,1);
            INSERT INTO job_operation_members VALUES(11,7);
            INSERT INTO job_history VALUES(1,11,'NEEDS_REVIEW',
                'ACQUISITION_METADATA_INVALID');
        """)
        self.db.execute(
            "INSERT INTO incoming_events VALUES(25,'sonarr-discovery',?)",
            ('{"torrent_infohash":"'+FIRST+'"}',))
        self.db.execute("INSERT INTO sonarr_episode_selections VALUES(1,2,901,?)",
                        (FIRST,))
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
        with patch('src.nextgen.storage.sqlite.enqueue_job_with_receipt',
                   return_value=(99,'receipt')) as enqueue:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.assertEqual(schedule_after_cleanup(self.db,7),('receipt',))
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.kwargs['kind'],
                             'DISCOVER_SONARR_BACKFILL')
            self.assertEqual(enqueue.call_args.kwargs['payload'],
                {'plan_id':1,'start':1,'fallback_episode':2})
        self.assertEqual(self.db.execute(
            "SELECT state FROM sonarr_episode_candidates ORDER BY rank").fetchall(),
            [('FAILED',),('READY',)])

    def test_oversized_webhook_release_enqueues_existing_cleanup(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.nextgen.storage.release_candidates import auto_cleanup_oversized
        self.db.executescript("""
            CREATE TABLE incoming_events(event_id INTEGER,source TEXT,raw_body TEXT);
            CREATE TABLE jobs(job_id INTEGER,event_id INTEGER,kind TEXT,last_reason_code TEXT);
            INSERT INTO incoming_events VALUES(25,'autobrr','{}');
            INSERT INTO incoming_events VALUES(26,'sonarr-discovery','{}');
            INSERT INTO jobs VALUES(12,25,'PREPARE_ACQUISITION','ACQUISITION_SIZE_LIMIT');
            INSERT INTO jobs VALUES(13,25,'PREPARE_ACQUISITION','ACQUISITION_METADATA_INVALID');
            INSERT INTO jobs VALUES(14,26,'PREPARE_ACQUISITION','ACQUISITION_SIZE_LIMIT');
        """)
        with patch('src.nextgen.storage.release_candidates.request_automatic_cleanup',
                   return_value=('cleanup',)) as cleanup:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.assertEqual(auto_cleanup_oversized(self.db,SimpleNamespace(
                    job_id=12,event_id=25,kind='PREPARE_ACQUISITION')),('cleanup',))
                # Other reviews, and Sonarr candidates (own fallback), are untouched.
                for job,event in ((13,25),(14,26)):
                    self.assertEqual(auto_cleanup_oversized(self.db,SimpleNamespace(
                        job_id=job,event_id=event,kind='PREPARE_ACQUISITION')),())
        cleanup.assert_called_once()

    def test_failed_sonarr_job_enqueues_existing_cleanup_automatically(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.nextgen.storage.release_candidates import auto_cleanup_failed_candidate
        self.db.executescript("""
            CREATE TABLE incoming_events(event_id INTEGER,source TEXT,raw_body TEXT);
            CREATE TABLE jobs(job_id INTEGER,event_id INTEGER,kind TEXT,
                created_by_job_id INTEGER,payload_json TEXT);
            CREATE TABLE trial_plans(plan_id INTEGER,anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,
                anidb_episode_number INTEGER,info_hash TEXT);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE job_operations(operation_id INTEGER,state TEXT,cleanup_state TEXT,reason_code TEXT);
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,
                target_job_id INTEGER,job_id INTEGER,created_at_ms INTEGER);
            INSERT INTO incoming_events VALUES(25,'sonarr-discovery',
                '{"torrent_infohash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}');
            INSERT INTO jobs VALUES(10,9,'DISCOVER_SONARR_BACKFILL',
                NULL,'{"plan_id":1}');
            INSERT INTO jobs VALUES(11,25,'VERIFY_RELEASE',10,'{}');
            INSERT INTO jobs VALUES(12,25,'PREPARE_ACQUISITION',11,'{}');
            INSERT INTO job_operation_members VALUES(11,7);
            INSERT INTO job_operation_members VALUES(12,7);
            INSERT INTO job_operations(operation_id,state) VALUES(7,'ACTIVE');
            INSERT INTO trial_plans VALUES(1,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER);
        """)
        self.db.execute(
            "INSERT INTO sonarr_episode_selections VALUES(1,2,?)",(FIRST,))
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
        claim=SimpleNamespace(job_id=12,event_id=25,kind='PREPARE_ACQUISITION')
        with patch('src.nextgen.storage.cancellation.CancellationStore.request_in_transaction',
                   return_value=(7,())) as cancel, patch(
                   'src.nextgen.storage.sqlite.enqueue_job_with_receipt',
                   return_value=(20,'new-cleanup-job')) as enqueue:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                result=auto_cleanup_failed_candidate(self.db,claim)
            self.assertEqual(result,('new-cleanup-job',))
            cancel.assert_called_once()
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.kwargs['kind'],
                             'CLEANUP_CANCELED_OPERATION')
            self.assertEqual(self.db.execute(
                'SELECT operation_id,action,target_job_id,job_id '
                'FROM management_actions').fetchall(),
                [(7,'CLEANUP',12,20)])
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.assertEqual(auto_cleanup_failed_candidate(self.db,claim),())
            self.assertEqual(enqueue.call_count,1)

    def test_manual_cancel_never_advances_to_candidate_b(self):
        from src.nextgen.storage.release_candidates import schedule_after_cleanup
        self.db.executescript("""
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,
                target_job_id INTEGER,job_id INTEGER,created_at_ms INTEGER);
            CREATE TABLE job_history(history_id INTEGER,job_id INTEGER,
                event_code TEXT,reason_code TEXT);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE jobs(job_id INTEGER,event_id INTEGER,kind TEXT,
                created_by_job_id INTEGER,payload_json TEXT);
            CREATE TABLE incoming_events(event_id INTEGER,source TEXT,raw_body TEXT);
            CREATE TABLE trial_plans(plan_id INTEGER,anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,
                anidb_episode_number INTEGER,tvdb_episode_id INTEGER,info_hash TEXT);
            CREATE TABLE episode_reservations(reservation_id INTEGER,state TEXT);
            CREATE TABLE acquisition_plans(reservation_id INTEGER,info_hash TEXT);
            INSERT INTO trial_plans VALUES(1,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER);
            INSERT INTO jobs VALUES(10,9,'DISCOVER_SONARR_BACKFILL',
                NULL,'{"plan_id":1}');
            INSERT INTO jobs VALUES(11,25,'VERIFY_RELEASE',10,'{}');
            INSERT INTO jobs VALUES(30,25,'CLEANUP_CANCELED_OPERATION',NULL,'{}');
            INSERT INTO management_actions VALUES(7,'CLEANUP',11,30,1);
            INSERT INTO job_operation_members VALUES(11,7);
            INSERT INTO job_history VALUES(1,11,'NEEDS_REVIEW',
                'ACQUISITION_METADATA_INVALID');
        """)
        self.db.execute(
            "INSERT INTO incoming_events VALUES(25,'sonarr-discovery',?)",
            ('{"torrent_infohash":"'+FIRST+'"}',))
        self.db.execute(
            "INSERT INTO sonarr_episode_selections VALUES(1,2,901,?)",
            (FIRST,))
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
            self.assertEqual(schedule_after_cleanup(self.db,7),())
        self.assertEqual(self.db.execute(
            "SELECT state FROM sonarr_episode_candidates ORDER BY rank").fetchall(),
            [('ACTIVE',),('READY',)])

    def test_exhausted_rank_schedules_visible_needs_review(self):
        from unittest.mock import patch
        from src.nextgen.storage.release_candidates import schedule_after_cleanup
        self.db.executescript("""
            CREATE TABLE job_history(history_id INTEGER,job_id INTEGER,
                event_code TEXT,reason_code TEXT);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,
                target_job_id INTEGER,job_id INTEGER,created_at_ms INTEGER);
            CREATE TABLE jobs(job_id INTEGER,event_id INTEGER,kind TEXT,
                created_by_job_id INTEGER,payload_json TEXT);
            CREATE TABLE incoming_events(event_id INTEGER,source TEXT,raw_body TEXT);
            CREATE TABLE trial_plans(plan_id INTEGER,anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,
                anidb_episode_number INTEGER,tvdb_episode_id INTEGER,info_hash TEXT);
            CREATE TABLE episode_reservations(reservation_id INTEGER,state TEXT);
            CREATE TABLE acquisition_plans(reservation_id INTEGER,info_hash TEXT);
            INSERT INTO trial_plans VALUES(1,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER);
            INSERT INTO jobs VALUES(10,9,'DISCOVER_SONARR_BACKFILL',
                NULL,'{"plan_id":1}');
            INSERT INTO jobs VALUES(11,25,'VERIFY_RELEASE',10,'{}');
            INSERT INTO jobs VALUES(30,25,'CLEANUP_CANCELED_OPERATION',11,'{}');
            INSERT INTO management_actions VALUES(7,'CLEANUP',11,30,1);
            INSERT INTO job_operation_members VALUES(11,7);
            INSERT INTO job_history VALUES(1,11,'NEEDS_REVIEW',
                'ACQUISITION_METADATA_INVALID');
        """)
        self.db.execute("INSERT INTO incoming_events VALUES(25,'sonarr-discovery',?)",
                        ('{"torrent_infohash":"'+FIRST+'"}',))
        self.db.execute(
            'INSERT INTO sonarr_episode_selections VALUES(1,2,901,?)',
            (FIRST,))
        self.db.execute(
            "UPDATE sonarr_episode_candidates SET state='FAILED' WHERE rank=2")
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
        with patch('src.nextgen.storage.sqlite.enqueue_job_with_receipt',
                   return_value=(99,'receipt')) as enqueue:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.assertEqual(schedule_after_cleanup(self.db,7),('receipt',))
            self.assertEqual(enqueue.call_args.kwargs['payload'],
                             {'plan_id':1,'start':1,'exhausted_episode':2})

    def test_withdrawn_release_skips_b_and_tries_c_without_download(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.nextgen.storage.episode_discovery import (
            BackfillBatch,FallbackTarget,FallbackMissing,
            persist_fallback_missing)
        from src.nextgen.episode_identity import VerifiedEpisode
        third='c'*40
        self.db.executescript("""
            CREATE TABLE trial_plans(plan_id INTEGER,tvdb_series_id INTEGER,
                tvdb_season INTEGER);
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,
                anidb_episode_number INTEGER,tvdb_episode_id INTEGER,info_hash TEXT,
                tvdb_season INTEGER,tvdb_episode INTEGER);
        """)
        self.db.execute("INSERT INTO trial_plans VALUES(1,456,1)")
        self.db.execute(
            "INSERT INTO sonarr_episode_selections VALUES(1,2,901,?,1,2)",(FIRST,))
        self.db.execute(
            "INSERT INTO sonarr_episode_candidates(plan_id,anidb_episode_number,rank,info_hash) "
            "VALUES(1,2,3,?)",(third,))
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
            fail_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST,
                           reason_code='ACQUISITION_METADATA_INVALID')
        plan=BackfillBatch(1,123,456,4,1,(1,2,3,4),(),())
        identity=VerifiedEpisode(456,901,1,2)
        value=FallbackMissing(plan,2,FallbackTarget(identity,FIRST,SECOND))
        claim=SimpleNamespace(kind='DISCOVER_SONARR_BACKFILL',
                              event_id=9,job_id=30)
        with patch('src.nextgen.storage.episode_discovery._plan',
                   return_value=plan), patch(
                   'src.nextgen.storage.episode_discovery.enqueue_job_with_receipt',
                   return_value=(99,'receipt')) as enqueue:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.assertEqual(persist_fallback_missing(
                    self.db,claim,value),('receipt',))
        self.assertEqual(enqueue.call_args.kwargs['payload'],
                         {'plan_id':1,'start':1,'fallback_episode':2})
        self.assertEqual(self.db.execute(
            "SELECT state FROM sonarr_episode_candidates ORDER BY rank").fetchall(),
            [('FAILED',),('FAILED',),('READY',)])

    def test_fallback_reselects_next_rank_and_uses_existing_dispatch(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.nextgen.storage.episode_discovery import (
            BackfillBatch,FallbackTarget,FallbackCompletion,persist_fallback,
            BackfillConflict)
        from src.nextgen.acquisition.prowlarr_search import (
            CandidateSelection,Release,SearchResult,DiscoveryPolicy)
        from src.nextgen.episode_identity import VerifiedEpisode
        self.db.executescript("""
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,
                anidb_episode_number INTEGER,tvdb_episode_id INTEGER,
                info_hash TEXT,selected_at_ms INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER);
            CREATE TABLE trial_plans(plan_id INTEGER,tvdb_series_id INTEGER);
            INSERT INTO trial_plans VALUES(1,456);
            CREATE TABLE episode_reservations(state TEXT,
                tvdb_series_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER);
            CREATE TABLE batch_episode_reservations(state TEXT,
                tvdb_series_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER);
        """)
        self.db.execute(
            "INSERT INTO sonarr_episode_selections VALUES(1,2,901,?,1,1,2)",
            (FIRST,))
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
            fail_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST,
                           reason_code='ACQUISITION_METADATA_INVALID')
        plan=BackfillBatch(1,123,456,4,1,(1,2,3,4),(),())
        identity=VerifiedEpisode(456,901,1,2)
        release=Release('Example S01E02 1080p','Nyaa',1080,9,123456789,901,
                        SECOND,'magnet:?xt=urn:btih:'+SECOND,True)
        results=SearchResult(456,901,(release,))
        choice=CandidateSelection(identity,results,release,DiscoveryPolicy())
        value=FallbackCompletion(plan,2,FallbackTarget(identity,FIRST,SECOND),
                                 choice)
        claim=SimpleNamespace(kind='DISCOVER_SONARR_BACKFILL',event_id=9,
                              job_id=30,attempt=1)
        with patch('src.nextgen.storage.episode_discovery._plan',
                   return_value=plan), patch(
                   'src.nextgen.storage.release_dispatch.dispatch',
                   return_value=()) as dispatch, patch(
                   'src.nextgen.storage.episode_discovery.append_job_history',
                   return_value=None):
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                persist_fallback(self.db,claim,value)
            dispatch.assert_called_once()
            self.assertEqual(self.db.execute(
                "SELECT info_hash FROM sonarr_episode_selections").fetchone(),
                (SECOND,))
            self.assertEqual(self.db.execute(
                "SELECT state FROM sonarr_episode_candidates ORDER BY rank").fetchall(),
                [('FAILED',),('ACTIVE',)])
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                with self.assertRaises(BackfillConflict):
                    persist_fallback(self.db,claim,value)

    def test_metadata_timeout_failure_isolation_and_fallback(self):
        """Proves TORRENT_METADATA_TIMEOUT on Candidate 1 isolates the failure and activates Candidate 2."""
        from unittest.mock import patch
        from src.nextgen.storage.release_candidates import schedule_after_cleanup
        self.db.executescript("""
            CREATE TABLE job_history(history_id INTEGER,job_id INTEGER,
                event_code TEXT,reason_code TEXT);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,
                target_job_id INTEGER,job_id INTEGER,created_at_ms INTEGER);
            CREATE TABLE jobs(job_id INTEGER,event_id INTEGER,kind TEXT,
                created_by_job_id INTEGER,payload_json TEXT);
            CREATE TABLE incoming_events(event_id INTEGER,source TEXT,raw_body TEXT);
            CREATE TABLE trial_plans(plan_id INTEGER,anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,
                anidb_episode_number INTEGER,tvdb_episode_id INTEGER,info_hash TEXT);
            CREATE TABLE episode_reservations(reservation_id INTEGER,state TEXT);
            CREATE TABLE acquisition_plans(reservation_id INTEGER,info_hash TEXT);
            INSERT INTO trial_plans VALUES(1,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER);
            INSERT INTO jobs VALUES(10,9,'DISCOVER_SONARR_BACKFILL',NULL,'{"plan_id":1}');
            INSERT INTO jobs VALUES(11,25,'VERIFY_RELEASE',10,'{}');
            INSERT INTO jobs VALUES(12,25,'PREPARE_ACQUISITION',11,'{}');
            INSERT INTO jobs VALUES(30,25,'CLEANUP_CANCELED_OPERATION',12,'{}');
            INSERT INTO management_actions VALUES(7,'CLEANUP',12,30,1);
            INSERT INTO job_operation_members VALUES(11,7),(12,7);
            INSERT INTO job_history VALUES(1,12,'NEEDS_REVIEW','TORRENT_METADATA_TIMEOUT');
            INSERT INTO incoming_events VALUES(25,'sonarr-discovery','{"torrent_infohash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}');
            INSERT INTO sonarr_episode_selections VALUES(1,2,901,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        """)
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
        self.assertEqual(
            self.db.execute("SELECT rank,state FROM sonarr_episode_candidates ORDER BY rank").fetchall(),
            [(1,'ACTIVE'),(2,'READY')])
        with patch('src.nextgen.storage.sqlite.enqueue_job_with_receipt',
                   return_value=(99,'receipt')) as enqueue:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                receipts = schedule_after_cleanup(self.db,7)
            self.assertEqual(receipts,('receipt',))
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.kwargs['payload'],
                             {'plan_id':1,'start':1,'fallback_episode':2})
        self.assertEqual(
            self.db.execute("SELECT rank,state,reason_code FROM sonarr_episode_candidates ORDER BY rank").fetchall(),
            [(1,'FAILED','TORRENT_METADATA_TIMEOUT'),(2,'READY',None)])

    def test_size_limit_rejection_failure_isolation_and_fallback(self):
        """Proves ACQUISITION_SIZE_LIMIT on Candidate 1 isolates the failure and activates Candidate 2."""
        from unittest.mock import patch
        from src.nextgen.storage.release_candidates import schedule_after_cleanup
        self.db.executescript("""
            CREATE TABLE job_history(history_id INTEGER,job_id INTEGER,
                event_code TEXT,reason_code TEXT);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,
                target_job_id INTEGER,job_id INTEGER,created_at_ms INTEGER);
            CREATE TABLE jobs(job_id INTEGER,event_id INTEGER,kind TEXT,
                created_by_job_id INTEGER,payload_json TEXT);
            CREATE TABLE incoming_events(event_id INTEGER,source TEXT,raw_body TEXT);
            CREATE TABLE trial_plans(plan_id INTEGER,anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,
                anidb_episode_number INTEGER,tvdb_episode_id INTEGER,info_hash TEXT);
            CREATE TABLE episode_reservations(reservation_id INTEGER,state TEXT);
            CREATE TABLE acquisition_plans(reservation_id INTEGER,info_hash TEXT);
            INSERT INTO trial_plans VALUES(1,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER);
            INSERT INTO jobs VALUES(10,9,'DISCOVER_SONARR_BACKFILL',NULL,'{"plan_id":1}');
            INSERT INTO jobs VALUES(11,25,'VERIFY_RELEASE',10,'{}');
            INSERT INTO jobs VALUES(12,25,'PREPARE_ACQUISITION',11,'{}');
            INSERT INTO jobs VALUES(30,25,'CLEANUP_CANCELED_OPERATION',12,'{}');
            INSERT INTO management_actions VALUES(7,'CLEANUP',12,30,1);
            INSERT INTO job_operation_members VALUES(11,7),(12,7);
            INSERT INTO job_history VALUES(1,12,'NEEDS_REVIEW','ACQUISITION_SIZE_LIMIT');
            INSERT INTO incoming_events VALUES(25,'sonarr-discovery','{"torrent_infohash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}');
            INSERT INTO sonarr_episode_selections VALUES(1,2,901,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        """)
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            activate_candidate(self.db,plan_id=1,episode=2,info_hash=FIRST)
        with patch('src.nextgen.storage.sqlite.enqueue_job_with_receipt',
                   return_value=(99,'receipt')) as enqueue:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                receipts = schedule_after_cleanup(self.db,7)
            self.assertEqual(receipts,('receipt',))
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.kwargs['payload'],
                             {'plan_id':1,'start':1,'fallback_episode':2})
        self.assertEqual(
            self.db.execute("SELECT rank,state,reason_code FROM sonarr_episode_candidates ORDER BY rank").fetchall(),
            [(1,'FAILED','ACQUISITION_SIZE_LIMIT'),(2,'READY',None)])
