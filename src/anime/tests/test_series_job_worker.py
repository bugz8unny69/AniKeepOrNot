"""End-to-end tests for the series worker loop running releases through all steps to staged."""
import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace

from src.anime.release_contracts import VerifiedRelease
from src.anime.series_job.store import add_torrent, get_series, wake
from src.anime.series_job.worker import run_series_attempt
from src.anime.storage.sqlite import initialize


class SeriesWorkerEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def test_release_runs_through_every_step_to_staged(self):
        # 1. API creates / wakes series and adds torrent
        wake(self.db, 16999, "Cyberpunk: Edgerunners", now=1000)
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[SubsPlease] Cyberpunk - 05 (1080p).mkv",
            "raw_body": b'{"torrent_infohash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        }
        add_torrent(self.db, 16999, torrent, now=1000)

        # Fake services
        async def fake_verify(source, config, cancelled):
            return VerifiedRelease(source, anidb_episode_number=5, anidb_episode_id=50,
                                   tvdb_season=1, tvdb_episode=5, tvdb_episode_id=500)

        class FakeQB:
            def __init__(self):
                self.submitted = False
                self.progress = 0.0
            async def add(self, t):
                self.submitted = True
            async def get_status(self, h):
                return {"progress": self.progress, "files": [{"name": "Cyberpunk - 05.mkv", "size": 1024000}]}

        qb = FakeQB()
        context = SimpleNamespace(
            db=self.db,
            verify=fake_verify,
            qb_client=qb,
            configuration=None,
            cancelled=lambda: False,
            now=1000,
        )

        # Iteration 1: check_release -> reserve_episodes
        worked = asyncio.run(run_series_attempt(self.db, context, now=1000))
        self.assertTrue(worked)
        s1 = get_series(self.db, 16999)
        self.assertEqual(s1.torrents[0]["step"], "reserve_episodes")
        self.assertEqual(s1.torrents[0]["episodes"], ["S01E05"])

        # Iteration 2: reserve_episodes -> download
        worked = asyncio.run(run_series_attempt(self.db, context, now=1100))
        self.assertTrue(worked)
        s2 = get_series(self.db, 16999)
        self.assertEqual(s2.torrents[0]["step"], "download")
        self.assertEqual(s2.episodes["S01E05"]["state"], "reserved")

        # Iteration 3: download (initial submission) -> waiting
        worked = asyncio.run(run_series_attempt(self.db, context, now=1200))
        self.assertTrue(worked)
        s3 = get_series(self.db, 16999)
        self.assertEqual(s3.torrents[0]["step"], "download")
        self.assertEqual(s3.torrents[0]["state"], "working")
        self.assertEqual(s3.state, "ASLEEP")
        self.assertEqual(s3.wake_at_ms, 6200)

        # Iteration 4: download finishes (progress = 1.0) -> check_media
        qb.progress = 1.0
        worked = asyncio.run(run_series_attempt(self.db, context, now=6200))
        self.assertTrue(worked)
        s4 = get_series(self.db, 16999)
        self.assertEqual(s4.torrents[0]["step"], "check_media")

        # Iteration 5: check_media -> transcode
        worked = asyncio.run(run_series_attempt(self.db, context, now=6300))
        self.assertTrue(worked)
        s5 = get_series(self.db, 16999)
        self.assertEqual(s5.torrents[0]["step"], "transcode")

        # Iteration 6: transcode (off by default) -> stage
        worked = asyncio.run(run_series_attempt(self.db, context, now=6400))
        self.assertTrue(worked)
        s6 = get_series(self.db, 16999)
        self.assertEqual(s6.torrents[0]["step"], "stage")

        # Iteration 7: stage -> done!
        worked = asyncio.run(run_series_attempt(self.db, context, now=6500))
        self.assertTrue(worked)
        s7 = get_series(self.db, 16999)
        self.assertEqual(s7.torrents[0]["step"], "done")
        self.assertEqual(s7.torrents[0]["state"], "done")
        self.assertEqual(s7.episodes["S01E05"]["state"], "staged")
        self.assertEqual(s7.state, "ASLEEP")
        self.assertIsNone(s7.wake_at_ms)

        # Nothing left to work: next claim returns False
        worked_after = asyncio.run(run_series_attempt(self.db, context, now=6600))
        self.assertFalse(worked_after)

    def test_unexpected_error_stops_the_torrent_keeps_its_files_and_notifies_while_the_other_continues(self):
        wake(self.db, 20000, "Multi-Torrent Show", now=1000)
        bad_torrent = {
            "infohash": "1" * 40,
            "release_name": "[Buggy] Show - 01.mkv",
            "raw_body": b'{"release_name": "[Buggy] Show - 01.mkv"}',
        }
        good_torrent = {
            "infohash": "2" * 40,
            "release_name": "[Good] Show - 02.mkv",
            "raw_body": b'{"release_name": "[Good] Show - 02.mkv"}',
        }
        add_torrent(self.db, 20000, bad_torrent, now=1000)
        add_torrent(self.db, 20000, good_torrent, now=1000)

        # Force series to work 2 torrents at once
        self.db.execute("UPDATE series SET torrents_at_once=2 WHERE anidb_id=20000")

        async def verify_selector(source, config, cancelled):
            if "Buggy" in str(source.raw_body):
                # Raise an unexpected program error
                raise ZeroDivisionError("unexpected division bug")
            return VerifiedRelease(source, anidb_episode_number=2, anidb_episode_id=20,
                                   tvdb_season=1, tvdb_episode=2, tvdb_episode_id=200)

        context = SimpleNamespace(
            db=self.db,
            verify=verify_selector,
            configuration=None,
            cancelled=lambda: False,
            now=1000,
        )

        with self.assertLogs("akon.series", level="ERROR") as logs:
            worked = asyncio.run(run_series_attempt(self.db, context, now=1000))

        self.assertTrue(worked)
        self.assertTrue(any("unexpected division bug" in line for line in logs.output))

        series = get_series(self.db, 20000)
        # Bad torrent stopped (not dropped: its files stay) with "program error at file:line"
        bad = next(t for t in series.torrents if t["infohash"] == "1" * 40)
        self.assertEqual((bad["state"], bad["step"]), ("stopped", "check_release"))
        self.assertTrue(bad["why"].startswith("program error at test_series_job_worker.py:"))

        # Good torrent was successfully advanced!
        good = next(t for t in series.torrents if t["infohash"] == "2" * 40)
        self.assertEqual(good["step"], "reserve_episodes")
        self.assertEqual(good["episodes"], ["S01E02"])

        # Notification was sent
        notifications = self.db.execute(
            "SELECT type, severity, title, message FROM notifications WHERE anidb_id=20000"
        ).fetchall()
        self.assertEqual(len(notifications), 1)
        ntype, severity, title, msg = notifications[0]
        self.assertEqual(ntype, "JOB_FAILED")
        self.assertEqual(severity, "attention")
        self.assertIn("Multi-Torrent Show", title)
        self.assertIn("program error at", msg)


    def test_a_series_whose_torrents_all_wait_sleeps_instead_of_running_again_at_once(self):
        # Before, a wait with no wake time left the series WORKING: it re-ran ~20 times a second.
        wake(self.db, 20001, "Waiting Show", now=1000)
        add_torrent(self.db, 20001, {"infohash": "3" * 40, "release_name": "[A] Show - 01",
                                     "episodes": ["S01E01"], "step": "reserve_episodes", "state": "working"}, now=1000)
        series = get_series(self.db, 20001)
        series.episodes["S01E01"] = {"state": "downloading", "torrent": "4" * 40, "file": None}
        series.torrents.append({"infohash": "4" * 40, "release_name": "[B] Show - 01", "episodes": ["S01E01"],
                                "step": "done", "state": "done"})
        from src.anime.series_job.store import update_series
        update_series(self.db, series, now=1000)
        context = SimpleNamespace(db=self.db, configuration=None, cancelled=lambda: False, now=1000)
        self.assertTrue(asyncio.run(run_series_attempt(self.db, context, now=2000)))
        self.assertEqual(self.db.execute("SELECT state,wake_at_ms FROM series WHERE anidb_id=20001").fetchone(),
                         ("ASLEEP", None))
        self.assertFalse(asyncio.run(run_series_attempt(self.db, context, now=3000)))   # Nothing due.

    def test_retry_resumes_a_stopped_torrent_at_its_step(self):
        from src.anime.series_job.actions import retry_series_sync
        wake(self.db, 20002, "Stopped Show", now=1000)
        add_torrent(self.db, 20002, {"infohash": "5" * 40, "release_name": "[A] Show - 02", "step": "download",
                                     "state": "stopped", "why": "program error at download.py:9"}, now=1000)
        retry_series_sync(self.db, 20002, now=2000)
        torrent = get_series(self.db, 20002).torrents[0]
        self.assertEqual((torrent["step"], torrent["state"], torrent["why"]), ("download", "working", None))


if __name__ == "__main__":
    unittest.main()
