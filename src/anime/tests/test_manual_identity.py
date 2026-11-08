"""Manual match regression tests (local-only; no CI execution)."""
import tempfile
import unittest
from pathlib import Path

from src.anime.storage.sqlite import connect, initialize
from src.anime.storage.manual_identity import ManualMatchError, _key, for_release


class ManualMatchTests(unittest.TestCase):
    def test_alias_is_episode_independent(self):
        first = _key("Example Anime S01E01 1080p WEB-DL")
        second = _key("Example Anime S01E03 1080p WEB-DL")
        self.assertEqual(first, second)

    def test_existing_special_classification(self):
        from src.anime.storage.manual_identity import _scope
        self.assertEqual(_scope("Example Anime S00E01 1080p WEB"), 0)
        self.assertEqual(_scope("[Group] Example Anime - 01 [OVA]"), -1)
        self.assertEqual(_scope("Example Anime ONA WEB-DL"), -1)
        self.assertEqual(_scope("Example Anime S01E01 1080p WEB"), 1)
        self.assertIsNone(_scope("Example Anime S01E01 S02E01 WEB"))

    def test_season_scoped_match(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db.sqlite"
            db = connect(path)
            initialize(db)
            db.execute(
                "INSERT INTO manual_identity_matches "
                "(alias_key,tvdb_season,anidb_id,tvdb_series_id,title) VALUES(?,?,?,?,?)",
                (_key("Example Anime S01E01 1080p WEB-DL"),1,123,456,"Example Anime"))
            db.commit()
            db.close()
            match = for_release(str(path),"Example Anime S01E03 1080p WEB-DL",1)
            self.assertEqual(match["anime"]["anidb_id"],123)
            self.assertEqual(match["anime"]["tvdb_series_id"],456)
            self.assertEqual(match["episodes"],[])
            self.assertIsNone(for_release(str(path),"Example Anime S02E03 1080p WEB-DL",2))
            self.assertIsNone(for_release(str(path),"Example Anime OVA 1080p WEB-DL",None))
            db = connect(path)
            db.execute(
                "INSERT INTO manual_identity_matches "
                "(alias_key,tvdb_season,anidb_id,tvdb_series_id,title) VALUES(?,?,?,?,?)",
                (_key("Example Anime OVA WEB-DL"),-1,789,456,"Example Anime OVA"))
            db.commit()
            db.close()
            self.assertEqual(for_release(str(path),"Example Anime OVA WEB-DL",None)["anime"]["anidb_id"],789)
            self.assertIsNone(for_release(str(path),"Example Anime OVA WEB-DL",0))



if __name__ == "__main__":
    unittest.main()
