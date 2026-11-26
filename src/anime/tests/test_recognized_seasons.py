"""Recognized names: what the series job verified lets /check answer a known series at once."""
import json
import sqlite3
import unittest

from src.anime.storage.recognition import RecognitionConflict, recognized_episode, recognized_for_release, \
    remember_release
from src.anime.storage.sqlite import initialize, transaction


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)
        self.addCleanup(self.db.close)

    def remember(self, season, episode, *, aid=1001, tid=2001, title='Example Anime', number=None):
        number = episode if number is None else number
        with transaction(self.db):
            remember_release(self.db, anidb_id=aid, title=title, tvdb_series_id=tid,
                             raw_body=json.dumps({"release_name": f"{title} S{season:02d}E{episode:02d}"}).encode(),
                             season=season, episodes=[(number, aid * 10 + number, season, episode,
                                                       aid * 100 + season * 10 + episode)], when=1)

    def test_two_verified_works_can_share_tvdb_season(self):
        self.remember(1, 3)
        self.remember(1, 4, aid=1002, title='Second Cour', number=1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM recognized_seasons WHERE tvdb_season=1").fetchone()[0], 2)
        self.assertEqual(recognized_for_release(self.db, 'Second Cour S01E04', season=1)['anidb_id'], 1002)
        self.assertEqual(recognized_for_release(self.db, 'Example Anime S01E03', season=1)['anidb_id'], 1001)
        self.assertEqual(recognized_episode(self.db, aid=1002, tid=2001, origin='tvdb', season=1, number=4)
                         ['anidb_episode_id'], 10021)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_shared_alias_is_ambiguous_instead_of_first_owner_winning(self):
        self.remember(1, 3)
        self.remember(1, 4, aid=1002, tid=2002, title='Example Anime', number=1)
        self.assertIsNone(recognized_for_release(self.db, 'Example Anime S01E09', season=1))
        self.assertEqual(self.db.execute('SELECT count(*) FROM recognized_episodes').fetchone()[0], 2)

    def test_automatic_series_season_recognition_is_not_per_episode(self):
        self.remember(1, 3)
        self.remember(2, 1, number=13)
        self.assertEqual(self.db.execute("SELECT count(*) FROM recognized_seasons").fetchone()[0], 2)
        self.assertEqual(recognized_for_release(self.db, "Example Anime S01E09", season=1)["anidb_id"], 1001)
        second = recognized_for_release(self.db, "Example Anime S02E03", season=2)
        self.assertEqual((second["tvdb_series_id"], second["tvdb_season"]), (2001, 2))
        self.assertEqual(recognized_for_release(self.db, "Example Anime - 01")["anidb_id"], 1001)
        self.assertIsNone(recognized_for_release(self.db, "Unknown Anime S02E01", season=2))
        self.assertEqual(recognized_episode(self.db, aid=1001, tid=2001, origin='tvdb', season=2, number=1)
                         ['anidb_episode_id'], 10023)
        self.assertEqual(recognized_episode(self.db, aid=1001, tid=2001, origin='anidb', season=None, number=3)
                         ['tvdb_season'], 1)
        self.assertIsNone(recognized_episode(self.db, aid=1001, tid=2001, origin='tvdb', season=2, number=5))

    def test_a_conflicting_placement_is_refused_and_rolled_back(self):
        self.remember(1, 3)
        with self.assertRaises(RecognitionConflict):
            self.remember(1, 4, number=3)            # AniDB 3 was already placed at S01E03.
        self.assertEqual(self.db.execute('SELECT count(*) FROM recognized_episodes').fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
