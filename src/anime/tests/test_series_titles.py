"""Owner, 2026-11-27: with English chosen, Intake and the staged NFOs show English titles from
AniDB, and every season of a TVDB series is staged in one folder (Jellyfin showed some twice)."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.anime.anime_titles import names, series_title, title_for
from src.anime.staging import series_folder, specification
from src.anime.storage.sqlite import initialize

TITLES = b"""<?xml version="1.0" encoding="UTF-8"?>
<animetitles>
 <anime aid="18026">
  <title xml:lang="x-jat" type="main">Tensei Kizoku, Kantei Skill de Nariagaru</title>
  <title xml:lang="en" type="official">As a Reincarnated Aristocrat, I'll Use My Appraisal Skill to Rise in the World</title>
 </anime>
 <anime aid="18707">
  <title xml:lang="x-jat" type="main">Tensei Kizoku, Kantei Skill de Nariagaru Dai 2 Ki</title>
  <title xml:lang="en" type="official">As a Reincarnated Aristocrat, I'll Use My Appraisal Skill to Rise in the World Season 2</title>
 </anime>
 <anime aid="1">
  <title xml:lang="x-jat" type="main">Only Romaji</title>
 </anime>
</animetitles>"""
MAPPING = b"""<?xml version="1.0" encoding="UTF-8"?>
<anime-list>
 <anime anidbid="18026" tvdbid="434704" defaulttvdbseason="1" episodeoffset=""><name>a</name></anime>
 <anime anidbid="18707" tvdbid="434704" defaulttvdbseason="2" episodeoffset=""><name>b</name></anime>
</anime-list>"""


class TitlesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.db = sqlite3.connect(self.base / "akon.sqlite")
        self.addCleanup(self.db.close)
        initialize(self.db)
        with self.db:
            for provider, key, payload in (("anidb_titles", "titles", TITLES), ("anime_lists", "mapping", MAPPING)):
                # Expired: display names may use a stale list; identification never does.
                self.db.execute("INSERT INTO provider_public_cache VALUES(?,?,1,2,?)", (provider, key, payload))
            pass                                  # The season-1 entry need not be a series AKON has.

    def test_english_comes_from_the_metadata_then_anidbs_title_list(self):
        self.assertTrue(title_for(self.db, 18707, "Tensei Kizoku ... Dai 2 Ki", "english").startswith("As a Reincarnated"))
        self.assertEqual(title_for(self.db, 18707, "x", "japanese"), "Tensei Kizoku, Kantei Skill de Nariagaru Dai 2 Ki")
        with self.db:
            self.db.execute("INSERT INTO anime_title_metadata VALUES(18707, 'From the metadata', NULL)")
        self.assertEqual(names(self.db, 18707)[0], "From the metadata")              # The metadata wins.
        self.assertEqual(title_for(self.db, 1, "Only Romaji", "english"), "Only Romaji")    # No English: as is.
        self.assertEqual(title_for(self.db, 99, "Unknown", "english"), "Unknown")

    def test_a_tvdb_series_is_named_after_its_season_one_entry(self):
        name = series_title(self.db, 434704, 18707, "Tensei Kizoku ... Dai 2 Ki", "english")
        self.assertEqual(name, "As a Reincarnated Aristocrat, I'll Use My Appraisal Skill to Rise in the World")
        self.assertEqual(series_title(self.db, 434704, 18707, "x", "japanese"),
                         "Tensei Kizoku, Kantei Skill de Nariagaru")
        self.assertEqual(series_title(self.db, 1, 18707, "x", "english"),               # Not mapped: its own.
                         "As a Reincarnated Aristocrat, I'll Use My Appraisal Skill to Rise in the World Season 2")


class OneFolderPerSeriesTests(unittest.TestCase):
    def plan(self, root, aid, season, name, folder=None):
        from types import SimpleNamespace
        evidence = {"path": "e.mkv", "sha256": "a" * 64, "file": [1, 2, 100, 4, 5]}
        identity = (aid, aid * 10, 434704, season, 1, 77, "AniDB title")
        return specification(SimpleNamespace(options=SimpleNamespace(download_root="/d")), evidence, identity,
                             str(root), 1, series_name=name, folder=folder)

    def test_every_season_of_a_tvdb_series_goes_in_one_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self.plan(root, 18026, 1, "As a Reincarnated Aristocrat")
            media = first["entries"][0]["path"]
            self.assertEqual(media, "As a Reincarnated Aristocrat [tvdbid-434704]/Season 01/"
                                    "As a Reincarnated Aristocrat - S01E01.mkv")
            (root / media).parent.mkdir(parents=True)
            # An older folder name (other title, or the old layout) is kept: the series never splits.
            os.rename(root / "As a Reincarnated Aristocrat [tvdbid-434704]",
                      root / "Tensei Kizoku [tvdbid-434704] [anidbid-18026]")
            folder = series_folder(str(root), 434704)
            self.assertEqual(folder, "Tensei Kizoku [tvdbid-434704] [anidbid-18026]")
            second = self.plan(root, 18707, 2, "As a Reincarnated Aristocrat", folder=folder)
            self.assertTrue(second["entries"][0]["path"].startswith(folder + "/Season 02/"))
            self.assertEqual(second["entries"][2]["path"], folder + "/tvshow.nfo")
            self.assertIn("As a Reincarnated Aristocrat", second["entries"][2]["text"])   # Shown in English.
            self.assertIsNone(series_folder(str(root), 1))
