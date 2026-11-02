"""Offline coverage for language-specific AniDB titles and persisted display metadata."""
import sqlite3
import unittest

from src.nextgen.providers.parsing import anime_record
from src.nextgen.storage.series_intake import preferred_title
from src.nextgen.storage.sqlite import upsert_anime_titles


class AnimeTitleLanguageTests(unittest.TestCase):
    def test_anidb_xml_language_tags_are_preserved(self):
        xml = """<anime id="42"><titles>
          <title xml:lang="x-jat" type="main">Shingeki no Kyojin</title>
          <title xml:lang="ja" type="official">進撃の巨人</title>
          <title xml:lang="en" type="syn">A Different English Alias</title>
          <title xml:lang="en" type="official">Attack on Titan</title>
        </titles></anime>""".encode("utf-8")
        record = anime_record(xml, 42)
        self.assertEqual(record["title"], "Shingeki no Kyojin")
        self.assertEqual(record["romaji_title"], "Shingeki no Kyojin")
        self.assertEqual(record["english_title"], "Attack on Titan")
        self.assertEqual(record["original_title"], "進撃の巨人")

    def test_missing_translations_fall_back_to_original(self):
        xml = b'<anime id="7"><titles><title type="main">Example</title></titles></anime>'
        record = anime_record(xml, 7)
        self.assertIsNone(record["english_title"])
        self.assertIsNone(record["romaji_title"])
        self.assertEqual(preferred_title("Example", None, None, "english"), "Example")
        self.assertEqual(preferred_title("Example", None, None, "japanese"), "Example")

    def test_title_updates_do_not_change_canonical_identity(self):
        db = sqlite3.connect(":memory:")
        try:
            db.executescript("""
                CREATE TABLE identified_anime(anidb_id INTEGER PRIMARY KEY,
                    tvdb_series_id INTEGER NOT NULL, title TEXT NOT NULL);
                CREATE TABLE anime_title_metadata(anidb_id INTEGER PRIMARY KEY
                    REFERENCES identified_anime(anidb_id),
                    english_title TEXT, romaji_title TEXT);
                INSERT INTO identified_anime VALUES (42, 500, 'Canonical');
            """)
            upsert_anime_titles(db, 42, "English", "Romaji")
            upsert_anime_titles(db, 42, "Replacement", None)
            self.assertEqual(db.execute("SELECT * FROM identified_anime").fetchone(),
                             (42, 500, "Canonical"))
            self.assertEqual(db.execute(
                "SELECT english_title,romaji_title FROM anime_title_metadata").fetchone(),
                ("English", "Romaji"))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
