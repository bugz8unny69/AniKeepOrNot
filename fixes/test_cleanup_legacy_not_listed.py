"""No CI required; standalone offline regression tests for one-time fix."""
import importlib.util
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cleanup_legacy_not_listed', Path(__file__).with_name('cleanup_legacy_not_listed.py'))
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


class BackfillCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'nextgen.sqlite'
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                CREATE TABLE user_evaluation_plans(plan_id INTEGER, tvdb_series_id INTEGER, tvdb_season INTEGER);
                CREATE TABLE episode_discovery_results(plan_id INTEGER, tvdb_episode INTEGER, status TEXT,
                    PRIMARY KEY(plan_id,tvdb_episode));
                CREATE TABLE episode_release_selections(plan_id INTEGER, tvdb_episode INTEGER);
                CREATE TABLE episode_release_candidates(plan_id INTEGER, tvdb_episode INTEGER);
                CREATE TABLE episode_reservations(tvdb_series_id INTEGER,tvdb_season INTEGER,
                    tvdb_episode INTEGER,state TEXT);
                CREATE TABLE batch_episode_reservations(tvdb_series_id INTEGER,tvdb_season INTEGER,
                    tvdb_episode INTEGER,state TEXT);
                INSERT INTO user_evaluation_plans VALUES (5,200,2);
                INSERT INTO episode_discovery_results VALUES (5,1,'NOT_LISTED'),
                     (5,2,'NOT_LISTED'),(5,3,'CANDIDATES'),(5,4,'NO_RESULTS');
                INSERT INTO episode_release_selections VALUES (5,2);
                PRAGMA user_version=%d;
            ''' % fix.SCHEMA_VERSION)

    def tearDown(self):
        self.temp.cleanup()

    def statuses(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT tvdb_episode,status FROM episode_discovery_results ORDER BY tvdb_episode').fetchall()

    def test_preview_never_changes_data(self):
        report = fix.execute(self.path)
        self.assertEqual((report['eligible'], report['protected'], report['deleted']), (1,1,0))
        self.assertEqual(len(list(self.path.parent.glob('*.before-not-listed-*.sqlite'))),0)
        self.assertEqual(len(self.statuses()),4)

    def test_apply_backs_up_only_unclaimed_historical_rows(self):
        with patch.object(fix,'ensure_stopped'):
            report = fix.execute(self.path,True)
        self.assertEqual(report['deleted'],1)
        self.assertTrue(Path(report['backup']).is_file())
        self.assertEqual(self.statuses(),[(2,'NOT_LISTED'),(3,'CANDIDATES'),(4,'NO_RESULTS')])
        with closing(sqlite3.connect(report['backup'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM episode_discovery_results').fetchone(),(4,))
        with patch.object(fix,'ensure_stopped'):
            repeat=fix.execute(self.path,True)
        self.assertEqual(repeat['deleted'],0)

    def test_active_reservation_is_protected(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO batch_episode_reservations VALUES (200,2,1,'ACTIVE')")
            db.commit()
        with patch.object(fix,'ensure_stopped'):
            result=fix.execute(self.path,True)
        self.assertEqual((result['eligible'],result['protected'],result['deleted']),(0,2,0))

    def test_rejects_unknown_schema_without_modifying_data(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=47')
        with self.assertRaisesRegex(ValueError,f'schema v{fix.SCHEMA_VERSION}'):
            fix.execute(self.path)
        self.assertEqual(len(self.statuses()),4)


if __name__ == '__main__':
    unittest.main()
