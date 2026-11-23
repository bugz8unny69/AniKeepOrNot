"""The API identifies a release (identity process), then gives the worker a job.

Anything the worker could not complete is refused with API Reject: a
notification that says why and keeps the release. The worker never sees it.
"""
import asyncio
import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.anime import identity_bridge, identity_process
from src.anime.providers.errors import ProviderDeferred, ProviderUnavailable
from src.anime.storage.sqlite import IdentityNotReady, connect, initialize

RAW = json.dumps({'release_name': '[Group] Cyberpunk Edgerunners - 05 (1080p)',
                  'torrent_infohash': 'a' * 40}).encode()
SERIES = {'schema_version': 2, 'anime': {'anidb_id': 16999, 'tvdb_series_id': 393400,
                                         'title': 'Cyberpunk Edgerunners'}, 'episodes': []}
LOG = logging.getLogger('identity-bridge-test')


class BridgeStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = str(Path(temp.name) / 'anime.sqlite')
        db = sqlite3.connect(self.database); initialize(db); db.close()

    def rows(self, sql, args=()):
        db = sqlite3.connect(self.database)
        try: return db.execute(sql, args).fetchall()
        finally: db.close()

    def queue(self, **kwargs):
        db = connect(self.database)
        try: return identity_bridge.queue_release(db, RAW, **kwargs)
        finally: db.close()

    def test_identified_release_gets_one_worker_job_and_leaves_the_queue(self):
        request = self.queue()
        done = identity_bridge.apply_result(self.database, {'request_id': request, 'result': 'identified',
                                                            'series': SERIES}, LOG)
        self.assertEqual(done, 'identified')
        self.assertEqual(self.rows('SELECT outcome,last_reason_code FROM identity_requests'), [('accepted', None)])
        self.assertEqual(self.rows('SELECT state FROM incoming_events'), [('IDENTIFIED',)])
        self.assertEqual(self.rows('SELECT anidb_id FROM identity_results'), [(16999,)])
        self.assertEqual(self.rows('SELECT kind,payload_json,state FROM jobs'),
                         [('VERIFY_RELEASE', '{"anidb_id": 16999}', 'PENDING')])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='IDENTIFY_ANIME'"), [(0,)])

    def test_unidentified_release_is_refused_with_why_and_kept_in_the_notification(self):
        request = self.queue()
        done = identity_bridge.apply_result(self.database, {'request_id': request, 'result': 'rejected',
                                                            'reason_code': 'IDENTITY_INCOMPLETE'}, LOG)
        self.assertEqual(done, 'rejected')
        self.assertEqual(self.rows('SELECT count(*) FROM jobs'), [(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM incoming_events'), [(0,)])
        self.assertEqual(self.rows('SELECT outcome,last_reason_code FROM identity_requests'),
                         [('rejected', 'IDENTITY_INCOMPLETE')])
        kind, title, raw = self.rows('SELECT type,title,raw_body FROM notifications')[0]
        self.assertEqual((kind, bytes(raw)), ('API_REJECTION', RAW))
        self.assertTrue(title.startswith('Not identified: '), title)

    def test_a_movie_says_not_processed(self):
        request = self.queue()
        identity_bridge.apply_result(self.database, {'request_id': request, 'result': 'rejected',
                                                     'reason_code': 'RELEASE_MOVIE_SKIPPED'}, LOG)
        self.assertTrue(self.rows('SELECT title FROM notifications')[0][0].startswith('Not processed: '))

    def test_decided_series_is_refused_and_process_anyway_is_not(self):
        with mock.patch('src.anime.storage.review_decisions.decision', return_value='REJECT'):
            request = self.queue()
            self.assertEqual(identity_bridge.apply_result(
                self.database, {'request_id': request, 'result': 'identified', 'series': SERIES}, LOG), 'decided')
            self.assertEqual(self.rows('SELECT count(*) FROM jobs'), [(0,)])
            request = self.queue(process_anyway=True)
            self.assertEqual(identity_bridge.apply_result(
                self.database, {'request_id': request, 'result': 'identified', 'series': SERIES}, LOG), 'identified')
        self.assertEqual(self.rows('SELECT kind FROM jobs'), [('VERIFY_RELEASE',)])

    def test_a_repeated_report_does_nothing(self):
        request = self.queue()
        result = {'request_id': request, 'result': 'identified', 'series': SERIES}
        identity_bridge.apply_result(self.database, result, LOG)
        self.assertEqual(identity_bridge.apply_result(self.database, result, LOG), 'gone')
        self.assertEqual(self.rows('SELECT count(*) FROM jobs'), [(1,)])

    def test_one_row_per_torrent_and_a_decided_answer_is_kept_for_a_day(self):
        first = self.queue()
        self.assertEqual(self.queue(), first)            # Autobrr's retry or its later action.
        identity_bridge.apply_result(self.database, {'request_id': first, 'result': 'rejected',
                                                     'reason_code': 'IDENTITY_INCOMPLETE'}, LOG)
        db = connect(self.database)
        try:
            self.assertEqual(identity_bridge.status(db, first), ('rejected', 'IDENTITY_INCOMPLETE'))
            self.assertFalse(identity_bridge.due(db))
            self.assertEqual(identity_bridge.prune(db, identity_bridge.now_ms()), 0)
            self.assertEqual(identity_bridge.prune(db, identity_bridge.now_ms() + identity_bridge.KEEP_DONE_MS + 1), 1)
        finally:
            db.close()

    def test_process_anyway_queues_a_rejected_release_again(self):
        first = self.queue()
        identity_bridge.apply_result(self.database, {'request_id': first, 'result': 'rejected',
                                                     'reason_code': 'IDENTITY_INCOMPLETE'}, LOG)
        self.assertEqual(self.queue(process_anyway=True), first)
        self.assertEqual(self.rows('SELECT outcome,process_anyway,tries FROM identity_requests'), [(None, 1, 0)])


class IdentifyTests(unittest.IsolatedAsyncioTestCase):
    async def outcome(self, effect, tries=0):
        async def resolve(request, configuration, cancelled, *, matching=None):
            if isinstance(effect, BaseException): raise effect
            return effect
        return await identity_process.identify(resolve, None, (7, RAW, 0, 0, tries), LOG)

    async def test_identified(self):
        kind, line = await self.outcome(SERIES)
        self.assertEqual((kind, line['result'], line['series']['anime']['anidb_id']), ('report', 'identified', 16999))

    async def test_anidb_pacing_waits_without_spending_a_try(self):
        kind, _, reason, spend = await self.outcome(ProviderDeferred(5000))
        self.assertEqual((kind, reason, spend), ('wait', 'JOB_WAITING', False))

    async def test_provider_down_waits_then_is_refused_after_its_tries(self):
        kind, _, _, spend = await self.outcome(ProviderUnavailable())
        self.assertEqual((kind, spend), ('wait', True))
        kind, line = await self.outcome(ProviderUnavailable(), tries=identity_process.MAX_SERVICE_TRIES - 1)
        self.assertEqual((kind, line['result']), ('report', 'rejected'))

    async def test_missing_credentials_wait_and_never_refuse_the_release(self):
        from src.anime.providers.errors import ProviderConfigurationError
        kind, _, reason, spend = await self.outcome(ProviderConfigurationError())
        self.assertEqual((kind, reason, spend), ('wait', 'SERVICE_SETUP_INVALID', False))

    async def test_not_identified_is_refused_with_its_reason(self):
        kind, line = await self.outcome(IdentityNotReady())
        self.assertEqual((kind, line['reason_code']), ('report', IdentityNotReady.reason_code))


class BridgeProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_api_applies_what_the_identity_process_reports_and_it_stops(self):
        applied = []
        script = 'import json; print(json.dumps({"request_id": 3, "result": "rejected", "reason_code": "X"}))'
        bridge = identity_bridge.IdentityBridge('/nowhere', 'db', LOG, command=(sys.executable, '-c', script),
                                                apply=lambda database, result, logger: applied.append(result) or 'ok')
        calls = iter((True, False))
        bridge._due = lambda: next(calls, False)
        task = asyncio.create_task(bridge.run())
        for _ in range(100):
            if applied: break
            await asyncio.sleep(0.05)
        await bridge.close(); await task
        self.assertEqual(applied, [{'request_id': 3, 'result': 'rejected', 'reason_code': 'X'}])
        self.assertIsNone(bridge.process)

    async def test_close_stops_a_running_identity_process(self):
        bridge = identity_bridge.IdentityBridge('/nowhere', 'db', LOG,
                                                command=(sys.executable, '-c', 'import time; time.sleep(60)'))
        bridge._due = lambda: True
        task = asyncio.create_task(bridge.run())
        for _ in range(100):
            if bridge.process is not None: break
            await asyncio.sleep(0.05)
        process = bridge.process
        await bridge.close(); await task
        self.assertIsNotNone(process.returncode)


if __name__ == '__main__':
    unittest.main()


class OldIdentificationMigrationTests(unittest.TestCase):
    """v77: the old identification job leaves no stale rows; waiting releases move to the bridge."""
    def test_unfinished_moves_dead_is_removed_finished_stays(self):
        from src.anime.storage import migrations
        scripts = migrations._scripts()
        db = sqlite3.connect(':memory:')
        db.execute('PRAGMA foreign_keys=ON')
        for _, sql in scripts[:76]:
            db.executescript(sql)
        raw = lambda name: json.dumps({'release_name': name}).encode()
        db.executescript('BEGIN;')
        for event, state in ((1, 'PENDING'), (2, 'PENDING'), (3, 'IDENTIFIED')):
            db.execute('INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,state) VALUES(?,?,?,?,?)',
                       (event, 'autobrr', raw(f'Show {event} - 01'), 10 + event, state))
        for job, event, state, payload in ((11, 1, 'NEEDS_REVIEW', '{"process_anyway": true}'),
                                           (12, 2, 'CANCELED', '{}'), (13, 3, 'DONE', '{}')):
            db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,"
                       "updated_at_ms,last_reason_code) VALUES(?,?,'IDENTIFY_ANIME',?,?,0,1,1,'X')",
                       (job, event, payload, state))
            db.execute('INSERT INTO job_operations(operation_id,created_at_ms,updated_at_ms) VALUES(?,1,1)', (job,))
            db.execute('INSERT INTO job_operation_members VALUES(?,?)', (job, job))
            db.execute("INSERT INTO job_history(job_id,occurred_at_ms,event_code,details_json) VALUES(?,1,'CREATED','{}')",
                       (job,))
        db.execute("INSERT INTO jobs(job_id,event_id,created_by_job_id,kind,payload_json,available_at_ms,created_at_ms,"
                   "updated_at_ms) VALUES(14,3,13,'VERIFY_RELEASE','{}',0,1,1)")
        db.execute("INSERT INTO notifications(type,severity,title,message,job_id,count,created_at_ms,last_at_ms) "
                   "VALUES('MOVIE_SKIPPED','info','t','m',12,1,1,1)")
        db.commit()
        db.executescript(scripts[76][1])
        rows = lambda sql: db.execute(sql).fetchall()
        self.assertEqual(rows('PRAGMA user_version'), [(77,)])
        self.assertEqual([(bytes(r[0]), r[1], r[2], r[3]) for r in rows(
            'SELECT raw_body,process_anyway,received_at_ms,infohash FROM identity_requests')],
            [(raw('Show 1 - 01'), 1, 11, None)])
        self.assertEqual(rows('SELECT job_id FROM jobs ORDER BY job_id'), [(13,), (14,)])
        self.assertEqual(rows('SELECT event_id FROM incoming_events'), [(3,)])
        self.assertEqual(rows('SELECT operation_id FROM job_operations'), [(13,)])
        self.assertEqual(rows('SELECT DISTINCT job_id FROM job_history'), [(13,)])
        self.assertEqual(rows('SELECT job_id FROM notifications'), [(None,)])
        self.assertEqual(rows('PRAGMA foreign_key_check'), [])
