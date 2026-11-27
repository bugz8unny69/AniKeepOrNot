"""Work item P4: Keep accepts the series; decided series are refused at the webhook."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

from src.anime.providers.identity import cached_anidb_id
from src.anime.providers.http import Clients, Options
from src.anime.storage.recognition import _alias
from src.anime.storage.sqlite import connect, initialize, transaction
from src.anime.tests.test_api_v1 import ApiV1Tests, ADMIN, WEB

NAME = 'Sousou no Frieren - 05 [1080p]'


class CachedLookupTests(unittest.TestCase):
    """cached_anidb_id never asks a provider for anything."""
    def setUp(self):
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = str(Path(temporary.name) / 'anime.sqlite')
        db = sqlite3.connect(self.database); initialize(db); db.close()

    def test_title_list_and_anime_record_only_never_anime_lists_or_tvdb(self):
        calls = []
        class Cached:
            def title_candidates(self, aliases):
                calls.append('titles'); return {1001: ('Sousou no Frieren',)}
            def anime(self, aid):
                calls.append('anime'); return {'anidb_id': aid, 'title': 'Sousou no Frieren', 'titles': ('Sousou no Frieren',)}
            def __getattr__(self, name):
                raise AssertionError(f'{name} must not be called')       # mapping, season_entry, series
        self.assertEqual(cached_anidb_id(self.database, None, NAME, client=Cached()), 1001)
        self.assertEqual(set(calls), {'titles', 'anime'})

    def test_missing_or_expired_cache_is_unknown_and_nothing_is_fetched(self):
        def opener(*args, **kwargs):
            raise AssertionError('a cache-only lookup must not fetch')
        client = Clients(self.database, Options('registeredclient', 1), opener=opener, cache_only=True)
        self.assertIsNone(cached_anidb_id(self.database, None, NAME, client=client))
        db = sqlite3.connect(self.database)
        db.execute("INSERT INTO provider_public_cache VALUES('anidb_titles','titles',0,1,?)",   # expired long ago
                   (b'<animetitles><anime aid="1001"><title>Sousou no Frieren</title></anime></animetitles>',))
        db.commit(); db.close()
        self.assertIsNone(cached_anidb_id(self.database, None, NAME, client=client))

    def test_a_season_redirect_is_unknown(self):
        class Cached:
            def __getattr__(self, name):
                raise AssertionError('not reached')
        self.assertIsNone(cached_anidb_id(self.database, None, 'Sousou no Frieren S02E03', client=Cached()))


class WebhookRefusalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiV1Tests.asyncSetUp
    request = ApiV1Tests.request
    release = ApiV1Tests.release

    def seed(self, decision):
        with connect(self.database) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            with transaction(db):
                # Kept or rejected: the series row is the decision (the series job).
                db.execute("INSERT INTO series(anidb_id,title,state,torrents_json,episodes_json,recent_json,"
                           "created_at_ms,updated_at_ms) VALUES(1001,'Sousou no Frieren',?,'[]','{}','[]',1,1)",
                           ({'ACCEPT': 'KEPT', 'REJECT': 'REJECTED'}[decision],))
                # Found through Recognized names (P2), from local data only.
                db.execute("INSERT INTO recognized_series VALUES(1001,'Sousou no Frieren',2001,NULL,1)")
                db.execute("INSERT INTO recognized_aliases VALUES(?,1001)", (_alias('Sousou no Frieren'),))

    def counts(self):
        with connect(self.database) as db:
            return (db.execute("SELECT coalesce(sum(json_array_length(torrents_json)),0) FROM series").fetchone()[0],
                    0, db.execute('SELECT count(*) FROM notifications').fetchone()[0])

    async def test_decided_series_are_refused_with_200_no_work_and_one_bell_item(self):
        for decision, code in (('ACCEPT', 'ANIME_ACCEPTED'), ('REJECT', 'ANIME_REJECTED')):
            with self.subTest(decision=decision):
                await self.asyncSetUp()
                self.seed(decision)
                before = self.counts()
                for _ in range(2):
                    status, data, _ = await self.request('POST', '/api/v1/webhook', self.release(release_name=NAME), token=WEB)
                    self.assertEqual((status, data['data']), (200, {'accepted': False, 'reason_code': code}))
                events, jobs, bell = self.counts()
                self.assertEqual((events, jobs), before[:2])
                self.assertEqual(bell, 1)                                  # same release: one item, ×2
                with connect(self.database) as db:
                    row = db.execute('SELECT type,anidb_id,count,raw_body IS NOT NULL FROM notifications').fetchone()
                self.assertEqual(row, ('API_REJECTION', 1001, 2, 1))

    async def test_undecided_and_unknown_series_are_queued_for_the_identity_process(self):
        self.identity = None   # Still being identified.
        status, data, _ = await self.request('POST', '/api/v1/webhook', self.release(release_name=NAME), token=WEB)
        self.assertEqual(status, 202)
        self.seed('ACCEPT')
        status, _, _ = await self.request('POST', '/api/v1/webhook', self.release(release_name='Dandadan - 03', torrent_infohash='d' * 40), token=WEB)
        self.assertEqual(status, 202)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM identity_requests').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM series').fetchone()[0], 1)   # Only the seeded one.

    async def test_process_anyway_identifies_again_and_skips_the_decision(self):
        self.identity = {'schema_version': 2, 'anime': {'anidb_id': 1001, 'tvdb_series_id': 2001,
                                                        'title': 'Sousou no Frieren'}, 'episodes': []}
        self.seed('REJECT')
        await self.request('POST', '/api/v1/webhook', self.release(release_name=NAME), token=WEB)
        with connect(self.database) as db:
            number = db.execute('SELECT notification_id FROM notifications').fetchone()[0]
        status, data, _ = await self.request('POST', f'/api/v1/notifications/{number}/actions/process_anyway', {}, token=ADMIN)
        self.assertEqual(status, 202)
        with connect(self.database) as db:
            # The rejected series is reopened with the release (Process anyway).
            state, torrents = db.execute("SELECT state,torrents_json FROM series WHERE anidb_id=1001").fetchone()
            self.assertEqual((state, [t['release_name'] for t in json.loads(torrents)]), ('WORKING', [NAME]))
            self.assertEqual(db.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)









if __name__ == '__main__':
    unittest.main()
