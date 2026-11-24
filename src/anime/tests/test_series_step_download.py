"""Tests for SJ-4c: submit and download step."""
import asyncio
import unittest
from types import SimpleNamespace

from src.anime.series_job.steps.download import download
from src.anime.series_job.store import SeriesRow


class DownloadStepTests(unittest.TestCase):
    def setUp(self):
        self.series = SeriesRow(
            anidb_id=16999,
            title="Cyberpunk: Edgerunners",
            state="WORKING",
            wake_at_ms=None,
            torrents_at_once=1,
            episodes={"S01E05": {"state": "reserved", "torrent": "a" * 40, "file": None}},
        )

    def test_initial_submission_starts_download(self):
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[SubsPlease] Cyberpunk - 05 (1080p).mkv",
            "episodes": ["S01E05"],
            "step": "download",
            "state": "waiting",
            "submitted_at_ms": None,
        }
        added = False

        class FakeClient:
            async def add(self, t):
                nonlocal added
                added = True

        context = SimpleNamespace(qb_client=FakeClient(), now=1000)
        result = asyncio.run(download(self.series, torrent, context))

        self.assertEqual(result.action, "wait")
        self.assertEqual(result.wake_at_ms, 6000)
        self.assertTrue(added)
        self.assertEqual(torrent["submitted_at_ms"], 1000)
        self.assertEqual(torrent["state"], "working")
        self.assertEqual(self.series.episodes["S01E05"]["state"], "downloading")
        self.assertTrue(any("download started" in line for line in self.series.recent))

    def test_progress_monitoring_only_logs_when_changed(self):
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[SubsPlease] Cyberpunk - 05 (1080p).mkv",
            "episodes": ["S01E05"],
            "step": "download",
            "state": "working",
            "submitted_at_ms": 1000,
            "progress": 0.0,
        }

        class FakeClient:
            def __init__(self, p):
                self.p = p
            async def get_status(self, h):
                return {"progress": self.p, "files": []}

        # Progress changed to 53%
        context = SimpleNamespace(qb_client=FakeClient(0.534), now=2000)
        result = asyncio.run(download(self.series, torrent, context))
        self.assertEqual(result.action, "wait")
        self.assertAlmostEqual(torrent["progress"], 0.534)
        self.assertTrue(any("downloading, 53%" in line for line in self.series.recent))

        # Progress unchanged at 53% -> logs nothing new
        count_before = len(self.series.recent)
        context2 = SimpleNamespace(qb_client=FakeClient(0.538), now=3000)
        result2 = asyncio.run(download(self.series, torrent, context2))
        self.assertEqual(result2.action, "wait")
        self.assertEqual(len(self.series.recent), count_before)

    def test_download_completion(self):
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[SubsPlease] Cyberpunk - 05 (1080p).mkv",
            "episodes": ["S01E05"],
            "step": "download",
            "state": "working",
            "submitted_at_ms": 1000,
            "progress": 0.95,
        }

        class FakeClient:
            async def get_status(self, h):
                return {"progress": 1.0, "files": [{"name": "Cyberpunk - 05.mkv", "size": 1024000}]}

        context = SimpleNamespace(qb_client=FakeClient(), now=5000)
        result = asyncio.run(download(self.series, torrent, context))

        self.assertEqual(result.action, "done")
        self.assertEqual(result.next_step, "check_media")
        self.assertEqual(torrent["progress"], 1.0)
        self.assertEqual(len(torrent["files"]), 1)
        self.assertEqual(torrent["step"], "check_media")
        self.assertTrue(any("download finished" in line for line in self.series.recent))


if __name__ == "__main__":
    unittest.main()
