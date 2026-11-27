import asyncio
import sqlite3
import unittest
from types import SimpleNamespace

from src.anime.series_job.actions import (
    keep_series, keep_series_sync,
    reject_series, reject_series_sync,
    search_series, search_series_sync,
    retry_series, retry_series_sync,
    cancel_series, cancel_series_sync,
)
from src.anime.series_job.store import wake, add_torrent, get_series, update_series
from src.anime.storage.sqlite import initialize
from src.anime.storage.review_decisions import decision


class FakeQBClient:
    def __init__(self):
        self.deleted = []

    async def delete_torrent(self, infohash: str, delete_files: bool = True):
        self.deleted.append((infohash, delete_files))


class TestSeriesActions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    async def test_keep_is_recorded_and_the_worker_does_it(self):
        aid = 16999
        wake(self.db, aid, "Cyberpunk Edgerunners")
        add_torrent(self.db, aid, {"infohash": "a" * 40, "release_name": "Release 1"})
        res = await keep_series(self.db, aid)
        self.assertEqual(res["state"], "WORKING")
        updated = get_series(self.db, aid)
        self.assertEqual((updated.work["keep"]["step"], updated.state, updated.lease_token), ("cancel", "WORKING", None))
        self.assertEqual(decision(self.db, aid), "ACCEPT")          # Keeping already counts as kept.
        self.assertEqual((await keep_series(self.db, aid))["state"], "WORKING")   # A second Keep is a no-op.

    async def test_keep_series_not_found_or_rejected(self):
        with self.assertRaises(ValueError):
            await keep_series(self.db, 99999)

        aid = 17000
        wake(self.db, aid, "Rejected Show")
        await reject_series(self.db, aid)
        with self.assertRaises(ValueError):
            await keep_series(self.db, aid)

    async def test_reject_removes_akons_torrents_through_the_worker(self):
        import asyncio as _asyncio
        from src.anime.series_job.worker import run_series_attempt
        from src.anime.tests.test_series_job_worker import SeriesWorkerEndToEndTests
        aid = 17001
        wake(self.db, aid, "Some Anime")
        add_torrent(self.db, aid, {"infohash": "c" * 40, "release_name": "Release 3"})
        series = get_series(self.db, aid)
        series.episodes["S01E01"] = {"state": "staged", "torrent": "c" * 40, "file": "ep1.mkv"}
        series.torrents[0].update(state="working", qb={"marker": "a" * 32, "added": "sent"})
        update_series(self.db, series)
        res = await reject_series(self.db, aid, reason="BAD_QUALITY", note_text="Artifacts present")
        self.assertEqual(res["state"], "WORKING")
        self.assertEqual(decision(self.db, aid), "REJECT")
        holder = SeriesWorkerEndToEndTests()
        holder.db, holder.addCleanup = self.db, self.addCleanup
        context = holder.context(None)
        context.configuration.values["storage.user_evaluation_root"] = "/nonexistent-staging"
        self.assertTrue(await run_series_attempt(self.db, context, now=10**13))
        updated = get_series(self.db, aid)
        self.assertEqual((updated.state, updated.torrents[0]["state"], updated.episodes["S01E01"]["state"]),
                         ("REJECTED", "dropped", "dropped"))
        self.assertEqual(holder.qb.calls, [("remove",)])

        # Rejecting already kept series raises error
        aid2 = 17002
        wake(self.db, aid2, "Kept Show")
        await keep_series(self.db, aid2)
        with self.assertRaises(ValueError):
            await reject_series(self.db, aid2)

    async def test_search_makes_the_series_search_due_now(self):
        aid = 17003
        wake(self.db, aid, "Searchable Show")
        res = await search_series(self.db, aid, now=5000)
        self.assertIn("search triggered", res["message"])
        updated = get_series(self.db, aid)
        self.assertEqual((updated.state, updated.work["search"]["due_ms"]), ("WORKING", 5000))

    async def test_retry_series(self):
        aid = 17004
        wake(self.db, aid, "Retry Show")
        add_torrent(self.db, aid, {"infohash": "e" * 40, "release_name": "Dropped Release"})

        series = get_series(self.db, aid)
        series.torrents[0]["state"] = "dropped"
        series.torrents[0]["why"] = "TIMEOUT"
        series.state = "ASLEEP"
        update_series(self.db, series)

        res = await retry_series(self.db, aid)
        self.assertEqual(res["torrents_retried"], 1)

        updated = get_series(self.db, aid)
        self.assertEqual(updated.state, "WORKING")
        self.assertEqual(updated.torrents[0]["state"], "waiting")
        self.assertEqual(updated.torrents[0]["step"], "get_metadata")
        self.assertIsNone(updated.torrents[0]["why"])
        self.assertTrue(any("retried" in line for line in updated.recent))

    async def test_cancel_drops_the_working_torrents_through_the_worker(self):
        from src.anime.series_job.worker import run_series_attempt
        from src.anime.tests.test_series_job_worker import SeriesWorkerEndToEndTests
        aid = 17005
        wake(self.db, aid, "Cancel Show")
        add_torrent(self.db, aid, {"infohash": "f" * 40, "release_name": "Working Release"})
        series = get_series(self.db, aid)
        series.torrents[0].update(state="working", qb={"marker": "a" * 32, "added": "sent"})
        update_series(self.db, series)
        self.assertEqual((await cancel_series(self.db, aid))["state"], "WORKING")
        holder = SeriesWorkerEndToEndTests()
        holder.db, holder.addCleanup = self.db, self.addCleanup
        self.assertTrue(await run_series_attempt(self.db, holder.context(None), now=10**13))
        updated = get_series(self.db, aid)
        self.assertEqual((updated.state, updated.torrents[0]["state"], updated.torrents[0]["why"]),
                         ("ASLEEP", "dropped", "canceled by you"))
        self.assertEqual(holder.qb.calls, [("remove",)])

    def test_sync_wrappers(self):
        aid = 17006
        wake(self.db, aid, "Sync Show")
        add_torrent(self.db, aid, {"infohash": "1" * 40, "release_name": "R1"})

        res_keep = keep_series_sync(self.db, aid)
        self.assertIn("Keep requested", res_keep["message"])

        aid2 = 17007
        wake(self.db, aid2, "Sync Show 2")
        res_reject = reject_series_sync(self.db, aid2)
        self.assertIn("Reject requested", res_reject["message"])

        aid3 = 17008
        wake(self.db, aid3, "Sync Show 3")
        res_search = search_series_sync(self.db, aid3)
        self.assertIn("search triggered", res_search["message"])

        res_retry = retry_series_sync(self.db, aid3)
        self.assertIn("retried", res_retry["message"])

        res_cancel = cancel_series_sync(self.db, aid3)
        self.assertEqual(res_cancel["state"], "WORKING")
