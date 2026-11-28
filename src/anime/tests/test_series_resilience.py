"""Owner, 2026-11-27: a program error stops the series; an outage never does.

Every service client raises its own typed error (tested with each step). These
tests cover the net under them: an outage that reaches the series loop raw makes
the torrent or the series wait and try again, and a program error in Search,
Keep, Reject or Cancel stops that one series (not the worker) until Retry.
"""
import asyncio
import errno
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.anime.series_job.context import SeriesContext
from src.anime.series_job.steps.common import SERVICE_WAIT_MS, outage
from src.anime.series_job.store import add_torrent, get_series, update_series, wake
from src.anime.series_job.worker import run_series_attempt
from src.anime.storage.sqlite import initialize

AID = 20100


def locked():
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorname = "SQLITE_BUSY"
    return exc


class SeriesResilienceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)
        wake(self.db, AID, "Resilient Show", now=1000)

    def tearDown(self):
        self.db.close()

    def context(self, verify=None):
        from src.anime.tests.test_series_steps import Configuration, FakeQb
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        qb = FakeQb()
        return SeriesContext(verify=verify, torrents=type("T", (), {"client": lambda s, c, m: qb})(),
                             cancelled=threading.Event(), db=self.db,
                             configuration=Configuration(str(Path(temp.name) / "akon.sqlite")))

    def notifications(self):
        return self.db.execute("SELECT type,title,message FROM notifications WHERE anidb_id=?", (AID,)).fetchall()

    def step_raising(self, error):
        """One attempt in which the release check raises error; returns the torrent after it."""
        from src.anime.series_job.torrents import save_metainfo
        from src.anime.tests.test_series_steps import HASH, RAW
        name = "[G] Resilient Show - 01"
        add_torrent(self.db, AID, {"infohash": HASH, "release_name": name, "step": "check_release",
                                   "raw_body": json.dumps({"release_name": name, "torrent_infohash": HASH}),
                                   "identity": {"anime": {"anidb_id": AID, "tvdb_series_id": 393400}}}, now=1000)

        async def verify(source, configuration, cancelled):
            raise error
        context = self.context(verify)
        save_metainfo(context.configuration, HASH, RAW)
        with self.assertLogs("akon.series", level="WARNING") as self.logs:
            self.assertTrue(asyncio.run(run_series_attempt(self.db, context, now=5000)))
        return get_series(self.db, AID).torrents[0]

    def assert_waits(self, error):
        torrent = self.step_raising(error)
        self.assertEqual((torrent["state"], torrent["step"]), ("working", "check_release"))
        self.assertEqual(self.db.execute("SELECT state,wake_at_ms FROM series WHERE anidb_id=?", (AID,)).fetchone(),
                         ("ASLEEP", 5000 + SERVICE_WAIT_MS))
        self.assertEqual(self.notifications(), [])
        self.assertTrue(all("WARNING" in line for line in self.logs.output))

    # A step: an outage no client typed waits; nothing stops, nothing is notified.

    def test_a_reset_connection_in_a_step_waits(self):
        self.assert_waits(ConnectionResetError("reset by peer"))

    def test_a_timeout_in_a_step_waits(self):
        self.assert_waits(TimeoutError())

    def test_a_full_disk_in_a_step_waits(self):
        self.assert_waits(OSError(errno.ENOSPC, "No space left on device"))

    def test_a_gone_network_share_in_a_step_waits(self):
        self.assert_waits(OSError(errno.ESTALE, "Stale file handle", "/mnt/staging/x.mkv"))

    def test_a_locked_database_in_a_step_waits(self):
        self.assert_waits(locked())

    def test_a_bad_file_descriptor_in_a_step_is_a_program_error(self):
        torrent = self.step_raising(OSError(errno.EBADF, "Bad file descriptor"))
        self.assertEqual(torrent["state"], "stopped")
        self.assertEqual(len(self.notifications()), 1)

    def test_a_broken_query_is_a_program_error(self):
        self.assertIsNone(outage(sqlite3.OperationalError("no such table: jobs")))
        self.assertIsNone(outage(sqlite3.IntegrityError("UNIQUE constraint failed")))
        self.assertIsNone(outage(KeyError("missing")))

    def test_a_stopped_step_keeps_the_torrent_and_its_files_and_retry_resumes_it(self):
        """Owner, 2026-11-27: a good release is never thrown away for a problem that is not its own."""
        from src.anime.series_job.actions import retry_series_sync
        from src.anime.series_job.steps.common import StepResult
        from src.anime.series_job import worker as series_worker
        add_torrent(self.db, AID, {"infohash": "9" * 40, "release_name": "[G] Show - 01", "step": "stage",
                                   "state": "working", "qb": {"marker": "b" * 32, "added": "sent"}}, now=1000)

        async def stopping(series, torrent, context):
            return StepResult.stop("STAGING_REVIEW: a file staging did not write")
        context = self.context()
        with patch.dict(series_worker.STEP_HANDLERS, {"stage": stopping}), \
                patch.object(series_worker, "drop_torrent") as dropped, \
                self.assertLogs("akon.series", level="WARNING"):
            asyncio.run(run_series_attempt(self.db, context, now=5000))
        dropped.assert_not_called()                                    # Nothing removed.
        torrent = get_series(self.db, AID).torrents[0]
        self.assertEqual((torrent["state"], torrent["step"]), ("stopped", "stage"))
        self.assertIn("STAGING_REVIEW", torrent["why"])
        [(kind, title, message)] = self.notifications()
        self.assertTrue(title.startswith("Stopped: [G] Show - 01"))
        self.assertIn("Nothing was removed", message)
        retry_series_sync(self.db, AID, now=6000)
        self.assertEqual(get_series(self.db, AID).torrents[0]["state"], "working")   # Resumes at stage.

    # The series: Search, Keep, Reject and Cancel run outside the steps.

    def search_raising(self, error):
        series = get_series(self.db, AID)
        series.work["search"] = {"wanted": [3], "due_ms": 1000}
        update_series(self.db, series, now=1000)
        with patch("src.anime.series_job.search.run", side_effect=error), \
                patch("src.anime.acquisition.prowlarr_search.Client.from_context", return_value=object()), \
                self.assertLogs("akon.series", level="WARNING") as self.logs:
            self.assertTrue(asyncio.run(run_series_attempt(self.db, self.context(), now=5000)))
        return get_series(self.db, AID)

    def test_a_program_error_in_the_search_stops_the_series_not_the_worker(self):
        series = self.search_raising(KeyError("missing"))
        self.assertEqual((series.state, series.wake_at_ms), ("ASLEEP", None))
        self.assertTrue(series.work["stopped"].startswith("program error at "))
        self.assertIn("(KeyError)", series.work["stopped"])
        self.assertTrue(any("Traceback" in line for line in self.logs.output))
        [(kind, title, message)] = self.notifications()
        self.assertEqual(kind, "JOB_FAILED")
        self.assertIn("Resilient Show", title)
        self.assertIn("program bug", message)
        # Nothing is due: the stopped series is not taken again until Retry.
        self.assertFalse(asyncio.run(run_series_attempt(self.db, self.context(), now=10 ** 9)))

    def test_retry_clears_a_stopped_series(self):
        from src.anime.series_job.actions import retry_series_sync
        self.search_raising(KeyError("missing"))
        retry_series_sync(self.db, AID, now=6000)
        series = get_series(self.db, AID)
        self.assertNotIn("stopped", series.work)
        self.assertEqual(series.state, "WORKING")

    def test_an_outage_in_the_search_makes_the_series_wait(self):
        series = self.search_raising(ConnectionResetError("reset by peer"))
        self.assertEqual((series.state, series.wake_at_ms), ("ASLEEP", 5000 + SERVICE_WAIT_MS))
        self.assertNotIn("stopped", series.work)
        self.assertEqual(self.notifications(), [])

    def test_a_program_error_in_keep_stops_the_series(self):
        series = get_series(self.db, AID)
        series.work["keep"] = {"step": "sonarr_series", "requested_at_ms": 1000}
        update_series(self.db, series, now=1000)
        with patch("src.anime.series_job.keep.HANDLERS", {"sonarr_series": lambda *a: {}["missing"]}), \
                self.assertLogs("akon.series", level="ERROR"):
            self.assertTrue(asyncio.run(run_series_attempt(self.db, self.context(), now=5000)))
        series = get_series(self.db, AID)
        self.assertEqual((series.state, series.wake_at_ms), ("ASLEEP", None))
        self.assertIn("(KeyError)", series.work["stopped"])
        self.assertEqual(series.work["keep"]["step"], "sonarr_series")     # Retry resumes Keep there.


class WorkerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Taking or saving a series failing with a program error never stops the worker."""

    async def test_a_program_error_outside_any_series_is_logged_and_the_worker_goes_on(self):
        from types import SimpleNamespace
        from src.anime.configuration import ConfigurationStore
        from src.anime.worker import Worker
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store = ConfigurationStore(root)
        startup = store.initialize({"storage": {"database_path": str(root / "q.sqlite")},
                                    "worker": {"concurrency": 1}}).snapshot
        with sqlite3.connect(root / "q.sqlite") as db:
            initialize(db)
        worker = Worker(store, startup, context_factory=lambda secrets, cancelled: SimpleNamespace())
        with patch("src.anime.series_job.worker.run_series_attempt", side_effect=KeyError("missing")), \
                patch("src.anime.worker.PROGRAM_ERROR_WAIT_S", 0), \
                self.assertLogs(worker.logger, level="ERROR") as logs:
            self.assertFalse(await worker.attempt())
        self.assertTrue(any("program error" in line and "KeyError" in line for line in logs.output))


class WorkerLanesTests(unittest.IsolatedAsyncioTestCase):
    """Owner, 2026-11-28: worker.concurrency series at a time, each attempt with its own context."""

    async def test_two_lanes_work_on_two_series_at_the_same_time(self):
        from types import SimpleNamespace
        from src.anime.configuration import ConfigurationStore
        from src.anime.worker import Worker
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store = ConfigurationStore(root)
        startup = store.initialize({"storage": {"database_path": str(root / "q.sqlite")},
                                    "worker": {"concurrency": 2}}).snapshot
        with sqlite3.connect(root / "q.sqlite") as db:
            initialize(db)
        worker = Worker(store, startup, context_factory=lambda secrets, cancelled: SimpleNamespace(cancelled=cancelled))
        active, most, contexts = [0], [0], []

        async def attempt(db, context):
            contexts.append(context)
            active[0] += 1
            most[0] = max(most[0], active[0])
            await asyncio.sleep(0.05)               # Long enough for the other lane to start.
            active[0] -= 1
            if len(contexts) >= 4:
                worker.stop()
            return True
        with patch("src.anime.series_job.worker.run_series_attempt", side_effect=attempt):
            await asyncio.wait_for(worker.run(), timeout=10)
        self.assertEqual(most[0], 2)                                   # Two series at once.
        self.assertEqual(len({id(c) for c in contexts}), len(contexts))   # Each attempt its own context.
        self.assertTrue(all(c.db is not None for c in contexts))

    def test_the_default_is_two_series_and_one_torrent_each(self):
        from src.anime.configuration_schema import FIELDS
        self.assertEqual((FIELDS["worker.concurrency"].default, FIELDS["series.torrents_at_once"].default), (2, 1))
