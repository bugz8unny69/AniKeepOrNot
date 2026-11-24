"""Tests for SJ-4e: transcode step."""
import asyncio
import unittest
from types import SimpleNamespace

from src.anime.series_job.steps.transcode import transcode
from src.anime.series_job.store import SeriesRow
from src.anime.services.transcode import TranscodeWaiting, TranscodeFailed


class TranscodeStepTests(unittest.TestCase):
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
            "step": "transcode",
            "state": "working",
        }

    def test_transcode_off_skips_to_stage(self):
        class Config:
            def value(self, path):
                if path == "transcode.mode":
                    return "off"
                raise KeyError(path)

        context = SimpleNamespace(configuration=Config(), now=1000)
        result = asyncio.run(transcode(self.series, self.torrent, context))

        self.assertEqual(result.action, "done")
        self.assertEqual(result.next_step, "stage")
        self.assertEqual(self.torrent["step"], "stage")

    def test_transcode_waiting(self):
        class Config:
            def value(self, path):
                if path == "transcode.mode":
                    return "local"
                raise KeyError(path)

        async def waiting_transcoder(s, t, c):
            raise TranscodeWaiting("GPU busy")

        context = SimpleNamespace(configuration=Config(), transcoder=waiting_transcoder, now=1000)
        result = asyncio.run(transcode(self.series, self.torrent, context))

        self.assertEqual(result.action, "wait")
        self.assertEqual(result.wake_at_ms, 11000)
        self.assertEqual(result.why, "TRANSCODE_WAITING")

    def test_transcode_success(self):
        class Config:
            def value(self, path):
                if path == "transcode.mode":
                    return "local"
                raise KeyError(path)

        async def ok_transcoder(s, t, c):
            return "done"

        context = SimpleNamespace(configuration=Config(), transcoder=ok_transcoder, now=1000)
        result = asyncio.run(transcode(self.series, self.torrent, context))

        self.assertEqual(result.action, "done")
        self.assertEqual(result.next_step, "stage")
        self.assertEqual(self.torrent["step"], "stage")
        self.assertTrue(any("transcode finished" in line for line in self.series.recent))

    def test_transcode_failure_drops_torrent(self):
        class Config:
            def value(self, path):
                if path == "transcode.mode":
                    return "local"
                raise KeyError(path)

        async def failing_transcoder(s, t, c):
            raise TranscodeFailed("ffmpeg failed")

        context = SimpleNamespace(configuration=Config(), transcoder=failing_transcoder, now=1000)
        result = asyncio.run(transcode(self.series, self.torrent, context))

        self.assertEqual(result.action, "drop")
        self.assertEqual(result.why, "TRANSCODE_FAILED")


if __name__ == "__main__":
    unittest.main()
