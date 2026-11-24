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

    async def test_keep_series_flow(self):
        aid = 16999
        wake(self.db, aid, "Cyberpunk Edgerunners")
        add_torrent(self.db, aid, {"infohash": "a" * 40, "release_name": "Release 1"})
        add_torrent(self.db, aid, {"infohash": "b" * 40, "release_name": "Release 2"})

        series = get_series(self.db, aid)
        series.episodes["S01E01"] = {"state": "staged", "torrent": "a" * 40, "file": "ep1.mkv"}
        series.torrents[0]["state"] = "done"
        series.torrents[0]["step"] = "done"
        series.torrents[1]["state"] = "waiting"
        update_series(self.db, series)

        qb = FakeQBClient()
        ctx = SimpleNamespace(qb_client=qb)

        res = await keep_series(self.db, aid, context=ctx)
        self.assertEqual(res["state"], "KEPT")

        updated = get_series(self.db, aid)
        self.assertEqual(updated.state, "KEPT")
        self.assertIsNone(updated.wake_at_ms)
        self.assertEqual(updated.episodes["S01E01"]["state"], "kept")
        self.assertEqual(updated.torrents[1]["state"], "dropped")
        self.assertIn("series kept", updated.torrents[1]["why"])
        self.assertTrue(any("kept; Sonarr owns it" in line for line in updated.recent))
        self.assertEqual(decision(self.db, aid), "ACCEPT")
        self.assertEqual(qb.deleted, [("b" * 40, True)])

        # Calling keep on already kept series is a safe no-op
        res2 = await keep_series(self.db, aid, context=ctx)
        self.assertEqual(res2["state"], "KEPT")

    async def test_keep_series_not_found_or_rejected(self):
        with self.assertRaises(ValueError):
            await keep_series(self.db, 99999)

        aid = 17000
        wake(self.db, aid, "Rejected Show")
        await reject_series(self.db, aid)
        with self.assertRaises(ValueError):
            await keep_series(self.db, aid)

    async def test_reject_series_flow(self):
        aid = 17001
        wake(self.db, aid, "Some Anime")
        add_torrent(self.db, aid, {"infohash": "c" * 40, "release_name": "Release 3"})

        series = get_series(self.db, aid)
        series.episodes["S01E01"] = {"state": "staged", "torrent": "c" * 40, "file": "ep1.mkv"}
        series.torrents[0]["state"] = "working"
        update_series(self.db, series)

        qb = FakeQBClient()
        ctx = SimpleNamespace(qb_client=qb)

        res = await reject_series(self.db, aid, context=ctx, reason="BAD_QUALITY", note_text="Artifacts present")
        self.assertEqual(res["state"], "REJECTED")

        updated = get_series(self.db, aid)
        self.assertEqual(updated.state, "REJECTED")
        self.assertIsNone(updated.wake_at_ms)
        self.assertEqual(updated.torrents[0]["state"], "dropped")
        self.assertEqual(updated.torrents[0]["why"], "BAD_QUALITY")
        self.assertEqual(updated.episodes["S01E01"]["state"], "dropped")
        self.assertTrue(any("rejected" in line for line in updated.recent))
        self.assertEqual(decision(self.db, aid), "REJECT")
        self.assertEqual(qb.deleted, [("c" * 40, True)])

        # Rejecting already kept series raises error
        aid2 = 17002
        wake(self.db, aid2, "Kept Show")
        await keep_series(self.db, aid2)
        with self.assertRaises(ValueError):
            await reject_series(self.db, aid2)

    async def test_search_series(self):
        aid = 17003
        wake(self.db, aid, "Searchable Show")

        async def fake_searcher(series, ctx):
            return [{"infohash": "d" * 40, "release_name": "Found Release"}]

        ctx = SimpleNamespace(searcher=fake_searcher)
        res = await search_series(self.db, aid, context=ctx)
        self.assertEqual(res["candidates_added"], 1)

        updated = get_series(self.db, aid)
        self.assertEqual(updated.state, "WORKING")
        self.assertEqual(len(updated.torrents), 1)
        self.assertEqual(updated.torrents[0]["infohash"], "d" * 40)
        self.assertTrue(any("search triggered" in line for line in updated.recent))

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
        self.assertEqual(updated.torrents[0]["step"], "check_release")
        self.assertIsNone(updated.torrents[0]["why"])
        self.assertTrue(any("retried" in line for line in updated.recent))

    async def test_cancel_series(self):
        aid = 17005
        wake(self.db, aid, "Cancel Show")
        add_torrent(self.db, aid, {"infohash": "f" * 40, "release_name": "Working Release"})

        series = get_series(self.db, aid)
        series.torrents[0]["state"] = "working"
        update_series(self.db, series)

        qb = FakeQBClient()
        ctx = SimpleNamespace(qb_client=qb)

        res = await cancel_series(self.db, aid, context=ctx)
        self.assertEqual(res["state"], "ASLEEP")

        updated = get_series(self.db, aid)
        self.assertEqual(updated.state, "ASLEEP")
        self.assertEqual(updated.torrents[0]["state"], "dropped")
        self.assertEqual(updated.torrents[0]["why"], "canceled by user")
        self.assertTrue(any("canceled" in line for line in updated.recent))

    def test_sync_wrappers(self):
        aid = 17006
        wake(self.db, aid, "Sync Show")
        add_torrent(self.db, aid, {"infohash": "1" * 40, "release_name": "R1"})

        res_keep = keep_series_sync(self.db, aid)
        self.assertEqual(res_keep["state"], "KEPT")

        aid2 = 17007
        wake(self.db, aid2, "Sync Show 2")
        res_reject = reject_series_sync(self.db, aid2)
        self.assertEqual(res_reject["state"], "REJECTED")

        aid3 = 17008
        wake(self.db, aid3, "Sync Show 3")
        res_search = search_series_sync(self.db, aid3)
        self.assertIn("search triggered", res_search["message"])

        res_retry = retry_series_sync(self.db, aid3)
        self.assertIn("retried", res_retry["message"])

        res_cancel = cancel_series_sync(self.db, aid3)
        self.assertEqual(res_cancel["state"], "ASLEEP")
