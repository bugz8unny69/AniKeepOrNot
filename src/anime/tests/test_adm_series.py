"""Tests for adm commands by AniDB ID: report intake, running, keep, reject, search, retry."""
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
from src.anime.api.adm_commands import (
    keep as adm_keep,
    reject as adm_reject,
    retry as adm_retry,
    running as adm_running,
    search as adm_search,
)


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
            code = adm_keep.main(["--id", "17001", "--config-dir", str(self.config_dir)], prog="adm keep")
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
            code = adm_reject.main(["--id", "17002", "--reason", "BAD_ENCODE", "--config-dir", str(self.config_dir)], prog="adm reject")
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
            code = adm_search.main(["--id", "17003", "--config-dir", str(self.config_dir)], prog="adm search")
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
            code = adm_retry.main(["--id", "17004", "--config-dir", str(self.config_dir)], prog="adm retry")
        self.assertEqual(code, 0)
        self.assertIn("retried", out.getvalue())

        db = sqlite3.connect(self.db_path)
        s = get_series(db, 17004)
        self.assertEqual(s.torrents[0]["state"], "waiting")
        db.close()
