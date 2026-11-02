"""Offline tests for the scoped dashboard search (/api/v1/search)."""
import unittest

from src.nextgen.search import SearchInvalid, match, words
from src.nextgen.storage.sqlite import connect, transaction
from src.nextgen.tests import test_api_v1 as base

ADMIN, WEB = base.ADMIN, base.WEB
SEASON = ("INSERT INTO recognized_seasons(tvdb_series_id,tvdb_season,anidb_id,display_title,"
          "evidence_verification_id,recognized_at_ms) VALUES(?,1,?,?,1,1)")


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
        sql, params = match('jobs', ['#12'])
        self.assertEqual((sql, params), ('(jobs.job_id=?)', [12]))
        sql, params = match('jobs', ['12'])
        self.assertIn('LIKE', sql)
        self.assertEqual(params[0], 12)


class SearchApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiV1Tests.asyncSetUp
    rows = base.ApiV1Tests.rows
    request = base.ApiV1Tests.request
    release = base.ApiV1Tests.release

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
        return receipt['data']['job_id']

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

    async def test_jobs_scope_matches_release_name_job_id_and_keeps_page_filters(self):
        frieren = await self.webhook('[SubsPlease] Sousou no Frieren - 05 (1080p)', 'a' * 40)
        other = await self.webhook('[SubsPlease] Dandadan - 05 (1080p)', 'b' * 40)
        status, data = await self.search('scope=jobs&q=FRIEREN+05')
        self.assertEqual(status, 200)
        self.assertEqual([item['job_id'] for item in data['items']], [frieren])
        self.assertEqual(data['items'][0]['release_name'], '[SubsPlease] Sousou no Frieren - 05 (1080p)')
        status, data = await self.search('scope=jobs&q=%23' + str(other))
        self.assertEqual([item['job_id'] for item in data['items']], [other])
        status, data = await self.search('scope=jobs&q=subsplease&before_id=9223372036854775807&limit=1')
        self.assertEqual([item['job_id'] for item in data['items']], [other])
        self.assertEqual(data['next_before_id'], other)
        with connect(self.database) as db, transaction(db):
            db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?", (frieren,))
        status, data = await self.search('scope=jobs&q=frieren&show_completed=0')
        self.assertEqual(data['items'], [])
        status, data = await self.search('scope=jobs&q=frieren&state=DONE')
        self.assertEqual([item['job_id'] for item in data['items']], [frieren])
        status, data = await self.search('scope=jobs&q=100%25')
        self.assertEqual(data['items'], [])

    async def test_intake_scope_returns_series_rows_by_title_or_alias(self):
        self.seed(("INSERT INTO identified_anime VALUES(1001,2001,'Sousou no Frieren')",),
                  ("INSERT INTO identified_anime VALUES(1002,2002,'Dandadan')",),
                  (SEASON, (2001, 1001, 'Sousou no Frieren')),
                  ("INSERT INTO recognized_aliases VALUES('frieren beyond journeys end',1,2001,1001)",))
        status, data = await self.search('scope=intake&q=journeys')
        self.assertEqual(status, 200)
        self.assertEqual([item['anidb_id'] for item in data['items']], [1001])
        self.assertEqual(set(data['items'][0]),
                         {'anidb_id', 'title', 'tvdb_series_id', 'staged_count', 'kept_count',
                          'missing_count', 'season_count', 'rejection'})
        status, data = await self.search('scope=intake&q=dan')
        self.assertEqual([item['anidb_id'] for item in data['items']], [1002])
        status, data = await self.search('scope=intake&q=frieren')
        self.assertEqual([item['anidb_id'] for item in data['items']], [1001])

    async def test_recognized_scope_matches_title_or_alias_only_in_recognized(self):
        self.seed(("INSERT INTO identified_anime VALUES(1001,2001,'Sousou no Frieren')",),
                  ("INSERT INTO identified_anime VALUES(1002,2002,'Dandadan')",),
                  (SEASON, (2001, 1001, 'Sousou no Frieren')), (SEASON, (2002, 1002, 'Dandadan')),
                  ("INSERT INTO recognized_aliases VALUES('frieren beyond journeys end',1,2001,1001)",))
        await self.webhook('Sousou no Frieren - 05', 'c' * 40)
        status, data = await self.search('scope=recognized&q=journeys')
        self.assertEqual(status, 200)
        self.assertEqual(data['items'], [{'tvdb_series_id': 2001, 'season': 1, 'anidb_id': 1001,
                                          'title': 'Sousou no Frieren', 'alias_count': 1,
                                          'verified_episode_count': 0}])
        status, data = await self.search('scope=recognized&q=2002')
        self.assertEqual([item['anidb_id'] for item in data['items']], [1002])
        status, full, _ = await self.request('GET', '/api/v1/recognized', token=ADMIN)
        self.assertEqual(len(full['data']['items']), 2)


    async def test_english_title_is_searchable_and_displayed(self):
        self.seed(("INSERT INTO identified_anime VALUES(1001,2001,'Sousou no Frieren')",),
                  ("INSERT INTO anime_title_metadata VALUES(1001,'Frieren: Beyond Journey''s End',NULL)",),
                  (SEASON, (2001, 1001, 'Sousou no Frieren')))
        for scope in ('intake', 'recognized'):
            status, data = await self.search(f'scope={scope}&q=beyond')
            self.assertEqual(status, 200)
            self.assertEqual([item['title'] for item in data['items']],
                             ["Frieren: Beyond Journey's End"], scope)


    async def test_jobs_scope_matches_activity_details_and_job_label(self):
        from src.nextgen.history import append_job_history
        stuck = await self.webhook('Show A - 01', 'd' * 40)
        stopped = await self.webhook('Show B - 01', 'e' * 40)
        with connect(self.database) as db, transaction(db):
            db.execute("UPDATE jobs SET state='NEEDS_REVIEW',last_reason_code='JOB_RETRY_EXHAUSTED' "
                       "WHERE job_id=?", (stuck,))
            db.execute("UPDATE jobs SET state='NEEDS_REVIEW',attempt_count=1,last_reason_code='IDENTITY_INCOMPLETE' "
                       "WHERE job_id=?", (stopped,))
            append_job_history(db, job_id=stopped, event_code='NEEDS_REVIEW',
                               reason_code='IDENTITY_INCOMPLETE', component='worker', occurred_at_ms=1,
                               details={'attempt': 1, 'failed_check': 'src/nextgen/acquisition/audio.py:80 audio_probe',
                                        'failed_path': '/staging/b.mkv'})
        status, data = await self.search('scope=jobs&q=retries&state=NEEDS_REVIEW')
        self.assertEqual(status, 200)
        self.assertEqual([item['job_id'] for item in data['items']], [stuck])
        self.assertEqual(data['items'][0]['reason']['message'], 'Automatic retries have stopped.')
        status, data = await self.search('scope=jobs&q=audio_probe')
        self.assertEqual([item['job_id'] for item in data['items']], [stopped])
        status, data = await self.search('scope=jobs&q=retries&state=DONE')
        self.assertEqual(data['items'], [])
        status, data = await self.search('scope=jobs&q=identify+series')
        self.assertEqual(sorted(item['job_id'] for item in data['items']), sorted([stuck, stopped]))


if __name__ == '__main__':
    unittest.main()
