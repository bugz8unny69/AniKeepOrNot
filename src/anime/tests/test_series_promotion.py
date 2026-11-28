"""Owner, 2026-11-28: promote a series ("I want to watch this first", or after a retry)."""
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.anime.series_job.actions import SeriesDecided, promote_series_sync, unpromote_series_sync
from src.anime.series_job.store import claim, get_series, promoted_at, save, update_series, wake
from src.anime.storage.sqlite import initialize


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        initialize(self.db)
        for aid in (1, 2, 3):
            wake(self.db, aid, f"Show {aid}", now=aid)          # 1 was waiting longest: it comes first.

    def tearDown(self):
        self.db.close()

    def claimed(self, now=10):
        found = claim(self.db, now)
        return found[1].anidb_id if found else None

    def test_a_promoted_series_is_taken_first_the_latest_promotion_first(self):
        promote_series_sync(self.db, 3, now=5)
        promote_series_sync(self.db, 2, now=6)
        self.assertEqual([self.claimed(), self.claimed(), self.claimed()], [2, 3, 1])

    def test_promoting_a_sleeping_series_wakes_it(self):
        series = get_series(self.db, 3)
        series.state, series.wake_at_ms = "ASLEEP", 10 ** 15
        update_series(self.db, series, now=4)
        promote_series_sync(self.db, 3, now=5)
        self.assertEqual(self.claimed(), 3)

    def test_the_workers_save_keeps_a_promotion_made_while_it_holds_the_series(self):
        held, series = claim(self.db, 10)
        promote_series_sync(self.db, series.anidb_id, now=11)        # From the dashboard, meanwhile.
        series.state = "ASLEEP"
        save(self.db, held, series, now=12)                          # The worker's own save.
        self.assertEqual(promoted_at(self.db, series.anidb_id), 11)

    def test_putting_it_back_and_decided_series(self):
        promote_series_sync(self.db, 3, now=5)
        unpromote_series_sync(self.db, 3, now=6)
        self.assertIsNone(promoted_at(self.db, 3))
        self.assertEqual(self.claimed(), 1)
        series = get_series(self.db, 2)
        series.state = "KEPT"
        update_series(self.db, series, now=7)
        with self.assertRaises(SeriesDecided):
            promote_series_sync(self.db, 2, now=8)


class AdmPromoteTests(unittest.TestCase):
    def test_retry_then_promote_in_one_command(self):
        from src.anime.api.adm_commands import series as adm_series
        from src.anime.configuration import ConfigurationStore
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        ConfigurationStore(root).initialize({"storage": {"database_path": str(root / "a.sqlite")}})
        db = sqlite3.connect(root / "a.sqlite")
        initialize(db)
        wake(db, 69, "One Piece", now=1)
        db.commit()
        db.close()
        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_series.main(["--id", "69", "--retry", "--promote", "--config-dir", str(root)], prog="adm series")
        self.assertEqual(code, 0)
        self.assertIn("retried", out.getvalue())
        self.assertIn("worked on first", out.getvalue())
        db = sqlite3.connect(root / "a.sqlite")
        self.assertIsNotNone(promoted_at(db, 69))
        db.close()
