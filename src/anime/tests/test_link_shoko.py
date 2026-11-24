"""After every Keep, AKON tells Shoko which AniDB episode each kept file is (owner, 2026-11-23)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.anime.jobs import link_shoko_job
from src.anime.shoko import Shoko, ShokoRefused
from src.anime.storage.sqlite import initialize


class FakeShoko:
    """Shoko that knows some files and episodes; records what AKON asked."""
    def __init__(self, files=None, episodes=None):
        self.files = files or {}          # relative path -> (file ID, set of linked AniDB episode IDs)
        self.episodes = episodes or {}    # AniDB episode ID -> Shoko episode ID
        self.asked = []

    def refresh_series(self, anidb_id): self.asked.append(('refresh', anidb_id))
    def import_new_files(self): self.asked.append(('import',))
    def find_file(self, path): return self.files.get(path)
    def episode_id(self, eid): return self.episodes.get(eid)
    def link(self, file_id, episode): self.asked.append(('link', file_id, episode))


class LinkShokoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = str(Path(temp.name) / 'anime.sqlite')
        db = sqlite3.connect(self.database)
        initialize(db)
        db.close()

    def files(self, rows):
        with mock.patch.object(link_shoko_job, 'kept_files', return_value=rows):
            return link_shoko_job.link_series(self.database, self.shoko, 18162)

    def test_links_each_kept_file_to_its_episode(self):
        self.shoko = FakeShoko({'Show/Season 1/E01.mkv': (77, set()), 'Show/Season 1/E02.mkv': (78, {2002})},
                               {2001: 501, 2002: 502})
        self.assertEqual(self.files([('Show/Season 1/E01.mkv', 2001), ('Show/Season 1/E02.mkv', 2002)]),
                         (1, [], []))
        # The series is fetched first; E02 is already linked, so only E01 is linked.
        self.assertEqual(self.shoko.asked, [('refresh', 18162), ('link', 77, 501)])

    def test_files_shoko_has_not_recorded_make_it_import_new_files_once(self):
        self.shoko = FakeShoko({}, {2001: 501})
        self.assertEqual(self.files([('a.mkv', 2001), ('b.mkv', 2002)]), (0, ['a.mkv', 'b.mkv'], []))
        self.assertEqual(self.shoko.asked.count(('import',)), 1)

    def test_an_episode_shoko_has_not_fetched_waits(self):
        self.shoko = FakeShoko({'a.mkv': (77, set())}, {})
        self.assertEqual(self.files([('a.mkv', 2001)]), (0, [], ['a.mkv (AniDB episode 2001)']))

    def test_kept_files_of_a_season_pack_come_with_their_anidb_episode(self):
        db = sqlite3.connect(self.database)
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute("INSERT INTO batch_keep_imports VALUES(5,41,0,'COMPLETE',?,1)",
                   (json.dumps({'result': {'relative_path': 'Show/Season 1/E01.mkv'}}),))
        db.execute("INSERT INTO batch_episode_reservations(plan_id,owner_job_id,file_index,anidb_id,anidb_episode_id,"
                   "tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,reserved_at_ms) "
                   "VALUES(41,1,0,18162,281724,438420,1,1,10348657,1)")
        db.commit()
        self.assertEqual(link_shoko_job.kept_files(db, 18162), [('Show/Season 1/E01.mkv', 281724)])
        self.assertEqual(link_shoko_job.kept_files(db, 1), [])
        db.close()


class WaitingTests(unittest.IsolatedAsyncioTestCase):
    """Wait while Shoko catches up; fail only past the deadline; Shoko being down doesn't count."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = str(Path(temp.name) / 'anime.sqlite')
        db = sqlite3.connect(self.database)
        initialize(db)
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute("INSERT INTO jobs(job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) "
                   "VALUES(7,'LINK_SHOKO','{\"anidb_id\": 18162}',0,0,0)")
        db.commit()
        db.close()

    def history(self, at_ms, reason):
        db = sqlite3.connect(self.database)
        db.execute("INSERT INTO job_history(job_id,occurred_at_ms,event_code,reason_code) VALUES(7,?,'DEFERRED',?)",
                   (at_ms, reason))
        db.commit()
        db.close()

    async def outcome(self, result):
        configuration = mock.Mock(value=lambda key: self.database)
        context = mock.Mock(configuration=configuration, claim=mock.Mock(job_id=7),
                            data=lambda: {'anidb_id': 18162})
        with mock.patch.object(link_shoko_job, 'link_series', return_value=result):
            return await link_shoko_job.registration(None, client=lambda *a: None).execute(context)

    async def test_waits_then_fails_after_thirty_minutes_without_the_file(self):
        import time
        now = time.time_ns() // 1_000_000
        self.assertEqual((await self.outcome((0, ['a.mkv'], []))).kind, 'defer')
        self.history(now - link_shoko_job.FILE_DEADLINE_MS - 1, 'MEDIA_LINK_WAITING')
        failed = await self.outcome((0, ['a.mkv'], []))
        self.assertEqual((failed.kind, failed.reason_code), ('fail', 'MEDIA_LINK_INCOMPLETE'))

    async def test_time_shoko_was_down_does_not_count(self):
        import time
        now = time.time_ns() // 1_000_000
        self.history(now - 5 * 3600_000, 'MEDIA_LINK_WAITING')
        self.history(now - 60_000, 'SERVICE_UNAVAILABLE')     # Shoko was down; back now.
        self.assertEqual((await self.outcome((0, ['a.mkv'], []))).kind, 'defer')

    async def test_an_episode_gets_two_hours(self):
        import time
        now = time.time_ns() // 1_000_000
        self.history(now - link_shoko_job.FILE_DEADLINE_MS - 1, 'MEDIA_LINK_WAITING')
        self.assertEqual((await self.outcome((0, [], ['a.mkv (AniDB episode 1)']))).kind, 'defer')

    async def test_done_when_everything_is_linked(self):
        self.assertEqual((await self.outcome((2, [], []))).kind, 'complete')


class PrepareSeriesTests(unittest.TestCase):
    def test_keep_asks_shoko_to_fetch_the_series_and_never_waits_on_it(self):
        from src.anime.shoko import ShokoUnavailable
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        database = str(Path(temp.name) / 'anime.sqlite')
        db = sqlite3.connect(database)
        initialize(db)
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute("INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,validated_at_ms) "
                   "VALUES(3,18162,2,'{}',0)")
        db.commit()
        db.close()
        configuration = mock.Mock(value=lambda key: 'https://shoko.test')
        asked = []
        class Client:
            def __init__(self, *args): pass
            def refresh_series(self, aid): asked.append(aid)
        link_shoko_job.prepare_series(configuration, None, database, 3, client=Client)
        self.assertEqual(asked, [18162])
        class Down(Client):
            def refresh_series(self, aid): raise ShokoUnavailable()
        with self.assertLogs(link_shoko_job.LOG, 'WARNING'):
            link_shoko_job.prepare_series(configuration, None, database, 3, client=Down)


class ShokoClientTests(unittest.TestCase):
    def client(self, answers):
        import io
        import urllib.error
        asked = []

        def send(request, timeout):
            asked.append((request.get_method(), request.full_url.split('/api/v3/')[1], request.data))
            code, body = answers.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(request.full_url, code, 'x', {}, io.BytesIO(body.encode()))
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body.encode()
            return response
        configuration = mock.Mock(value=lambda key: {'shoko.base_url': 'https://shoko.test',
                                                     'shoko.timeout_seconds': 5}[key])
        return Shoko(configuration, mock.Mock(shoko_api_key=lambda: 'k'), transport=send), asked

    def test_find_link_and_missing_episode(self):
        shoko, asked = self.client([
            (200, json.dumps([{'ID': 77, 'SeriesIDs': [{'EpisodeIDs': [{'ID': 9, 'AniDB': 2002}]}]}])),
            (404, 'No Episode entry'), (200, json.dumps({'IDs': {'ID': 501}})), (200, '')])
        self.assertEqual(shoko.find_file('Show/E01.mkv'), (77, {2002}))
        self.assertIsNone(shoko.episode_id(2001))
        self.assertEqual(shoko.episode_id(2001), 501)
        shoko.link(77, 501)
        self.assertEqual(asked[-1], ('POST', 'File/77/Link', b'{"EpisodeIDs": [501]}'))

    def test_a_refusal_carries_shokos_answer(self):
        shoko, _ = self.client([(400, 'Unable to remove AniDB cross-reference')])
        with self.assertRaises(ShokoRefused) as caught:
            shoko.link(77, 501)
        self.assertIn('Unable to remove AniDB cross-reference', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
