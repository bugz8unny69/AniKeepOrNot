"""Tests for SJ-4g: drop_torrent."""
import asyncio
import unittest
from types import SimpleNamespace

from src.anime.series_job.steps.drop import drop_torrent
from src.anime.series_job.store import SeriesRow


class DropTorrentTests(unittest.TestCase):
    def setUp(self):
        self.series = SeriesRow(
            anidb_id=16999,
            title="Cyberpunk: Edgerunners",
            state="WORKING",
            wake_at_ms=None,
            torrents_at_once=1,
            episodes={
                "S01E01": {"state": "staged", "torrent": "keep_hash", "file": "path.mkv"},
                "S01E02": {"state": "reserved", "torrent": "bad_hash", "file": None},
                "S01E03": {"state": "downloading", "torrent": "bad_hash", "file": None},
            },
        )
        self.torrent = {
            "infohash": "bad_hash",
            "release_name": "[Bad] Cyberpunk - 02-03.mkv",
            "episodes": ["S01E02", "S01E03"],
            "step": "download",
            "state": "working",
            "submitted_at_ms": 1000,
        }

    def test_drop_removes_from_client_and_unreserves_episodes(self):
        deleted_hash = None
        files_deleted = False

        class FakeClient:
            async def delete(self, h, delete_files=False):
                nonlocal deleted_hash, files_deleted
                deleted_hash = h
                files_deleted = delete_files

        context = SimpleNamespace(qb_client=FakeClient(), now=2000)
        asyncio.run(drop_torrent(self.series, self.torrent, "BAD_CORRUPT_FILES", context))

        self.assertEqual(self.torrent["state"], "dropped")
        self.assertEqual(self.torrent["why"], "BAD_CORRUPT_FILES")
        self.assertEqual(deleted_hash, "bad_hash")
        self.assertTrue(files_deleted)

        # S01E01 was staged and owned by keep_hash: preserved!
        self.assertIn("S01E01", self.series.episodes)
        self.assertEqual(self.series.episodes["S01E01"]["state"], "staged")

        # S01E02 and S01E03 were unreserved
        self.assertNotIn("S01E02", self.series.episodes)
        self.assertNotIn("S01E03", self.series.episodes)

        self.assertTrue(any("dropped: BAD_CORRUPT_FILES" in line for line in self.series.recent))


if __name__ == "__main__":
    unittest.main()
