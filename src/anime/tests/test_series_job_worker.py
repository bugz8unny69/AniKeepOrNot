"""End-to-end tests for the series worker loop running releases through all steps to staged."""
import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace

from src.anime.release_contracts import VerifiedRelease
from src.anime.series_job.store import add_torrent, get_series, wake
from src.anime.series_job.context import SeriesContext
from src.anime.series_job.worker import run_series_attempt
from src.anime.storage.sqlite import initialize


class SeriesWorkerEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def context(self, verify):
        from src.anime.tests.test_series_steps import FakeQb, Configuration
        import tempfile, threading
        from pathlib import Path
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.qb = qb = FakeQb()
        return SeriesContext(verify=verify, torrents=type("T", (), {"client": lambda s, c, m: qb})(),
                             cancelled=threading.Event(), db=self.db,
                             configuration=Configuration(str(Path(temp.name) / "akon.sqlite")))

    def torrent(self, digest, name):
        return {"infohash": digest, "release_name": name,
                "raw_body": json.dumps({"release_name": name, "torrent_infohash": digest}),
                "identity": {"anime": {"anidb_id": 16999, "tvdb_series_id": 393400}}}

    def test_a_release_runs_from_metadata_to_a_finished_download(self):
        from src.anime.tests.test_series_steps import HASH
        wake(self.db, 16999, "Cyberpunk: Edgerunners", now=1000)
        add_torrent(self.db, 16999, self.torrent(HASH, "[G] Cyberpunk Edgerunners - 05"), now=1000)

        async def verify(source, configuration, cancelled):
            return VerifiedRelease(source, 5, 50, 1, 5, 500)
        context = self.context(verify)
        now = 1000
        for _ in range(12):
            now += 6000
            asyncio.run(run_series_attempt(self.db, context, now=now))
            if get_series(self.db, 16999).torrents[0]["step"] == "download" and self.qb.calls[-1] == ("start",):
                break
        series = get_series(self.db, 16999)
        self.assertEqual(series.episodes["S01E05"]["torrent"], HASH)
        self.assertEqual([c[0] for c in self.qb.calls], ["add", "start", "stop", "select", "start"])
        self.qb.torrent["progress"][1] = 1
        asyncio.run(run_series_attempt(self.db, context, now=now + 6000))
        self.assertEqual(get_series(self.db, 16999).torrents[0]["step"], "check_media")

    def test_unexpected_error_stops_the_torrent_keeps_its_files_and_notifies_while_the_other_continues(self):
        wake(self.db, 20000, "Multi-Torrent Show", now=1000)
        from src.anime.tests.test_series_steps import HASH, RAW
        from src.anime.series_job.torrents import save_metainfo
        bad_torrent = dict(self.torrent("1" * 40, "[Buggy] Show - 01"), step="check_release")
        good_torrent = dict(self.torrent(HASH, "[Good] Show - 02"), step="check_release")
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

        context = self.context(verify_selector)
        for digest in ("1" * 40, HASH):
            save_metainfo(context.configuration, digest, RAW)

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
        good = next(t for t in series.torrents if t["infohash"] == HASH)
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
        context = self.context(None)
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
