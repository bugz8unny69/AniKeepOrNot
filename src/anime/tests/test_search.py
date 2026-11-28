"""Offline tests for the scoped dashboard search (/api/v1/search)."""
import unittest

from src.anime.search import SearchInvalid, match, words
from src.anime.storage.sqlite import connect, transaction
from src.anime.tests import test_api_v1 as base

ADMIN, WEB = base.ADMIN, base.WEB
SEASON = ("INSERT INTO recognized_seasons(tvdb_series_id,tvdb_season,anidb_id,display_title,"
          "recognized_at_ms) VALUES(?,1,?,?,1)")
SERIES = "INSERT INTO recognized_series(anidb_id,display_title,tvdb_series_id,recognized_at_ms) VALUES(?,?,?,1)"


# One series row (the series job), as the identity bridge makes it.
ROW = ("INSERT INTO series(anidb_id,title,state,torrents_json,episodes_json,recent_json,created_at_ms,updated_at_ms) "
       "VALUES(?,?,'ASLEEP','[]','{}','[]',1,1)")

class SearchWordsTests(unittest.TestCase):
    def test_words_bounds(self):
        self.assertEqual(words('  frieren  05 '), ['frieren', '05'])
        for bad in ('', ' a ', 'x' * 101, ' '.join(['ab'] * 9), None):
            with self.assertRaises(SearchInvalid):
                words(bad)

    def test_unknown_scope_and_like_escaping(self):
        with self.assertRaises(SearchInvalid):
            match('settings', ['ab'])
        _, params = match('recognized', ['50%_off'])
        self.assertIn('%50\\%\\_off%', params)

    def test_hash_number_is_exact_id_only(self):
        sql, params = match('intake', ['#12'])
        self.assertEqual((sql, params), ('(series.anidb_id=?)', [12]))
        sql, params = match('intake', ['12'])
        self.assertIn('LIKE', sql)
        self.assertEqual(params[0], 12)


class SearchApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiV1Tests.asyncSetUp
    rows = base.ApiV1Tests.rows
    request = base.ApiV1Tests.request
    release = base.ApiV1Tests.release
    # What the API's identity process identifies each test release as (matches the seeded series).
    @staticmethod
    def identity(release_name):
        for key, aid, tid, title in (('Frieren', 1001, 2001, 'Sousou no Frieren'), ('Dandadan', 1002, 2002, 'Dandadan')):
            if key in release_name:
                break
        else:
            aid, tid, title = 1003, 2003, release_name.split(' - ')[0]
        return {'schema_version': 2, 'anime': {'anidb_id': aid, 'tvdb_series_id': tid, 'title': title}, 'episodes': []}

    def seed(self, *statements):
        # Recognized rows normally come from release verification; seed them directly.
        with connect(self.database) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            with transaction(db):
                for sql, *params in statements:
                    db.execute(sql, *params)

    async def search(self, query, token=ADMIN):
        status, result, _ = await self.request('GET', '/api/v1/search?' + query, token=token)
        return status, result.get('data') if status == 200 else result

    async def webhook(self, name, infohash):
        status, receipt, _ = await self.request(
            'POST', '/api/v1/webhook',
            self.release(release_name=name, torrent_infohash=infohash), token=WEB)
        self.assertEqual(status, 202)
        return receipt['data']['anidb_id']

    async def test_requests_are_validated_and_authenticated(self):
        for query in ('q=frieren', 'scope=jobs', 'scope=settings&q=frieren', 'scope=jobs&q=a',
                      'scope=jobs&q=ab&q=cd', 'scope=jobs&q=frieren&extra=1',
                      'scope=recognized&q=frieren&limit=5', 'scope=intake&q=frieren&state=DONE'):
            status, _ = await self.search(query)
            self.assertEqual(status, 400, query)
        for token in (None, WEB):
            status, _ = await self.search('scope=jobs&q=frieren', token=token)
            self.assertEqual(status, 401)
        status, _, _ = await self.request('POST', '/api/v1/search?scope=jobs&q=frieren', {}, token=ADMIN)
        self.assertEqual(status, 405)


    async def test_intake_scope_returns_series_rows_by_title_or_alias(self):
        self.seed((ROW, (1001, 'Sousou no Frieren')),
                  (ROW, (1002, 'Dandadan')),
                  (SEASON, (2001, 1001, 'Sousou no Frieren')), (SERIES, (1001, 'Sousou no Frieren', 2001)),
                  ("INSERT INTO recognized_aliases VALUES('frieren beyond journeys end',1001)",))
        status, data = await self.search('scope=intake&q=journeys')
        self.assertEqual(status, 200)
        self.assertEqual([item['anidb_id'] for item in data['items']], [1001])
        self.assertEqual(set(data['items'][0]),
                         {'anidb_id', 'title', 'state', 'wake_at_ms', 'updated_at_ms', 'episodes',
                          'torrents_active', 'torrents_stopped', 'keep', 'stopped', 'promoted_at_ms'})
        status, data = await self.search('scope=intake&q=dan')
        self.assertEqual([item['anidb_id'] for item in data['items']], [1002])
        status, data = await self.search('scope=intake&q=frieren')
        self.assertEqual([item['anidb_id'] for item in data['items']], [1001])

    async def test_recognized_scope_matches_title_or_alias_only_in_recognized(self):
        self.seed((ROW, (1001, 'Sousou no Frieren')),
                  (ROW, (1002, 'Dandadan')),
                  (SEASON, (2001, 1001, 'Sousou no Frieren')), (SERIES, (1001, 'Sousou no Frieren', 2001)), (SEASON, (2002, 1002, 'Dandadan')), (SERIES, (1002, 'Dandadan', 2002)),
                  ("INSERT INTO recognized_aliases VALUES('frieren beyond journeys end',1001)",))
        await self.webhook('Sousou no Frieren - 05', 'c' * 40)
        status, data = await self.search('scope=recognized&q=journeys')
        self.assertEqual(status, 200)
        self.assertEqual(data['items'], [{'tvdb_series_id': 2001, 'seasons': [1], 'anidb_id': 1001,
                                          # 2: identifying the delivery remembers its release name.
                                          'title': 'Sousou no Frieren', 'anime_type': None, 'alias_count': 2,
                                          'verified_episode_count': 0, 'placed_episode_count': 0}])
        status, data = await self.search('scope=recognized&q=2002')
        self.assertEqual([item['anidb_id'] for item in data['items']], [1002])
        status, full, _ = await self.request('GET', '/api/v1/recognized', token=ADMIN)
        self.assertEqual(len(full['data']['items']), 2)


    async def test_english_title_is_searchable_and_displayed(self):
        self.seed((ROW, (1001, 'Sousou no Frieren')),
                  ("INSERT INTO anime_title_metadata VALUES(1001,'Frieren: Beyond Journey''s End',NULL)",),
                  (SEASON, (2001, 1001, 'Sousou no Frieren')), (SERIES, (1001, 'Sousou no Frieren', 2001)))
        for scope in ('intake', 'recognized'):
            status, data = await self.search(f'scope={scope}&q=beyond')
            self.assertEqual(status, 200)
            self.assertEqual([item['title'] for item in data['items']],
                             ["Frieren: Beyond Journey's End"], scope)




if __name__ == '__main__':
    unittest.main()
