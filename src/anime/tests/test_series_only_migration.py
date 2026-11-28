"""Migration 080: the old pipeline's tables go, but never with unfinished work in them (owner, 2026-11-26)."""
import sqlite3
import unittest

from src.anime.storage import migrations
from src.anime.storage.migrations import _structure
from src.anime.storage.sqlite import initialize

V80 = (migrations.SQL_DIR / '080_series_only.sql').read_text()


def at_v79():
    db = sqlite3.connect(':memory:')
    for path in migrations.SCRIPTS[:79]:
        db.executescript((migrations.SQL_DIR / path).read_text())
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("INSERT INTO identified_anime VALUES(1001,2001,'Frieren')")
    db.execute("INSERT INTO recognized_series VALUES(1001,'Frieren',2001,NULL,1)")
    db.execute("INSERT INTO anime_title_metadata VALUES(1001,'Frieren: Beyond Journey''s End',NULL)")
    db.commit()
    return db


class SeriesOnlyMigrationTests(unittest.TestCase):
    def test_unfinished_old_work_refuses_the_migration(self):
        db = at_v79()
        db.execute("INSERT INTO jobs(job_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms) "
                   "VALUES(1,'VERIFY_RELEASE','{}','ON_HOLD',0,0,0)")
        db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            db.executescript(V80)
        db.rollback()
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 79)
        self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)     # Nothing lost.

    def test_finished_work_is_dropped_and_recognized_names_are_kept(self):
        db = at_v79()
        db.execute("INSERT INTO jobs(job_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms) "
                   "VALUES(1,'VERIFY_RELEASE','{}','DONE',0,0,0)")
        db.commit()
        db.executescript(V80)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 80)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('jobs', tables)
        self.assertEqual(db.execute('SELECT display_title FROM recognized_series').fetchall(), [('Frieren',)])
        self.assertEqual(db.execute('SELECT english_title FROM anime_title_metadata').fetchall(),
                         [("Frieren: Beyond Journey's End",)])
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        for name in migrations.SCRIPTS[80:]:                    # v81 on: the series job's own changes.
            db.executescript((migrations.SQL_DIR / name).read_text())
        fresh = sqlite3.connect(':memory:')
        initialize(fresh)
        self.assertEqual(_structure(db), _structure(fresh))      # The same as a new database.


if __name__ == '__main__':
    unittest.main()
