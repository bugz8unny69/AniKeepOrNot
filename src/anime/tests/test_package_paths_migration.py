"""v67: stored diagnostics follow the package move from src/nextgen to src/anime."""  # rename_package: keep
import json
import sqlite3
import unittest

from src.anime.storage.migrations import SCRIPTS, SQL_DIR

SCRIPT = (SQL_DIR / '067_package_paths.sql').read_text()


class PackagePathsMigrationTests(unittest.TestCase):
    def v66(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        for name in SCRIPTS[:66]:
            db.executescript((SQL_DIR / name).read_text())
        db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('autobrr',x'7b7d',1)")
        db.execute("INSERT INTO jobs(event_id,kind,payload_json,state,available_at_ms,attempt_count,created_at_ms,updated_at_ms) "
                   "VALUES(1,'KEEP_MEDIA','{}','NEEDS_REVIEW',0,1,1,1)")
        details = {'attempt': 1, 'failed_check': 'src/nextgen/keep_service.py:45 _run SERIES_MISSING',  # rename_package: keep
                   'failed_path': '/library/src/nextgen-like/name.mkv'}  # rename_package: keep
        db.execute("INSERT INTO job_history(job_id,event_code,reason_code,component,occurred_at_ms,details_json) "
                   "VALUES(1,'NEEDS_REVIEW','KEEP_IMPORT_REVIEW','worker',2,?)", (json.dumps(details),))
        db.commit()
        return db

    def test_stored_failed_checks_point_at_the_new_package(self):
        db = self.v66()
        db.executescript(SCRIPT)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 67)
        details = json.loads(db.execute('SELECT details_json FROM job_history').fetchone()[0])
        self.assertEqual(details['failed_check'], 'src/anime/keep_service.py:45 _run SERIES_MISSING')
        self.assertEqual(details['failed_path'], '/library/src/nextgen-like/name.mkv')   # only "src/nextgen/" moves; rename_package: keep

    def test_the_guard_refuses_another_version(self):
        db = self.v66()
        db.executescript(SCRIPT)
        with self.assertRaises(sqlite3.DatabaseError):
            db.executescript(SCRIPT)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 67)


if __name__ == '__main__':
    unittest.main()
