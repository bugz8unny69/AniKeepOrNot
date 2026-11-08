"""Offline lease-fenced backfill/continue scheduling and observation tests."""
import sqlite3
import json
import unittest

from src.anime.configuration import ConfigurationStore
from src.anime.job_contracts import Registry, Policy
from src.anime.jobs.select_user_evaluation_job import registration as user_evaluation_registration
from src.anime.jobs.discover_episode_releases_job import (
    registration as discovery_registration, KIND as DISCOVER_KIND,
)
from src.anime.acquisition.prowlarr_search import BackfillObservation, SearchUnavailable
from src.anime.storage.sqlite import transaction
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, LATEST_VERSION, _structure
from src.anime.storage.sqlite import connect, initialize
from src.anime.tests.test_user_evaluation_policy import UserEvaluationCheckTests
from src.anime.worker import Worker


class FakeDiscovery:
    def __init__(self):
        self.calls = []
        self.error = False

    def backfill(self, anidb_id, tvdb_series_id, numbers, *, reserved=(), verified_ids=None, **kwargs):
        self.calls.append((tvdb_series_id, numbers, tuple(reserved), dict(verified_ids or {})))
        if self.error: raise SearchUnavailable()
        return tuple(
            BackfillObservation(n, 2000+n, 'IN_PROGRESS')
            if n in reserved else
            BackfillObservation(n, 2000+n, 'CANDIDATES', 3, 1)
            for n in numbers
        )


class BackfillWorkerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = UserEvaluationCheckTests.asyncSetUp
    rows = UserEvaluationCheckTests.rows
    source = UserEvaluationCheckTests.source

    def worker(self, fake, *, enabled=True):
        user_evaluation = user_evaluation_registration(discovery_kind=DISCOVER_KIND if enabled else None)
        registrations = [user_evaluation]
        if enabled:
            registrations.append(discovery_registration(None,
                client_factory=lambda config, secrets: fake))
        worker = Worker(self.store, self.startup, Registry(registrations),
                        policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        return worker

    async def test_first_plan_creates_one_paged_discovery_chain_with_durable_results(self):
        self.source(7)
        fake = FakeDiscovery()
        worker = self.worker(fake)
        await worker.attempt()  # SELECT_USER_EVALUATION -> first DISCOVER job in same tx.
        self.assertEqual(self.rows(
            "SELECT kind,payload_json FROM jobs WHERE kind=?",(DISCOVER_KIND,)),
            [(DISCOVER_KIND,'{"plan_id": 1, "start": 1}')])
        await worker.attempt()  # E01–E04 -> E05–E06; the delivered E07 is not searched.
        await worker.attempt()
        self.assertEqual([row[0] for row in self.rows(
            "SELECT anidb_episode_number FROM episode_discovery_results ORDER BY anidb_episode_number")],
            [1,2,3,4,5,6])
        self.assertEqual(self.rows(
            "SELECT status,candidates,approved,exact FROM episode_discovery_results "
            "WHERE anidb_episode_number=6"), [('CANDIDATES',3,1,1)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind=?",(DISCOVER_KIND,)),[(2,)])
        self.assertEqual([v[1] for v in fake.calls],[(1,2,3,4),(5,6)])
        self.source(8)
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind=?",(DISCOVER_KIND,)),[(2,)])
        self.assertEqual(self.rows("SELECT count(*) FROM episode_discovery_results"),[(6,)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM jobs WHERE kind IN "
            "('SUBMIT_TORRENT','RESERVE_EPISODE','DOWNLOAD_TORRENT')"),[(0,)])

    async def test_optional_selection_does_not_invent_identity(self):
        """Unverified catalog episodes remain observations and never gain a choice."""
        self.source(7)
        fake = FakeDiscovery()
        worker = self.worker(fake)
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM episode_release_selections"),
                         [(0,)])

    async def _run_recognized_candidate(self, acquire=False, recognized=True):
        from src.anime.acquisition.prowlarr_search import CandidateSelection, Release, SearchResult, DiscoveryPolicy
        from src.anime.episode_identity import VerifiedEpisode

        self.source(7)
        _, release_job, _ = self.source(1)
        db = connect(self.database)
        try:
            with db:
                if recognized:
                    db.execute(
                    "INSERT INTO recognized_seasons VALUES(456,1,123,'Test',?,1)",
                    (db.execute("SELECT verification_id FROM release_verifications "
                                "WHERE job_id=?", (release_job,)).fetchone()[0],))
                    db.execute(
                    "INSERT INTO recognized_episodes VALUES(456,1,123,1,11001,1,111001,?,1)",
                    (db.execute("SELECT verification_id FROM release_verifications "
                                "WHERE job_id=?", (release_job,)).fetchone()[0],))
        finally:
            db.close()

        class VerifiedFake(FakeDiscovery):
            def backfill(self, anidb_id, tvdb_series_id, numbers, *, reserved=(), verified_ids=None,
                         select_candidates=False, **kwargs):
                assert select_candidates is True
                result = list(super().backfill(anidb_id,tvdb_series_id,numbers,
                                              reserved=reserved,verified_ids=verified_ids))
                assert verified_ids.get(1) == (111001 if recognized else None)
                release = Release('[Group] Test - 01 [1080p]', 'Nyaa', 1080, 9, 123456789,
                                  111001, 'a'*40,
                                  'magnet:?xt=urn:btih:'+'a'*40+'&tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce',
                                  True,'Test - 01')
                result[0] = BackfillObservation(
                    1,111001,'CANDIDATES',1,1,
                    CandidateSelection(VerifiedEpisode(456,111001,1,1),
                                       SearchResult(456,111001,(release,)),release,DiscoveryPolicy()))
                return tuple(result)

        user_evaluation = user_evaluation_registration(discovery_kind=DISCOVER_KIND)
        registry = Registry((user_evaluation, discovery_registration(None,
            client_factory=lambda config,secrets:VerifiedFake(),select_candidates=True,
            acquire_candidates=acquire)))
        worker = Worker(self.store,self.startup,registry,policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        await worker.attempt()  # First evaluation.
        await worker.attempt()  # Additional episode evaluation.
        await worker.attempt()  # Search observation and selection.
        self.assertEqual(self.rows(
            "SELECT anidb_episode_number,tvdb_episode_id,info_hash FROM episode_release_selections"),
            [(1,111001,'a'*40)])
        self.assertEqual(self.rows(
            "SELECT candidates,approved,exact FROM episode_discovery_results WHERE anidb_episode_number=1"),
            [(1,1,1)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM torrent_submissions"),[(0,)])
        self.assertEqual(self.rows(
            "SELECT state FROM episode_release_candidates WHERE plan_id=1 AND anidb_episode_number=1"),
            [('ACTIVE' if acquire else 'READY',)])

        events=self.rows("SELECT raw_body,state FROM incoming_events WHERE source='release-discovery'")
        if acquire:
            self.assertEqual(len(events),1)
            self.assertEqual(events[0][1],'IDENTIFIED')
            body=json.loads(events[0][0])
            self.assertEqual(body['torrent_infohash'],'a'*40)
            # Verification reads the plain title; the group's title is kept.
            self.assertEqual((body['release_name'],body['source_release_name']),
                             ('Test - 01','[Group] Test - 01 [1080p]'))
            # The sanitized magnet travels with the delivery; no Sonarr re-search.
            self.assertEqual(body['magnet_uri'],'magnet:?xt=urn:btih:'+'a'*40
                             +'&tr=udp://tracker.example:1337/announce')
            self.assertEqual(self.rows(
                "SELECT kind FROM jobs WHERE event_id IN "
                "(SELECT event_id FROM incoming_events WHERE source='release-discovery')"),
                [('VERIFY_RELEASE',)])
            # The discovery page may spawn several episodes; cancellation of
            # one acquisition must not cancel its sibling or the backfill job.
            self.assertEqual(self.rows(
                "SELECT count(DISTINCT m.operation_id) FROM job_operation_members m "
                "JOIN jobs j USING(job_id) WHERE j.kind IN "
                "('DISCOVER_EPISODE_RELEASES','VERIFY_RELEASE')"),
                [(4,)])
        else:
            self.assertEqual(events,[])

    async def test_history_lists_each_release_tried_and_why_it_stopped(self):
        from src.anime.storage.backfill_attempts import backfill_attempts
        await self._run_recognized_candidate(True)
        verify_job, event_id = self.rows(
            "SELECT job_id,event_id FROM jobs WHERE kind='VERIFY_RELEASE' AND event_id IN "
            "(SELECT event_id FROM incoming_events WHERE source='release-discovery')")[0]
        db = connect(self.database)
        try:
            found = backfill_attempts(db, verify_job)
            self.assertEqual((found['episode'], [
                (a['rank'], a['state'], a['release_name'], a['trace_id']) for a in found['attempts']]),
                (1, [(1, 'IN_PROGRESS', '[Group] Test - 01 [1080p]', f'AD-{event_id}')]))
            # A size-limit stop is shown with its numbers.
            with transaction(db):
                db.execute("UPDATE episode_release_candidates SET state='FAILED',"
                           "reason_code='ACQUISITION_SIZE_LIMIT'")
                db.execute("INSERT INTO job_history(job_id,event_code,reason_code,component,"
                           "occurred_at_ms,details_json) VALUES(?,'NEEDS_REVIEW',"
                           "'ACQUISITION_SIZE_LIMIT','worker',1,?)",
                           (verify_job, json.dumps({'attempt':1,'video_mb':1420,'limit_mb':600})))
            item = backfill_attempts(db, verify_job)['attempts'][0]
            self.assertEqual((item['state'], item['reason_code'], item['details']),
                             ('FAILED', 'ACQUISITION_SIZE_LIMIT', {'video_mb':1420,'limit_mb':600}))
            # Jobs outside a backfill episode have nothing to show.
            first = self.rows("SELECT min(job_id) FROM jobs")[0][0]
            self.assertIsNone(backfill_attempts(db, first))
        finally:
            db.close()

    async def test_recognized_candidate_is_selected_without_download(self):
        await self._run_recognized_candidate(False)

    async def test_opted_in_candidate_routes_to_existing_verification(self):
        await self._run_recognized_candidate(True)

    async def test_unrecognized_backfill_candidate_still_routes_to_provider_verification(self):
        await self._run_recognized_candidate(True, recognized=False)

    async def test_opt_in_off_means_no_new_discovery_jobs(self):
        self.source(7)
        worker = self.worker(FakeDiscovery(),enabled=False)
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind=?",(DISCOVER_KIND,)),[(0,)])

    async def test_only_the_delivered_episode_creates_no_discovery_work(self):
        self.store.update({'user_evaluation':{'backfill_count':0,'following_count':1}},
                          expected_revision=1)
        self.source(7)
        worker = self.worker(FakeDiscovery())
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind=?",(DISCOVER_KIND,)),[(0,)])

    async def test_continue_without_backfill_searches_following_episodes(self):
        self.store.update({'user_evaluation':{'backfill_count':0,'following_count':3}},
                          expected_revision=1)
        self.source(7)
        fake = FakeDiscovery()
        worker = self.worker(fake)
        await worker.attempt()
        await worker.attempt()
        self.assertEqual([v[1] for v in fake.calls],[(8,9)])

    async def test_backfill_and_continue_overlap_counts_once(self):
        # backfill=6, incoming=5, following=3 selects episodes 1-7 once;
        # the incoming episode 5 is acquired by its own delivery.
        self.store.update({'user_evaluation':{'backfill_count':6,'following_count':3}},
                          expected_revision=1)
        self.source(5)
        fake = FakeDiscovery()
        worker = self.worker(fake)
        await worker.attempt()
        await worker.attempt()
        await worker.attempt()
        self.assertEqual([v[1] for v in fake.calls],[(1,2,3,4),(6,7)])
        self.assertEqual(self.rows("SELECT count(*) FROM episode_discovery_results"),[(6,)])

    async def test_missing_episode_can_be_searched_again_without_resending_others(self):
        from src.anime.storage.series_intake import retry_episode
        self.source(7)
        fake = FakeDiscovery()
        original = fake.backfill

        def first_pass(*args, **kwargs):
            result = list(original(*args, **kwargs))
            return tuple(BackfillObservation(2, 2002, 'NO_RESULTS')
                         if item.number == 2 else item for item in result)

        fake.backfill = first_pass
        worker = self.worker(fake)
        await worker.attempt()
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows(
            "SELECT status FROM episode_discovery_results WHERE anidb_episode_number=2"),
            [('NO_RESULTS',)])
        fake.backfill = original
        receipt = retry_episode(self.database, 123, 2)
        self.assertFalse(receipt['reused'])
        repeated = retry_episode(self.database, 123, 2)
        self.assertEqual(repeated, {'job_id': receipt['job_id'], 'reused': True})
        await worker.attempt()
        self.assertEqual(fake.calls[-1][1], (2,))
        self.assertEqual(self.rows(
            "SELECT status FROM episode_discovery_results WHERE anidb_episode_number=2"),
            [('CANDIDATES',)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM episode_discovery_results"), [(6,)])

    async def test_unavailable_episodes_do_not_block_episodes_with_releases(self):
        self.source(7)
        fake = FakeDiscovery()
        statuses = {1:'NOT_AIRED',2:'NO_RESULTS',3:'MAPPING_MISSING'}
        def mixed(anidb_id, tid, numbers, **kw):
            return tuple(BackfillObservation(n, None if n in (1,3) else 2000+n, statuses[n])
                         if n in statuses else
                         BackfillObservation(n, 2000+n, 'CANDIDATES', 2, 1) for n in numbers)
        fake.backfill = mixed
        worker = self.worker(fake)
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind=? ORDER BY job_id",(DISCOVER_KIND,)),
                         [('DONE',),('PENDING',)])
        self.assertEqual(self.rows("SELECT anidb_episode_number,status FROM episode_discovery_results ORDER BY anidb_episode_number"),
                         [(1,'NOT_AIRED'),(2,'NO_RESULTS'),(3,'MAPPING_MISSING'),(4,'CANDIDATES')])

    async def fail_twice(self, fake):
        """First failure waits 30 minutes; the second one stops for a manual Retry."""
        worker = self.worker(fake)
        await worker.attempt()   # SELECT_USER_EVALUATION
        await worker.attempt()   # search fails -> one delayed automatic retry
        state, available = self.rows(
            "SELECT state,available_at_ms-updated_at_ms FROM jobs WHERE kind=?",(DISCOVER_KIND,))[0]
        self.assertEqual((state, available), ('RETRY_WAIT', 30*60*1000))
        self.assertFalse(await worker.attempt())   # not due yet
        self.make_due()
        await worker.attempt()   # fails again -> BLOCKED
        return worker

    async def test_unavailable_prowlarr_is_a_service_outage_and_persists_no_partial_page(self):
        # An outage is SERVICE_UNAVAILABLE naming Prowlarr. With service health (as in
        # production) the worker makes it a wait; here, without it, an ordinary retry.
        # It is never BLOCKED for a manual Retry, unlike a timeout.
        self.source(7)
        fake = FakeDiscovery()
        fake.error = True
        worker = self.worker(fake)
        await worker.attempt()   # SELECT_USER_EVALUATION
        await worker.attempt()   # search fails
        self.assertEqual(self.rows("SELECT count(*) FROM episode_discovery_results"),[(0,)])
        self.assertEqual(self.rows(
            "SELECT state,last_reason_code FROM jobs WHERE kind=?",(DISCOVER_KIND,)),
            [('RETRY_WAIT','SERVICE_UNAVAILABLE')])
        event, details = self.rows(
            "SELECT event_code,json_extract(details_json,'$.service') FROM job_history "
            "WHERE reason_code='SERVICE_UNAVAILABLE' ORDER BY history_id DESC LIMIT 1")[0]
        self.assertEqual((event, details), ('RETRY_SCHEDULED', 'prowlarr'))
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind=?",(DISCOVER_KIND,)),[(1,)])
        self.assertEqual(len(fake.calls),1)

    async def test_a_prowlarr_outage_recovers_on_the_next_attempt(self):
        self.source(7)
        fake = FakeDiscovery()
        fake.error = True
        worker = self.worker(fake)
        await worker.attempt()
        await worker.attempt()
        fake.error = False
        self.make_due()
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind=? ORDER BY job_id",(DISCOVER_KIND,))[0],('DONE',))
        self.assertEqual(self.rows("SELECT count(*) FROM episode_discovery_results"),[(4,)])


    def make_due(self):
        db = connect(self.database)
        try:
            with transaction(db): db.execute('UPDATE jobs SET available_at_ms=0 WHERE kind=?',(DISCOVER_KIND,))
        finally: db.close()

    async def test_timeout_waits_once_then_stops_without_successor(self):
        from src.anime.acquisition.prowlarr_search import SearchTimeout
        self.source(7)
        fake = FakeDiscovery()
        def timeout(*args, **kwargs): raise SearchTimeout()
        fake.backfill = timeout
        worker = await self.fail_twice(fake)
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind=?",(DISCOVER_KIND,)),
                         [('BLOCKED','RELEASE_SEARCH_TIMEOUT')])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind=?",(DISCOVER_KIND,)),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='DEFERRED'"),[(1,)])
        self.make_due()
        self.assertFalse(await worker.attempt())

    async def test_blocked_backfill_can_retry_the_same_job_manually(self):
        from src.anime.acquisition.prowlarr_search import SearchTimeout
        from src.anime.storage.review_retry import capability, request_review_retry
        self.source(7)
        fake = FakeDiscovery()
        def timeout(*args, **kwargs): raise SearchTimeout()
        fake.backfill = timeout
        worker = await self.fail_twice(fake)
        job, attempt = self.rows("SELECT job_id,attempt_count FROM jobs WHERE kind=?",(DISCOVER_KIND,))[0]
        db = connect(self.database)
        try: self.assertTrue(capability(db, job)['enabled'])
        finally: db.close()
        request_review_retry(self.database, job, expected_attempt=attempt,
                             expected_reason='RELEASE_SEARCH_TIMEOUT')
        del fake.backfill  # Prowlarr answers again
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state,attempt_count FROM jobs WHERE job_id=?",(job,)),[('DONE',3)])
        self.assertEqual(self.rows("SELECT count(*) FROM episode_discovery_results"),[(4,)])

    async def test_old_missing_series_hold_is_not_automatically_restarted(self):
        self.source(7)
        worker = self.worker(FakeDiscovery())
        await worker.attempt()
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='ON_HOLD',attempt_count=1,"
                    "last_reason_code='EPISODE_DISCOVERY_OUTDATED' WHERE kind=?",(DISCOVER_KIND,))
        finally: db.close()
        await worker._recover()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind=?",(DISCOVER_KIND,)),[('ON_HOLD',)])

    async def test_configuration_review_is_labeled_and_retryable_from_dashboard(self):
        from src.anime.acquisition.prowlarr_search import SearchConfiguration
        from src.anime.dashboard.presentation import HANDLERS
        from src.anime.storage.review_retry import capability, request_review_retry
        self.assertEqual(HANDLERS.require(DISCOVER_KIND).code, DISCOVER_KIND)
        self.source(7)
        fake = FakeDiscovery()
        worker = self.worker(fake)
        await worker.attempt()  # SELECT_USER_EVALUATION
        def misconfigured(*args, **kwargs): raise SearchConfiguration()
        fake.backfill = misconfigured
        await worker.attempt()
        job, attempt = self.rows("SELECT job_id,attempt_count FROM jobs WHERE kind=?",(DISCOVER_KIND,))[0]
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),
                         [('NEEDS_REVIEW','SERVICE_SETUP_INVALID')])
        db = connect(self.database)
        try: self.assertTrue(capability(db, job)['enabled'])
        finally: db.close()
        request_review_retry(self.database, job, expected_attempt=attempt,
                             expected_reason='SERVICE_SETUP_INVALID')
        del fake.backfill  # settings fixed: the real fake search answers again
        await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM episode_discovery_results"),[(4,)])

    async def test_other_holds_are_not_resumed(self):
        self.source(7)
        worker = self.worker(FakeDiscovery())
        await worker.attempt()
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='ON_HOLD',attempt_count=1,"
                    "last_reason_code='EPISODE_DISCOVERY_PLAN_CONFLICT' WHERE kind=?",(DISCOVER_KIND,))
        finally: db.close()
        await worker._recover()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind=?",(DISCOVER_KIND,)),[('ON_HOLD',)])

    async def test_plan_window_preserves_immutable_stored_results_across_anchor_shift(self):
        from src.anime.storage.episode_discovery import plan_window, _plan
        self.source(1157)
        worker = self.worker(FakeDiscovery())
        await worker.attempt()  # SELECT_USER_EVALUATION -> creates plan 1
        db = connect(self.database)
        try:
            # Without stored results, window is computed from anchor 1157
            self.assertEqual(plan_window(db, 1, 1157, 6, 0), (1151, 1152, 1153, 1154, 1155, 1156))

            # Simulate legacy plan results stored prior to migration anchor shift
            with transaction(db):
                for ep in (1, 2, 3, 4, 5, 6):
                    db.execute(
                        "INSERT INTO episode_discovery_results(plan_id, anidb_episode_number, job_id, tvdb_episode_id, status, candidates, approved, exact, observed_at_ms) "
                        "VALUES(1, ?, 1, 2000+?, 'CANDIDATES', 3, 1, 1, 0)", (ep, ep))

            # With stored results that are not a subset of computed, stored results are returned
            self.assertEqual(plan_window(db, 1, 1157, 6, 0), (1, 2, 3, 4, 5, 6))

            # _plan respects the restored immutable window
            plan_batch_1 = _plan(db, 1, 1)
            self.assertEqual(plan_batch_1.numbers, (1, 2, 3, 4))
            self.assertEqual(plan_batch_1.window_size, 6)
            self.assertIn(2, plan_batch_1.numbers)

            plan_batch_2 = _plan(db, 1, 5)
            self.assertEqual(plan_batch_2.numbers, (5, 6))

            # When stored results ARE a subset of computed (e.g. current in-progress discovery)
            with transaction(db):
                db.execute("DELETE FROM episode_discovery_results WHERE plan_id=1")
                for ep in (1151, 1152):
                    db.execute(
                        "INSERT INTO episode_discovery_results(plan_id, anidb_episode_number, job_id, tvdb_episode_id, status, candidates, approved, exact, observed_at_ms) "
                        "VALUES(1, ?, 1, 2000+?, 'CANDIDATES', 3, 1, 1, 0)", (ep, ep))
            self.assertEqual(plan_window(db, 1, 1157, 6, 0), (1151, 1152, 1153, 1154, 1155, 1156))
        finally:
            db.close()



class BackfillSchemaTests(unittest.TestCase):
    def test_explicit_v41_to_latest_and_fresh_schema_match(self):
        old = sqlite3.connect(':memory:')
        fresh = sqlite3.connect(':memory:')
        try:
            for script in SCRIPTS[:41]:
                old.executescript((SQL_DIR/script).read_text())
            old.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) "
                        "VALUES('test',X'61',1)")
            old.commit()
            before = old.execute('SELECT * FROM incoming_events').fetchall()
            for name in SCRIPTS[41:]:
                old.executescript((SQL_DIR/name).read_text())
            initialize(fresh)
            self.assertEqual(_structure(old),_structure(fresh))
            self.assertEqual(old.execute('SELECT * FROM incoming_events').fetchall(),before)
            self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0],LATEST_VERSION)
        finally:
            old.close()
            fresh.close()

    def test_v43_allows_local_candidates_even_when_sonarr_approved_zero(self):
        db = sqlite3.connect(':memory:')
        try:
            initialize(db)
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='episode_discovery_results'").fetchone()[0]
            self.assertIn('exact BETWEEN 0 AND candidates', sql)
            self.assertNotIn('exact BETWEEN 0 AND approved', sql)
        finally:
            db.close()

    def test_wrong_starting_version_rolls_back(self):
        db = sqlite3.connect(':memory:')
        try:
            db.execute('PRAGMA user_version=40')
            with self.assertRaises(sqlite3.Error):
                db.executescript((SQL_DIR/'042_sonarr_backfill.sql').read_text())
            db.rollback()
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],40)
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='episode_discovery_results'").fetchone())
        finally:
            db.close()
