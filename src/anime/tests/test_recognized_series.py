"""Work item P2: Recognized names track every series (AniDB anime) and its episodes."""
import json
import sqlite3
import unittest
from types import SimpleNamespace

from src.anime.storage.migrations import SCRIPTS, SQL_DIR, _structure
from src.anime.storage.recognition import (
    _alias, recognized_for_release, remember_verified_episode, remember_verified_release)
from src.anime.storage.sqlite import initialize, save_identity, transaction


def event(db, release):
    return db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('autobrr',?,1)",
                      (json.dumps({'release_name': release}).encode(),)).lastrowid


def identify(db, aid, tid, title, release, kind=None):
    anime = {'anidb_id': aid, 'tvdb_series_id': tid, 'title': title}
    if kind:
        anime['type'] = kind
    with transaction(db):
        number = event(db, release)
    return save_identity(db, number, {'schema_version': 2, 'anime': anime})


class RecognizedSeriesTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        initialize(self.db)
        self.addCleanup(self.db.close)

    def series(self):
        return self.db.execute('SELECT anidb_id,tvdb_series_id,anime_type FROM recognized_series ORDER BY anidb_id').fetchall()

    def test_identified_series_is_recognized_even_when_its_episode_has_no_tvdb_placement(self):
        result = identify(self.db, 1001, 2001, 'Sousou no Frieren', 'Frieren Beyond Journeys End - 05 [1080p]')
        self.assertEqual(self.series(), [(1001, 2001, None)])
        self.assertEqual(recognized_for_release(self.db, 'Frieren Beyond Journeys End - 06')['anidb_id'], 1001)
        # Verification stopped at MAPPING_MISSING: the AniDB episode is still recognized.
        with transaction(self.db):
            job = self.db.execute("INSERT INTO jobs(kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) "
                                  "VALUES('VERIFY_RELEASE','{}',1,1,1)").lastrowid
            self.db.execute('INSERT INTO identified_episodes VALUES(30005,1001)')
            verification = self.db.execute(
                "INSERT INTO release_verifications(job_id,identity_result_id,anidb_id,release_sha256,verification_scope,"
                "anidb_episode_number,anidb_episode_id,tvdb_series_id,verified_at_ms) "
                "VALUES(?,?,1001,?,'DECLARED_SINGLE_EPISODE',5,30005,2001,1)", (job, result, 'a' * 64)).lastrowid
            source = SimpleNamespace(anidb_id=1001, tvdb_series_id=2001,
                                     raw_body=json.dumps({'release_name': 'Frieren - 05'}).encode())
            remember_verified_release(self.db, verification_id=verification, source=source, season=None)
            remember_verified_episode(self.db, verification_id=verification)
        self.assertEqual(self.db.execute('SELECT anidb_episode_number,tvdb_season,tvdb_episode_id FROM recognized_episodes').fetchall(),
                         [(5, None, None)])
        self.assertEqual(self.db.execute('SELECT count(*) FROM recognized_seasons').fetchone()[0], 0)

    def test_ova_and_movie_are_their_own_series_and_tags_narrow_a_shared_alias(self):
        identify(self.db, 1001, 2001, 'Example Anime', 'Example Anime - 01', 'TV Series')
        identify(self.db, 1002, 2001, 'Example Anime OVA', 'Example Anime OVA - 01', 'OVA')
        identify(self.db, 1003, 2001, 'Example Anime Movie', 'Example Anime (Movie) [1080p]', 'Movie')
        self.assertEqual([row[0] for row in self.series()], [1001, 1002, 1003])
        # "Example Anime" names all three: without a tag it fails closed.
        self.db.execute("INSERT OR IGNORE INTO recognized_aliases VALUES(?,1002)", (_alias('Example Anime'),))
        self.assertIsNone(recognized_for_release(self.db, 'Example Anime - 02'))
        self.assertEqual(recognized_for_release(self.db, 'Example Anime OVA - 02')['anidb_id'], 1002)
        self.assertEqual(recognized_for_release(self.db, 'Example Anime Movie')['anidb_id'], 1003)

    def test_season_marker_narrows_to_the_verified_tvdb_season(self):
        identify(self.db, 1001, 2001, 'Example Anime', 'Example Anime S01E01')
        identify(self.db, 1002, 2001, 'Example Anime Season 2', 'Example Anime S02E01')
        self.db.execute("INSERT OR IGNORE INTO recognized_aliases VALUES(?,1002)", (_alias('Example Anime'),))
        self.assertIsNone(recognized_for_release(self.db, 'Example Anime - 03'))
        self.assertIsNone(recognized_for_release(self.db, 'Example Anime S02E03', season=2))   # no verified season yet
        self.db.execute("INSERT INTO jobs(job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) "
                        "VALUES(50,'VERIFY_RELEASE','{}',1,1,1)")
        self.db.execute("INSERT INTO identified_episodes VALUES(30101,1002)")
        self.db.execute("INSERT INTO release_verifications VALUES(50,50,2,1002,?,'DECLARED_SINGLE_EPISODE',1,30101,2001,2,1,40101,'official',1)",
                        ('b' * 64,))
        self.db.execute("INSERT INTO recognized_seasons VALUES(2001,2,1002,'Example Anime Season 2',50,1)")
        self.assertEqual(recognized_for_release(self.db, 'Example Anime S02E03', season=2)['anidb_id'], 1002)


class RecognizedSeriesMigrationTests(unittest.TestCase):
    def v69(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        for name in SCRIPTS[:69]:
            db.executescript((SQL_DIR / name).read_text())
        db.execute("INSERT INTO identified_anime VALUES(1001,2001,'Example Anime')")
        db.execute("INSERT INTO identified_anime VALUES(1002,2002,'Never Verified')")
        db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'autobrr',x'7b7d',1)")
        db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(1,1,'VERIFY_RELEASE','{}',1,1,1)")
        db.execute("INSERT INTO identity_results VALUES(1,1,1001,2,'{}',7)")
        db.execute("INSERT INTO identified_episodes VALUES(30001,1001)")
        db.execute("INSERT INTO release_verifications VALUES(1,1,1,1001,?,'DECLARED_SINGLE_EPISODE',1,30001,2001,1,1,40001,'official',5)", ('a' * 64,))
        db.execute("INSERT INTO recognized_seasons VALUES(2001,1,1001,'Example Anime',1,5)")
        db.execute("INSERT INTO recognized_aliases VALUES('example anime',1,2001,1001)")
        db.execute("INSERT INTO recognized_aliases VALUES('example',1,2001,1001)")
        db.execute("INSERT INTO recognized_episodes VALUES(2001,1,1001,1,30001,1,40001,1,5)")
        db.commit()
        return db

    def test_rows_are_kept_and_every_identified_anime_gets_a_series(self):
        db = self.v69()
        db.executescript((SQL_DIR / '070_recognized_series.sql').read_text())
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 70)
        self.assertEqual(db.execute('SELECT * FROM recognized_series ORDER BY anidb_id').fetchall(),
                         [(1001, 'Example Anime', 2001, None, 5), (1002, 'Never Verified', 2002, None, 0)])
        self.assertEqual(db.execute('SELECT * FROM recognized_aliases ORDER BY alias_key').fetchall(),
                         [('example', 1001), ('example anime', 1001)])
        self.assertEqual(db.execute('SELECT * FROM recognized_episodes').fetchall(),
                         [(2001, 1, 1001, 1, 30001, 1, 40001, 1, 5)])
        self.assertEqual(db.execute('SELECT count(*) FROM recognized_seasons').fetchone()[0], 1)
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        fresh = sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        initialize(fresh)
        self.assertEqual(_structure(db), _structure(fresh))

    def test_a_conflicting_series_stops_the_migration(self):
        db = self.v69()
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute("UPDATE recognized_seasons SET tvdb_series_id=2999")
        db.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            db.executescript((SQL_DIR / '070_recognized_series.sql').read_text())
        if db.in_transaction:
            db.rollback()
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 69)
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='recognized_series'").fetchone())


if __name__ == '__main__':
    unittest.main()
