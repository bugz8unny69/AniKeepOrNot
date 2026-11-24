"""Tests for SJ-8: worker log formatting for series steps."""
import asyncio
import logging
import sqlite3
import unittest
from types import SimpleNamespace

from src.anime.series_job.store import wake, add_torrent, get_series, update_series
from src.anime.series_job.worker import run_series_attempt
from src.anime.storage.sqlite import initialize


class TestSeriesLogging(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def test_worker_log_formatting(self):
        aid = 16999
        title = "Cyberpunk Edgerunners"
        wake(self.db, aid, title)
        add_torrent(self.db, aid, {
            "infohash": "a" * 40,
            "release_name": "[Group] Cyberpunk - 05",
            "episodes": ["S01E05"],
            "step": "download",
            "state": "waiting",
            "progress": 0.0,
        })

        class MockQB:
            def __init__(self):
                self.progress = 0.53
            async def add(self, t): pass
            async def get_status(self, h):
                return {"progress": self.progress, "files": [{"name": "Cyberpunk - 05.mkv", "size": 1000}]}

        qb = MockQB()
        context = SimpleNamespace(
            db=self.db,
            qb_client=qb,
            configuration=None,
            cancelled=lambda: False,
            now=1000,
        )

        with self.assertLogs("akon.series", level="INFO") as logs:
            # First attempt: download starts
            worked = asyncio.run(run_series_attempt(self.db, context, now=1000))
            self.assertTrue(worked)

        output = "\n".join(logs.output)
        self.assertIn(f"series {aid} {title} · work started", output)
        self.assertIn(f"series {aid} {title} · S01E05 [Group] Cyberpunk - 05 · download started", output)

        # Second attempt: download status polled at wake_at_ms (6000), 53%
        with self.assertLogs("akon.series", level="INFO") as logs_prog:
            worked_prog = asyncio.run(run_series_attempt(self.db, context, now=6000))
            self.assertTrue(worked_prog)

        output_prog = "\n".join(logs_prog.output)
        self.assertIn(f"series {aid} {title} · S01E05 [Group] Cyberpunk - 05 · downloading, 53%", output_prog)

        # Repeated wait with same progress (53%) at next wake (11000): logs NOTHING about downloading
        with self.assertLogs("akon.series", level="INFO") as logs2:
            worked2 = asyncio.run(run_series_attempt(self.db, context, now=11000))
            self.assertTrue(worked2)

        output2 = "\n".join(logs2.output)
        self.assertNotIn("downloading, 53%", output2)

        # Download finishes at next wake (16000)
        qb.progress = 1.0
        with self.assertLogs("akon.series", level="INFO") as logs3:
            worked3 = asyncio.run(run_series_attempt(self.db, context, now=16000))
            self.assertTrue(worked3)

        output3 = "\n".join(logs3.output)
        self.assertIn(f"series {aid} {title} · S01E05 [Group] Cyberpunk - 05 · download finished", output3)
