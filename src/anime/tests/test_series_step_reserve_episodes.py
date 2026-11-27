"""Tests for SJ-4b: reserve_episodes step."""
import asyncio
import unittest
from types import SimpleNamespace

from src.anime.series_job.steps.reserve_episodes import reserve_episodes
from src.anime.series_job.store import SeriesRow


class ReserveEpisodesStepTests(unittest.TestCase):
    def setUp(self):
        self.series = SeriesRow(
            anidb_id=16999,
            title="Cyberpunk: Edgerunners",
            state="WORKING",
            wake_at_ms=None,
            torrents_at_once=1,
        )

    def test_reserve_episodes_successfully(self):
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[Group] Show - 01",
            "episodes": ["S01E01"],
            "step": "reserve_episodes",
            "state": "waiting",
        }
        context = SimpleNamespace(now=1000)
        result = asyncio.run(reserve_episodes(self.series, torrent, context))

        self.assertEqual(result.action, "done")
        self.assertEqual(result.next_step, "download")
        self.assertIn("S01E01", self.series.episodes)
        self.assertEqual(self.series.episodes["S01E01"]["state"], "reserved")
        self.assertEqual(self.series.episodes["S01E01"]["torrent"], "a" * 40)
        self.assertEqual(torrent["step"], "download")

    def test_two_torrents_never_work_on_same_episode_at_once(self):
        # Torrent 1 has reserved S01E01
        self.series.episodes["S01E01"] = {
            "state": "reserved",
            "torrent": "a" * 40,
            "file": None,
        }

        # Torrent 2 attempts to reserve S01E01
        torrent2 = {
            "infohash": "b" * 40,
            "release_name": "[OtherGroup] Show - 01",
            "episodes": ["S01E01"],
            "step": "reserve_episodes",
            "state": "waiting",
        }
        context = SimpleNamespace(now=1000)
        result = asyncio.run(reserve_episodes(self.series, torrent2, context))

        self.assertEqual(result.action, "wait")
        self.assertIn("waiting for episode S01E01", result.why)
        # Reservation remains owned by Torrent 1
        self.assertEqual(self.series.episodes["S01E01"]["torrent"], "a" * 40)

    def test_torrent_dropped_if_episodes_already_staged(self):
        self.series.episodes["S01E01"] = {
            "state": "staged",
            "torrent": "a" * 40,
            "file": "staged/Show/S01E01.mkv",
        }
        torrent = {
            "infohash": "c" * 40,
            "release_name": "[Duplicate] Show - 01",
            "episodes": ["S01E01"],
            "step": "reserve_episodes",
            "state": "waiting",
        }
        context = SimpleNamespace(now=1000)
        result = asyncio.run(reserve_episodes(self.series, torrent, context))

        self.assertEqual(result.action, "drop")
        self.assertEqual(result.why, "EPISODE_ALREADY_STAGED")


if __name__ == "__main__":
    unittest.main()
