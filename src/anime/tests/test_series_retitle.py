"""Owner, 2026-11-27: staged series are retitled in the chosen language (cutover and adm)."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.anime.acquisition.media import content_id, same_content
from src.anime.series_job import retitle
from src.anime.series_job.reject import remove_staged
from src.anime.staging import specification

TVDB = 9


def cid(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return content_id(fd, os.fstat(fd).st_size)
    finally:
        os.close(fd)


def staged_series(root):
    """Two AniDB entries (seasons) of one TVDB series, staged in the old layout: one romaji
    folder each, romaji NFO titles. Returns the series as the cutover's dicts."""
    series = []
    for aid, season, romaji in ((101, 1, "Tensei Kizoku"), (102, 2, "Tensei Kizoku Dai 2 Ki")):
        code = f"S{season:02d}E01"
        plan = specification(SimpleNamespace(options=SimpleNamespace(download_root="/d")),
                             {"path": "x.mkv", "sha256": "a" * 64, "file": [0, 0, 5, 0, 0]},
                             (aid, aid * 10, TVDB, season, 1, 777, romaji), str(root), 1,
                             folder=f"{romaji} [tvdbid-{TVDB}] [anidbid-{aid}]", series_name=romaji)
        for entry in plan["entries"]:
            path = root / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry["role"] == "media":
                path.write_bytes(b"video")
                entry["content"] = cid(path)
            else:
                path.write_text(entry["text"])
        media = plan["entries"][0]["path"]
        series.append({"anidb_id": aid, "title": romaji, "episodes": {code: {"state": "staged", "file": media}},
                       "torrents": [{"identity": {"anime": {"tvdb_series_id": TVDB}},
                                     "files": [{"episode": code, "anidb_episode_id": aid * 10,
                                                "tvdb_episode_id": 777}],
                                     "staging": {"plans": {code: plan}}}]})
    return series


class RetitleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.series = staged_series(self.root)

    def run_retitle(self):
        with patch("src.anime.anime_titles.series_title", return_value="As a Reincarnated Aristocrat"):
            found = retitle.plan(None, self.series, "english")
        done = retitle.apply(found)
        retitle.rewrite(self.series, found)
        retitle.finish(found)
        return found, done

    def test_folders_files_and_nfos_become_english_in_one_folder(self):
        found, _ = self.run_retitle()
        self.assertEqual(len(found["groups"]), 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["As a Reincarnated Aristocrat [tvdbid-9]"])
        folder = self.root / "As a Reincarnated Aristocrat [tvdbid-9]"
        self.assertEqual(sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()), [
            "Season 01/As a Reincarnated Aristocrat - S01E01.mkv", "Season 01/As a Reincarnated Aristocrat - S01E01.nfo",
            "Season 02/As a Reincarnated Aristocrat - S02E01.mkv", "Season 02/As a Reincarnated Aristocrat - S02E01.nfo",
            "tvshow.nfo"])
        self.assertIn("<title>As a Reincarnated Aristocrat</title>", (folder / "tvshow.nfo").read_text())
        self.assertIn("As a Reincarnated Aristocrat", (folder / "Season 02/As a Reincarnated Aristocrat - S02E01.nfo").read_text())
        self.assertIn('<uniqueid type="anidb" default="false">1020</uniqueid>', (folder / "Season 02/As a Reincarnated Aristocrat - S02E01.nfo").read_text())

    def test_the_rows_follow_so_keep_and_reject_still_know_the_files(self):
        self.run_retitle()
        second = self.series[1]
        staged = second["episodes"]["S02E01"]["file"]
        self.assertEqual(staged, "As a Reincarnated Aristocrat [tvdbid-9]/Season 02/As a Reincarnated Aristocrat - S02E01.mkv")
        media = second["torrents"][0]["staging"]["plans"]["S02E01"]["entries"][0]
        self.assertTrue(same_content(str(self.root), staged, 5, media["content"]))     # Keep's check.
        for item in self.series:                                                         # Reject removes all.
            self.assertEqual(remove_staged(SimpleNamespace(value=lambda key: str(self.root)), item["torrents"][0]), 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_running_it_again_changes_nothing(self):
        self.run_retitle()
        with patch("src.anime.anime_titles.series_title", return_value="As a Reincarnated Aristocrat"):
            self.assertEqual(retitle.plan(None, self.series, "english")["groups"], [])

    def test_undo_puts_everything_back(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())
        with patch("src.anime.anime_titles.series_title", return_value="As a Reincarnated Aristocrat"):
            found = retitle.plan(None, self.series, "english")
        retitle.undo(retitle.apply(found))
        after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(after, before)

    def test_a_destination_that_holds_someone_elses_file_stops_that_series(self):
        target = self.root / "As a Reincarnated Aristocrat [tvdbid-9]/Season 01"
        target.mkdir(parents=True)
        (target / "As a Reincarnated Aristocrat - S01E01.mkv").write_bytes(b"not ours")
        with patch("src.anime.anime_titles.series_title", return_value="As a Reincarnated Aristocrat"):
            found = retitle.plan(None, self.series, "english")
        self.assertEqual(found["groups"], [])
        self.assertIn("not a file AKON staged", found["skipped"][0]["why"])


class AdmRetitleTests(unittest.TestCase):
    """adm retitle: preview by default; with --apply, renames and saves the rows (worker stopped)."""

    def setUp(self):
        import sqlite3
        from src.anime.configuration import ConfigurationStore
        from src.anime.series_job.store import get_series, update_series, wake
        from src.anime.storage.sqlite import initialize
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.staging, self.config = base / "staging", base / "config"
        self.staging.mkdir()
        self.config.mkdir(mode=0o700)
        self.database = str(base / "akon.sqlite")
        ConfigurationStore(self.config).initialize({"storage": {"database_path": self.database,
                                                                "user_evaluation_root": str(self.staging)}})
        db = sqlite3.connect(self.database)
        initialize(db)
        for item in staged_series(self.staging):
            wake(db, item["anidb_id"], item["title"], now=1)
            row = get_series(db, item["anidb_id"])
            row.torrents, row.episodes = item["torrents"], item["episodes"]
            update_series(db, row, now=1)
        db.commit()
        db.close()

    def run_adm(self, *argv, worker_active=False):
        import contextlib, io, subprocess
        from src.anime.api.adm_commands import retitle as command
        out = io.StringIO()
        state = subprocess.CompletedProcess([], 0 if worker_active else 3)
        with patch("subprocess.run", return_value=state), \
                patch("src.anime.anime_titles.series_title", return_value="As a Reincarnated Aristocrat"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = command.main(["--config-dir", str(self.config), *argv], prog="adm retitle")
        return code, out.getvalue()

    def test_preview_changes_nothing_and_apply_renames_with_the_worker_stopped(self):
        import sqlite3
        from src.anime.series_job.store import get_series
        before = sorted(p.name for p in self.staging.iterdir())
        code, said = self.run_adm()
        self.assertEqual(code, 0, said)
        self.assertIn("As a Reincarnated Aristocrat [tvdbid-9]: 2 staged episode(s)", said)
        self.assertIn("Preview only", said)
        self.assertEqual(sorted(p.name for p in self.staging.iterdir()), before)
        code, said = self.run_adm("--apply", worker_active=True)
        self.assertEqual(code, 1)
        self.assertIn("stop the worker first", said)
        code, said = self.run_adm("--apply")
        self.assertEqual(code, 0, said)
        self.assertEqual(sorted(p.name for p in self.staging.iterdir()), ["As a Reincarnated Aristocrat [tvdbid-9]"])
        db = sqlite3.connect(self.database)
        self.assertEqual(get_series(db, 102).episodes["S02E01"]["file"],
                         "As a Reincarnated Aristocrat [tvdbid-9]/Season 02/As a Reincarnated Aristocrat - S02E01.mkv")
        db.close()
        code, said = self.run_adm()
        self.assertIn("already named in english", said)
