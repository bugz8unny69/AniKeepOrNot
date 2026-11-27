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
from src.anime.storage import notifications as inbox
from src.anime.storage.sqlite import initialize, transaction
from src.anime.tests.test_api_v1 import ApiV1Tests, ADMIN
from src.anime.notifications import EVENT_TYPES, notify

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


    def test_cap_keeps_the_newest_5000(self):
        with transaction(self.db):
            for n in range(inbox.CAP + 3):
                inbox.save(self.db, {'type': 'KEEP_SEARCH_FAILED', 'title': 't', 'message': str(n)}, n)
        self.assertEqual(inbox.unread(self.db), inbox.CAP)
        self.assertEqual(inbox.items(self.db, limit=1)[0]['message'], str(inbox.CAP + 2))
        self.assertIsNone(self.db.execute("SELECT 1 FROM notifications WHERE message IN ('0','1','2')").fetchone())









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
        self.assertEqual(data['data']['items'][0]['actions'], ['open_series'])   # No job numbers.
        self.assertNotIn('raw_body', data['data']['items'][0])
        status, data, _ = await self.request('GET', '/api/v1/services', token=ADMIN)
        self.assertEqual(data['data']['notifications_unread'], 2)
        first, second = 2, 1
        status, data, _ = await self.request('POST', f'/api/v1/notifications/{second}/actions/open_series', {}, token=ADMIN)
        self.assertEqual(status, 409)                                    # this notice names no series
        status, data, _ = await self.request('DELETE', f'/api/v1/notifications/{second}', token=ADMIN)
        self.assertEqual((status, data['data']['unread']), (200, 1))
        status, _, _ = await self.request('DELETE', f'/api/v1/notifications/{second}', token=ADMIN)
        self.assertEqual(status, 404)
        status, data, _ = await self.request('DELETE', '/api/v1/notifications', token=ADMIN)
        self.assertEqual((status, data['data']), (200, {'deleted': 1, 'unread': 0}))


if __name__ == '__main__':
    unittest.main()
