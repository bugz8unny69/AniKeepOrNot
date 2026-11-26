"""After every Keep, AKON tells Shoko which AniDB episode each kept file is (owner, 2026-11-23)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        from src.anime.series_job.shoko_link import link_files
        return link_files(self.shoko, 18162, rows)

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
