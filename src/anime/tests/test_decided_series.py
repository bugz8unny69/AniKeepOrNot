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
from src.anime.tests.test_keep_sonarr import KeepSonarrTests

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
                db.execute("INSERT INTO identified_anime VALUES(1001,2001,'Sousou no Frieren')")
                db.execute("INSERT INTO jobs(job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) "
                           "VALUES(900,'REJECT_SERIES','{}',0,1,1)")
                db.execute("INSERT INTO anime_review_decisions(anidb_id,operation_id,job_id,decision,cleanup_state,decided_at_ms) "
                           "VALUES(1001,1,900,?,'COMPLETE',1)", (decision,))
                # Found through Recognized names (P2), from local data only.
                db.execute("INSERT INTO recognized_series VALUES(1001,'Sousou no Frieren',2001,NULL,1)")
                db.execute("INSERT INTO recognized_aliases VALUES(?,1001)", (_alias('Sousou no Frieren'),))

    def counts(self):
        with connect(self.database) as db:
            return tuple(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                         for table in ('incoming_events', 'jobs', 'notifications'))

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

    async def test_undecided_and_unknown_series_are_queued_as_before(self):
        status, data, _ = await self.request('POST', '/api/v1/webhook', self.release(release_name=NAME), token=WEB)
        self.assertEqual(status, 202)
        self.seed('ACCEPT')
        status, _, _ = await self.request('POST', '/api/v1/webhook', self.release(release_name='Dandadan - 03'), token=WEB)
        self.assertEqual(status, 202)

    async def test_process_anyway_creates_exactly_one_identify_job_that_skips_the_check(self):
        self.seed('REJECT')
        await self.request('POST', '/api/v1/webhook', self.release(release_name=NAME), token=WEB)
        with connect(self.database) as db:
            number = db.execute('SELECT notification_id FROM notifications').fetchone()[0]
        status, data, _ = await self.request('POST', f'/api/v1/notifications/{number}/actions/process_anyway', {}, token=ADMIN)
        self.assertEqual(status, 202)
        with connect(self.database) as db:
            kind, payload = db.execute("SELECT kind,payload_json FROM jobs WHERE job_id=?", (data['data']['job_id'],)).fetchone()
            self.assertEqual((kind, json.loads(payload)), ('IDENTIFY_ANIME', {'process_anyway': True}))
            self.assertEqual(db.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)
            self.assertEqual(json.loads(db.execute('SELECT raw_body FROM incoming_events WHERE event_id=?',
                                                   (data['data']['event_id'],)).fetchone()[0])['release_name'], NAME)


class IdentifyRefusalTests(unittest.IsolatedAsyncioTestCase):
    from src.anime.tests.test_identify_anime_job import IdentifyAnimeJobTests as _Base
    asyncSetUp = _Base.asyncSetUp
    enqueue = _Base.enqueue
    rows = _Base.rows
    worker = _Base.worker

    async def identify(self, payload=None):
        from src.anime.jobs.identify_anime_job import registration
        from src.anime.handlers import HandlerDefinition
        from src.anime.job_contracts import Outcome, Registration
        async def resolver(request, configuration, cancelled):
            return {'schema_version': 2, 'anime': {'anidb_id': 1001, 'tvdb_series_id': 2001, 'title': 'Sousou no Frieren'},
                    'episodes': [], 'attributes': {}}
        async def verify(context):
            return Outcome()
        successor = Registration('VERIFY_RELEASE', HandlerDefinition('VERIFY_RELEASE', 'Verify release episode'), verify)
        _, job = self.enqueue(payload, raw_body=json.dumps({'release_name': NAME}).encode())
        worker = self.worker(registration(resolver, release_kind='VERIFY_RELEASE'), successor)
        self.assertTrue(await worker.attempt())
        return job

    def decide(self, decision):
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute('PRAGMA defer_foreign_keys=ON')
                db.execute("INSERT OR IGNORE INTO identified_anime VALUES(1001,2001,'Sousou no Frieren')")
                db.execute("INSERT INTO jobs(job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) "
                           "VALUES(900,'REJECT_SERIES','{}',9000000000000,1,1)")
                operation = db.execute("INSERT INTO job_operations(state,cleanup_state,created_at_ms,updated_at_ms) "
                                       "VALUES('ACTIVE','NONE',1,1)").lastrowid
                db.execute("INSERT INTO anime_review_decisions(anidb_id,operation_id,job_id,decision,cleanup_state,decided_at_ms) "
                           "VALUES(1001,?,900,?,'COMPLETE',1)", (operation, decision))
        finally:
            db.close()

    async def test_a_queued_delivery_for_a_decided_series_is_done_with_no_successor(self):
        self.decide('ACCEPT')
        job = await self.identify()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?', (job,)), [('DONE',)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='VERIFY_RELEASE'"), [(0,)])
        self.assertEqual(self.rows("SELECT reason_code FROM job_history WHERE job_id=? AND event_code='DELIVERY_REFUSED'", (job,)),
                         [('ANIME_ACCEPTED',)])
        self.assertEqual(self.rows('SELECT type,job_id FROM notifications'), [('API_REJECTION', job)])
        self.assertEqual(self.rows('SELECT count(*) FROM recognized_aliases WHERE anidb_id=1001'), [(1,)])  # alias recorded

    async def test_process_anyway_is_not_refused_again(self):
        self.decide('REJECT')
        job = await self.identify({'process_anyway': True})
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='VERIFY_RELEASE' AND created_by_job_id=?", (job,)), [(1,)])
        self.assertEqual(self.rows('SELECT count(*) FROM notifications'), [(0,)])


class KeepAcceptsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = KeepSonarrTests.asyncSetUp
    rows = KeepSonarrTests.rows
    source = KeepSonarrTests.source
    queued = KeepSonarrTests.queued
    due = KeepSonarrTests.due
    ready = KeepSonarrTests.ready
    staged_ready = KeepSonarrTests.staged_ready
    prepared = KeepSonarrTests.prepared

    async def test_keep_records_accept_the_status_is_accepted_and_keep_still_runs(self):
        from src.anime.storage.series_intake import detail, _summary, reject_series, IntakeUnavailable
        worker, fake, actions, target, job = await self.prepared()
        aid = fake.selection.anidb_id
        self.assertEqual(self.rows('SELECT decision,job_id FROM anime_review_decisions WHERE anidb_id=?', (aid,)),
                         [('ACCEPT', job)])
        self.assertEqual(self.rows("SELECT event_code,reason_code FROM job_history WHERE job_id=? AND event_code='SERIES_ACCEPTED'", (job,)),
                         [('SERIES_ACCEPTED', 'ANIME_ACCEPTED')])
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?', (job,)), [('DONE',)])   # passes require_allowed
        db = connect(self.database)
        try:
            self.assertEqual(_summary(db, aid, 'english')['status'], 'ACCEPTED')
            self.assertEqual(detail(db, aid)['reject_operations'], [])
        finally:
            db.close()
        with self.assertRaises(IntakeUnavailable):
            reject_series(self.database, aid, [], 'Other', '')


class AcceptKeptSeriesFixTests(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / 'anime.sqlite'
        db = sqlite3.connect(database); initialize(db); db.close()
        before = database.read_bytes()
        result = subprocess.run([sys.executable, 'fixes/accept_kept_series.py', '--database', str(database)],
                                capture_output=True, text=True, cwd=Path(__file__).resolve().parents[3])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.split('\nPreview')[0])['series'], [])
        self.assertEqual(database.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
