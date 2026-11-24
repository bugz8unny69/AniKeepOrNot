"""Tests for SJ-4d: check_media step."""
import asyncio
import unittest
from types import SimpleNamespace

from src.anime.acquisition.media import MediaInvalid
from src.anime.series_job.steps.check_media import check_media
from src.anime.series_job.store import SeriesRow


class CheckMediaStepTests(unittest.TestCase):
    def setUp(self):
        self.series = SeriesRow(
            anidb_id=16999,
            title="Cyberpunk: Edgerunners",
            state="WORKING",
            wake_at_ms=None,
            torrents_at_once=1,
            episodes={"S01E05": {"state": "downloading", "torrent": "a" * 40, "file": None}},
        )

    def test_media_checked_successfully(self):
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[SubsPlease] Cyberpunk - 05 (1080p).mkv",
            "episodes": ["S01E05"],
            "files": [{"name": "Cyberpunk - 05.mkv", "size": 1024000}],
            "step": "check_media",
            "state": "working",
        }
        context = SimpleNamespace()
        result = asyncio.run(check_media(self.series, torrent, context))

        self.assertEqual(result.action, "done")
        self.assertEqual(result.next_step, "transcode")
        self.assertEqual(torrent["step"], "transcode")
        self.assertTrue(any("media checked" in line for line in self.series.recent))

    def test_no_video_files_dropped(self):
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[Group] Non-Video Release",
            "episodes": ["S01E05"],
            "files": [{"name": "readme.txt", "size": 100}],
            "step": "check_media",
            "state": "working",
        }
        context = SimpleNamespace()
        result = asyncio.run(check_media(self.series, torrent, context))

        self.assertEqual(result.action, "drop")
        self.assertEqual(result.why, "MEDIA_VIDEO_MISSING")

    def test_media_verifier_failure_dropped(self):
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[Corrupted] Cyberpunk - 05.mkv",
            "episodes": ["S01E05"],
            "files": [{"name": "Cyberpunk - 05.mkv", "size": 1000}],
            "step": "check_media",
            "state": "working",
        }

        async def failing_verifier(s, t, c):
            raise MediaInvalid("corrupt container")

        context = SimpleNamespace(media_verifier=failing_verifier)
        result = asyncio.run(check_media(self.series, torrent, context))

        self.assertEqual(result.action, "drop")
        self.assertEqual(result.why, "MEDIA_VERIFICATION_INVALID")


if __name__ == "__main__":
    unittest.main()
