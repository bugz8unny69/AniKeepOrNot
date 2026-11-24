"""Tests for SJ-4f: stage step."""
import asyncio
import unittest
from types import SimpleNamespace

from src.anime.series_job.steps.stage import stage
from src.anime.series_job.store import SeriesRow
from src.anime.staging import StagingReview, StagingUnavailable


class StageStepTests(unittest.TestCase):
    def setUp(self):
        self.series = SeriesRow(
            anidb_id=16999,
            title="Cyberpunk: Edgerunners",
            state="WORKING",
            wake_at_ms=None,
            torrents_at_once=1,
            episodes={"S01E05": {"state": "downloading", "torrent": "a" * 40, "file": None}},
        )
        self.torrent = {
            "infohash": "a" * 40,
            "release_name": "[SubsPlease] Cyberpunk - 05 (1080p).mkv",
            "episodes": ["S01E05"],
            "files": [{"name": "Cyberpunk - 05.mkv", "size": 1024000}],
            "step": "stage",
            "state": "working",
        }

    def test_stage_successfully(self):
        context = SimpleNamespace(now=1000)
        result = asyncio.run(stage(self.series, self.torrent, context))

        self.assertEqual(result.action, "done")
        self.assertEqual(result.next_step, "done")
        self.assertEqual(self.torrent["step"], "done")
        self.assertEqual(self.torrent["state"], "done")
        self.assertEqual(self.series.episodes["S01E05"]["state"], "staged")
        self.assertEqual(self.series.episodes["S01E05"]["file"], "Cyberpunk: Edgerunners/S01E05.mkv")
        self.assertTrue(any("staged for evaluation" in line for line in self.series.recent))

    def test_stager_review_error_drops_torrent(self):
        async def failing_stager(s, t, c):
            raise StagingReview("permission denied")

        context = SimpleNamespace(stager=failing_stager, now=1000)
        result = asyncio.run(stage(self.series, self.torrent, context))

        self.assertEqual(result.action, "drop")
        self.assertEqual(result.why, "STAGING_REVIEW")

    def test_stager_unavailable_waits(self):
        async def unavailable_stager(s, t, c):
            raise StagingUnavailable()

        context = SimpleNamespace(stager=unavailable_stager, now=1000)
        result = asyncio.run(stage(self.series, self.torrent, context))

        self.assertEqual(result.action, "wait")
        self.assertEqual(result.wake_at_ms, 31000)
        self.assertEqual(result.why, "STAGING_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
