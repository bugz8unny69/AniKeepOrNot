"""Work item P2: Recognized names track every series (AniDB anime) and its episodes."""
import json
import sqlite3
import unittest
from types import SimpleNamespace

from src.anime.storage.migrations import SCRIPTS, SQL_DIR, _structure
from src.anime.storage.sqlite import initialize


def event(db, release):
    return db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('autobrr',?,1)",
                      (json.dumps({'release_name': release}).encode(),)).lastrowid









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
        for name in SCRIPTS[70:79]:
            db.executescript((SQL_DIR / name).read_text())
        db.execute("UPDATE jobs SET state='DONE'")         # The cutover closes the old work before v80.
        db.commit()
        db.executescript((SQL_DIR / SCRIPTS[79]).read_text())
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
