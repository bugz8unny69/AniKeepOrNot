"""adm series --id <anidb_id> [--keep|--reject|--search|--retry|--cancel], and adm running."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from src.anime.configuration import ConfigurationStore
from src.anime.series_job.store import wake, add_torrent, get_series, update_series
from src.anime.storage.sqlite import initialize
from src.anime.api.adm_commands import running as adm_running, series as adm_series


class TestAdmSeries(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.db_path = str(self.config_dir / "test.sqlite")
        ConfigurationStore(self.config_dir).initialize({'storage': {'database_path': self.db_path}})

        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA foreign_keys=ON")
        initialize(db)
        db.close()

    def tearDown(self):
        self.temp_dir.cleanup()


    def test_adm_running(self):
        db = sqlite3.connect(self.db_path)
        wake(db, 16999, "Cyberpunk Edgerunners")
        add_torrent(db, 16999, {"infohash": "b" * 40, "release_name": "Working Release", "step": "download"})
        series = get_series(db, 16999)
        series.torrents[0]["state"] = "working"
        series.torrents[0]["progress"] = 0.45
        update_series(db, series)
        db.close()

        # Plain text
        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_running.main(["--config-dir", str(self.config_dir)], prog="adm running")
        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("Cyberpunk Edgerunners", output)
        self.assertIn("Working Release", output)
        self.assertIn("Working Release: download, 45%", output)

        # JSON
        out_json = io.StringIO()
        with redirect_stdout(out_json):
            code = adm_running.main(["--json", "--config-dir", str(self.config_dir)], prog="adm running")
        self.assertEqual(code, 0)
        data = json.loads(out_json.getvalue())
        self.assertEqual(len(data["series"]), 1)
        self.assertEqual(data["series"][0]["anidb_id"], 16999)

    def test_adm_keep(self):
        db = sqlite3.connect(self.db_path)
        wake(db, 17001, "Keep Show")
        db.close()

        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_series.main(["--id", "17001", "--keep", "--config-dir", str(self.config_dir)], prog="adm series")
        self.assertEqual(code, 0)
        self.assertIn("Keep requested", out.getvalue())

        db = sqlite3.connect(self.db_path)
        s = get_series(db, 17001)
        self.assertIn("keep", s.work)
        db.close()

    def test_adm_reject(self):
        db = sqlite3.connect(self.db_path)
        wake(db, 17002, "Reject Show")
        db.close()

        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_series.main(["--id", "17002", "--reject", "--reason", "BAD_ENCODE", "--config-dir", str(self.config_dir)], prog="adm series")
        self.assertEqual(code, 0)
        self.assertIn("Reject requested", out.getvalue())

        db = sqlite3.connect(self.db_path)
        s = get_series(db, 17002)
        self.assertIn("reject", s.work)
        db.close()

    def test_adm_search(self):
        db = sqlite3.connect(self.db_path)
        wake(db, 17003, "Search Show")
        db.close()

        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_series.main(["--id", "17003", "--search", "--config-dir", str(self.config_dir)], prog="adm series")
        self.assertEqual(code, 0)
        self.assertIn("search triggered", out.getvalue())

    def test_adm_retry(self):
        db = sqlite3.connect(self.db_path)
        wake(db, 17004, "Retry Show")
        add_torrent(db, 17004, {"infohash": "c" * 40, "release_name": "Dropped"})
        series = get_series(db, 17004)
        series.torrents[0]["state"] = "dropped"
        update_series(db, series)
        db.close()

        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_series.main(["--id", "17004", "--retry", "--config-dir", str(self.config_dir)], prog="adm series")
        self.assertEqual(code, 0)
        self.assertIn("retried", out.getvalue())

        db = sqlite3.connect(self.db_path)
        s = get_series(db, 17004)
        self.assertEqual(s.torrents[0]["state"], "waiting")
        db.close()

    def test_adm_series_shows_a_series_without_an_action(self):
        db = sqlite3.connect(self.db_path)
        wake(db, 17005, "Shown Show")
        add_torrent(db, 17005, {"infohash": "d" * 40, "release_name": "[G] Shown Show - 01"})
        db.close()
        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_series.main(["--id", "17005", "--config-dir", str(self.config_dir)], prog="adm series")
        self.assertEqual(code, 0)
        self.assertIn("Series 17005 Shown Show", out.getvalue())
        self.assertIn("[G] Shown Show - 01", out.getvalue())

    def test_adm_series_cancel(self):
        db = sqlite3.connect(self.db_path)
        wake(db, 17006, "Cancel Show")
        db.close()
        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_series.main(["--id", "17006", "--cancel", "--config-dir", str(self.config_dir)], prog="adm series")
        self.assertEqual(code, 0)
        db = sqlite3.connect(self.db_path)
        self.assertIn("cancel", get_series(db, 17006).work)
        db.close()

    def test_one_action_at_a_time_and_reject_options_only_with_reject(self):
        for argv in (["--keep", "--retry"], ["--retry", "--note", "x"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                adm_series.main(["--id", "1", *argv, "--config-dir", str(self.config_dir)], prog="adm series")

    def test_a_series_that_is_not_there(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = adm_series.main(["--id", "99999", "--config-dir", str(self.config_dir)], prog="adm series")
        self.assertEqual(code, 1)
        self.assertIn("has no series", out.getvalue())

    def test_a_keep_leaves_running_once_sonarr_has_every_episode(self):
        # Owner, 2026-11-28: after the import only follow-ups are left (Shoko, dubs, Jellyfin,
        # cleanup) and webhooks for the series are refused: nothing there to watch.
        from src.anime.series_job.views import running
        db = sqlite3.connect(self.db_path)
        wake(db, 19881, "The Laid-Off Cheat-Granting Mage")
        series = get_series(db, 19881)
        listed = []
        for step, stopped in (("sonarr_import", None), ("shoko", None), ("dubs", None),
                              ("shoko", "KEEP_IMPORT_REVIEW: stopped"), ("cleanup", None)):
            series.work["keep"] = {"step": step, "requested_at_ms": 1, **({"stopped": stopped} if stopped else {})}
            update_series(db, series)
            listed.append(bool(running(db)["series"]))
        db.close()
        self.assertEqual(listed, [True, False, False, True, False])     # A stopped Keep still shows.

