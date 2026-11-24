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
    def scan(self): self.asked.append(('scan',))
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
        linked, waiting = self.files([('Show/Season 1/E01.mkv', 2001), ('Show/Season 1/E02.mkv', 2002)])
        self.assertEqual((linked, waiting), (1, []))
        # The series is fetched first; E02 is already linked, so only E01 is linked.
        self.assertEqual(self.shoko.asked, [('refresh', 18162), ('link', 77, 501)])

    def test_a_file_shoko_has_not_seen_asks_for_one_scan_and_waits(self):
        self.shoko = FakeShoko({}, {2001: 501})
        linked, waiting = self.files([('a.mkv', 2001), ('b.mkv', 2002)])
        self.assertEqual((linked, len(waiting)), (0, 2))
        self.assertEqual(self.shoko.asked.count(('scan',)), 1)
        self.assertIn('Shoko has not found the file yet', waiting[0])

    def test_an_episode_shoko_has_not_fetched_waits(self):
        self.shoko = FakeShoko({'a.mkv': (77, set())}, {})
        linked, waiting = self.files([('a.mkv', 2001)])
        self.assertEqual((linked, waiting), (0, ['a.mkv: Shoko has not fetched AniDB episode 2001 yet']))

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
