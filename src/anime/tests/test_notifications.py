"""Work item P3: notify(), the Generic notifier, the inbox and the bell API."""
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

from src.anime import notifications
from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import Outcome, Policy, Registration, Registry
from src.anime.notifications import EVENT_TYPES, notify, series_trouble, keep_search_failed
from src.anime.storage import notifications as inbox
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import initialize, transaction
from src.anime.tests.test_api_v1 import ApiV1Tests, ADMIN

REQUIRED = {'type', 'title', 'message'}
LIMITS = {'title': 120, 'message': 500, 'release_name': 1024, 'group_key': 200}


def check_contract(test, event):
    """The callers' contract (notifications.py); the only place it is checked."""
    test.assertIn(event['type'], EVENT_TYPES)
    test.assertLessEqual(REQUIRED, set(event))
    test.assertLessEqual(set(event), REQUIRED | {'anidb_id', 'job_id', 'release_name', 'group_key', 'severity', 'raw_body'})
    for key, limit in LIMITS.items():
        if event.get(key) is not None:
            test.assertIsInstance(event[key], str)
            test.assertTrue(1 <= len(event[key]) <= limit, key)
    for key in ('anidb_id', 'job_id'):
        if event.get(key) is not None:
            test.assertIsInstance(event[key], int); test.assertGreater(event[key], 0)
    test.assertIn(event.get('severity', 'info'), ('info', 'attention'))
    if event.get('raw_body') is not None:
        test.assertEqual(event['type'], 'API_REJECTION'); test.assertLessEqual(len(event['raw_body']), 65536)
    test.assertNotRegex(event['message'], r'/[a-z]+/|Traceback|api[_-]?key')


class Recorder:
    def __init__(self): self.events = []
    def deliver(self, db, event, now): self.events.append(dict(event))


def no_network(*args, **kwargs):
    raise AssertionError('notify() must not do network I/O')


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        initialize(self.db)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO identified_anime VALUES(1001,2001,'Sousou no Frieren')")
        self.db.commit()

    def rows(self):
        return self.db.execute('SELECT type,severity,title,message,count,anidb_id,job_id FROM notifications').fetchall()

    def test_rolled_back_action_leaves_no_notification_and_no_network(self):
        with patch.object(socket, 'create_connection', no_network), patch.object(urllib.request, 'urlopen', no_network):
            self.db.execute('BEGIN')
            notify(self.db, {'type': 'KEEP_SEARCH_FAILED', 'title': 'x', 'message': 'y'})
            self.db.rollback()
        self.assertEqual(self.rows(), [])

    def test_generic_stores_every_event_and_keeps_severity(self):
        with transaction(self.db):
            notify(self.db, {'type': 'KEEP_SEARCH_FAILED', 'title': 'A', 'message': 'one', 'severity': 'attention'})
            notify(self.db, {'type': 'KEEP_SEARCH_FAILED', 'title': 'B', 'message': 'two'})
        self.assertEqual([row[:5] for row in self.rows()],
                         [('KEEP_SEARCH_FAILED', 'attention', 'A', 'one', 1), ('KEEP_SEARCH_FAILED', 'info', 'B', 'two', 1)])

    def test_episode_trouble_is_one_item_per_series_listing_the_episodes(self):
        plan = self.plan()
        for number in (5, 7):
            with transaction(self.db):
                self.db.execute("INSERT INTO episode_discovery_results VALUES(?,?,1,NULL,NULL,'NO_RESULTS',0,0,0,1)", (plan, number))
                series_trouble(self.db, 1001, job_id=1)
        self.assertEqual(self.rows(), [('SERIES_EPISODE_TROUBLE', 'attention', 'Episode trouble: Sousou no Frieren',
                                        'Sousou no Frieren: trouble getting episodes 5, 7.', 2, 1001, 1)])

    def test_cap_keeps_the_newest_5000(self):
        with transaction(self.db):
            for n in range(inbox.CAP + 3):
                inbox.save(self.db, {'type': 'KEEP_SEARCH_FAILED', 'title': 't', 'message': str(n)}, n)
        self.assertEqual(inbox.unread(self.db), inbox.CAP)
        self.assertEqual(inbox.items(self.db, limit=1)[0]['message'], str(inbox.CAP + 2))
        self.assertIsNone(self.db.execute("SELECT 1 FROM notifications WHERE message IN ('0','1','2')").fetchone())

    def test_every_producer_keeps_the_contract(self):
        recorder = Recorder()
        plan = self.plan()
        with patch.object(notifications, 'CHANNELS', (recorder,)), transaction(self.db):
            self.db.execute("INSERT INTO episode_discovery_results VALUES(?,5,1,NULL,NULL,'NO_RESULTS',0,0,0,1)", (plan,))
            series_trouble(self.db, 1001, job_id=1)
            keep_search_failed(self.db, SimpleNamespace(job_id=1))
        self.assertEqual([event['type'] for event in recorder.events], ['SERIES_EPISODE_TROUBLE', 'KEEP_SEARCH_FAILED'])
        for event in recorder.events:
            with self.subTest(event=event['type']):
                check_contract(self, event)

    def plan(self):
        self.db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'autobrr',x'7b7d',1)")
        self.db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) "
                        "VALUES(1,1,'VERIFY_RELEASE','{}',1,1,1)")
        self.db.execute("INSERT INTO identity_results VALUES(1,1,1001,2,'{}',1)")
        self.db.execute("INSERT INTO identified_episodes VALUES(30001,1001)")
        self.db.execute("INSERT INTO release_verifications VALUES(1,1,1,1001,?,'DECLARED_SINGLE_EPISODE',1,30001,2001,1,1,40001,'official',1)", ('a' * 64,))
        plan = self.db.execute("INSERT INTO user_evaluation_plans(anidb_id,tvdb_series_id,tvdb_season,anchor_verification_id,"
                               "anchor_episode,backfill_count,following_count,configuration_revision,created_at_ms) "
                               "VALUES(1001,2001,1,1,1,6,1,1,1)").lastrowid
        self.db.commit()
        return plan


async def no_work(context):
    return Outcome()


class NeedsReviewTests(unittest.TestCase):
    """An episode job ending in Needs review posts one event; a retry or wait posts none."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'queue.sqlite'
        db = sqlite3.connect(path)
        initialize(db)
        with transaction(db):
            db.execute("INSERT INTO identified_anime VALUES(1001,2001,'Sousou no Frieren')")
            for number, episode in ((1, 5), (2, 7)):
                db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(?,'autobrr',x'7b7d',1)", (number,))
                db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) "
                           "VALUES(?,?,'VERIFY_RELEASE','{}',0,1,1)", (number, number))
                db.execute("INSERT INTO identity_results VALUES(?,?,1001,2,'{}',1)", (number, number))
                db.execute("INSERT INTO identified_episodes VALUES(?,1001)", (30000 + episode,))
                db.execute("INSERT INTO release_verifications VALUES(?,?,?,1001,?,'DECLARED_SINGLE_EPISODE',?,?,2001,NULL,NULL,NULL,'official',1)",
                           (number, number, number, 'a' * 64, episode, 30000 + episode))
        db.close()
        self.now = 1000
        self.queue = JobQueue(path, policy=Policy(lease_ms=1000, max_retries=3, backoff_ms=10, max_backoff_ms=100),
                              clock=lambda: self.now)
        self.spec = Registration('VERIFY_RELEASE', HandlerDefinition('VERIFY_RELEASE', 'Verify release episode'), no_work)
        self.registry = Registry((self.spec,))

    def run_one(self, outcome):
        self.runs = getattr(self, 'runs', 0) + 1
        invocation = f'{self.runs:032x}'
        claim, _ = self.queue.claim()
        self.queue.start(claim, self.spec, invocation, self.registry)
        self.queue.finish(claim, outcome, spec=self.spec, invocation=invocation, registry=self.registry)

    def inbox(self):
        with self.queue.connection() as db:
            return db.execute('SELECT type,message,count FROM notifications').fetchall()

    def test_review_posts_and_retry_does_not(self):
        self.run_one(Outcome('retry', 'SERVICE_UNAVAILABLE'))
        self.assertEqual(self.inbox(), [])
        self.now = 5000
        self.run_one(Outcome('review', 'RELEASE_TVDB_EPISODE_MISSING'))
        self.run_one(Outcome('review', 'RELEASE_TVDB_EPISODE_MISSING'))
        self.assertEqual(self.inbox(), [('SERIES_EPISODE_TROUBLE', 'Sousou no Frieren: trouble getting episodes 5, 7.', 2)])


class NotificationApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiV1Tests.asyncSetUp
    request = ApiV1Tests.request

    def seed(self, *events):
        db = sqlite3.connect(self.database)
        try:
            with transaction(db):
                for n, event in enumerate(events):
                    inbox.save(db, event, n + 1)
        finally:
            db.close()

    async def test_list_delete_actions_and_unread_on_the_service_poll(self):
        self.seed({'type': 'KEEP_SEARCH_FAILED', 'title': 'Search', 'message': 'not started', 'job_id': 4},
                  {'type': 'SERIES_EPISODE_TROUBLE', 'title': 'Trouble', 'message': 'ep 5', 'anidb_id': 9, 'group_key': 'series:9'})
        status, data, _ = await self.request('GET', '/api/v1/notifications', token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(data['data']['unread'], 2)
        self.assertEqual([item['title'] for item in data['data']['items']], ['Trouble', 'Search'])
        self.assertEqual(data['data']['items'][0]['actions'], ['open_series', 'open_job'])
        self.assertNotIn('raw_body', data['data']['items'][0])
        status, data, _ = await self.request('GET', '/api/v1/services', token=ADMIN)
        self.assertEqual(data['data']['notifications_unread'], 2)
        first, second = 2, 1
        status, data, _ = await self.request('POST', f'/api/v1/notifications/{second}/actions/open_series', {}, token=ADMIN)
        self.assertEqual(status, 400)                                    # not an action of KEEP_SEARCH_FAILED
        status, data, _ = await self.request('POST', f'/api/v1/notifications/{second}/actions/open_job', {}, token=ADMIN)
        self.assertEqual((status, data['data']), (200, {'action': 'open_job', 'job_id': 4}))
        status, data, _ = await self.request('DELETE', f'/api/v1/notifications/{second}', token=ADMIN)
        self.assertEqual((status, data['data']['unread']), (200, 1))
        status, _, _ = await self.request('DELETE', f'/api/v1/notifications/{second}', token=ADMIN)
        self.assertEqual(status, 404)
        status, data, _ = await self.request('DELETE', '/api/v1/notifications', token=ADMIN)
        self.assertEqual((status, data['data']), (200, {'deleted': 1, 'unread': 0}))


if __name__ == '__main__':
    unittest.main()
