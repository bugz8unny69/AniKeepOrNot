"""Offline tests for the Intake status filter (/intake/series?status=...)."""
import sqlite3
import unittest
from unittest import mock

from src.anime.storage import series_intake
from src.anime.storage.sqlite import connect, transaction
from src.anime.tests import test_api_v1 as base


def fake_detail(db, aid, language='english'):
    # Odd ids are staged, ids divisible by 4 wait for dub, the rest track.
    episodes = [{'season': 1, 'episode': 1, 'status': 'Staged'}] if aid % 2 else []
    missing = [{'season': 1, 'episode': 2, 'status': series_intake.WAITING_FOR_DUB,
                'can_retry': True}] if aid % 4 == 0 else []
    return {'title': f'Anime {aid}', 'tvdb_series_id': aid, 'episodes': episodes,
            'missing': missing, 'rejection': None, 'acceptance': None}


class StatusFilterTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE identified_anime(anidb_id INTEGER PRIMARY KEY)')
        self.db.executemany('INSERT INTO identified_anime VALUES(?)', [(i,) for i in range(1, 121)])
        patcher = mock.patch.object(series_intake, 'detail', fake_detail)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.db.close)

    def walk(self, status, limit):
        seen, before = [], 1 << 62
        while True:
            page = series_intake.list_items(self.db, before, limit, status=status)
            self.assertLessEqual(len(page['items']), limit)
            seen += page['items']
            if page['next_before_id'] is None:
                return seen
            before = page['next_before_id']

    def test_filtered_pages_cover_every_match_once_across_scan_chunks(self):
        for status, expected in (('STAGED', [i for i in range(120, 0, -1) if i % 2]),
                                 ('WAITING_FOR_DUB', [i for i in range(120, 0, -1) if i % 4 == 0]),
                                 ('TRACKING', [i for i in range(120, 0, -1) if i % 2 == 0 and i % 4]),
                                 (None, list(range(120, 0, -1)))):
            items = self.walk(status, 7)
            self.assertEqual([row['anidb_id'] for row in items], expected, status)
            if status:
                self.assertTrue(all(row['status'] == status for row in items))
        self.assertEqual(self.walk('ACCEPTED', 30), [])

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(ValueError):
            series_intake.list_items(self.db, 1 << 62, 10, status='Staged')


class StatusFilterApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiV1Tests.asyncSetUp
    rows = base.ApiV1Tests.rows
    request = base.ApiV1Tests.request

    async def test_status_filter_on_list_and_search(self):
        with connect(self.database) as db, transaction(db):
            db.execute("INSERT INTO identified_anime VALUES(1001,2001,'Sousou no Frieren')")
        for path, expected in (('/api/v1/intake/series?status=TRACKING', [1001]),
                               ('/api/v1/intake/series?status=STAGED', []),
                               ('/api/v1/search?scope=intake&q=frieren&status=TRACKING', [1001]),
                               ('/api/v1/search?scope=intake&q=frieren&status=STAGED', [])):
            status, result, _ = await self.request('GET', path, token=base.ADMIN)
            self.assertEqual(status, 200, path)
            self.assertEqual([row['anidb_id'] for row in result['data']['items']], expected, path)
        status, result, _ = await self.request('GET', '/api/v1/intake/series', token=base.ADMIN)
        self.assertEqual(result['data']['items'][0]['status'], 'TRACKING')
        for path in ('/api/v1/intake/series?status=staged', '/api/v1/intake/series?status=STAGED&status=ACCEPTED',
                     '/api/v1/search?scope=jobs&q=frieren&status=STAGED'):
            status, _, _ = await self.request('GET', path, token=base.ADMIN)
            self.assertEqual(status, 400, path)


if __name__ == '__main__':
    unittest.main()
