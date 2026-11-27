"""SJ-8: the series log names the series, episode and torrent; a repeated wait logs nothing."""
import asyncio
import sqlite3
import unittest

from src.anime.series_job.store import add_torrent, wake
from src.anime.series_job.worker import run_series_attempt
from src.anime.storage.sqlite import initialize
from src.anime.tests.test_series_job_worker import SeriesWorkerEndToEndTests
from src.anime.tests.test_series_steps import HASH, RAW


class TestSeriesLogging(unittest.TestCase):
    context = SeriesWorkerEndToEndTests.context

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        initialize(self.db)
        self.addCleanup(self.db.close)

    def test_worker_log_formatting(self):
        wake(self.db, 16999, "Cyberpunk Edgerunners")
        add_torrent(self.db, 16999, {"infohash": HASH, "release_name": "[Group] Cyberpunk - 05",
                                     "episodes": ["S01E05"], "step": "download", "state": "waiting",
                                     "files": [{"index": 1, "path": "Show/Show - 05.mkv", "size": 100,
                                                "episode": "S01E05"}]})
        self.db.execute("UPDATE series SET torrents_json=json_set(torrents_json,'$[0].qb',json(?))",
                        ('{"marker": "%s", "added": "sent", "selected": true, "started": true}' % ("a" * 32),))
        context = self.context(None)
        self.qb.add_series_torrent(HASH, "a" * 32, raw=RAW)
        self.qb.select_series_files(HASH, "a" * 32, {1})
        self.qb.torrent["state"] = "downloading"
        self.qb.torrent["progress"][1] = 0.53
        label = "series 16999 Cyberpunk Edgerunners · S01E05 [Group] Cyberpunk - 05"

        with self.assertLogs("akon.series", level="INFO") as logs:
            self.assertTrue(asyncio.run(run_series_attempt(self.db, context, now=1000)))
        output = "\n".join(logs.output)
        self.assertIn("series 16999 Cyberpunk Edgerunners · work started", output)
        self.assertIn(f"{label} · downloading, 53%", output)

        with self.assertNoLogs("akon.series", level="INFO"):      # Same progress: nothing.
            self.assertTrue(asyncio.run(run_series_attempt(self.db, context, now=6000)))

        self.qb.torrent["progress"][1] = 1
        with self.assertLogs("akon.series", level="INFO") as logs:
            self.assertTrue(asyncio.run(run_series_attempt(self.db, context, now=11000)))
        self.assertIn(f"{label} · download finished", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
