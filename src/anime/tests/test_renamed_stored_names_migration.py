"""Offline v63 -> v64: Sonarr-era and "trial" tables, column, job kinds and event source renamed."""
import sqlite3
import unittest

from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize

SCRIPT_63 = (SQL_DIR / '064_renamed_stored_names.sql').read_text()
TABLES = {
    'sonarr_backfill_results': 'episode_discovery_results',
    'sonarr_episode_candidates': 'episode_release_candidates',
    'sonarr_episode_selections': 'episode_release_selections',
    'sonarr_magnet_intents': 'magnet_intents',
    'trial_plans': 'user_evaluation_plans',
    'trial_evaluations': 'user_evaluation_checks',
    'batch_trial_plans': 'batch_user_evaluation_plans',
    'batch_trial_files': 'batch_user_evaluation_files',
}
KINDS = {
    'DISCOVER_SONARR_BACKFILL': 'DISCOVER_EPISODE_RELEASES',
    'EVALUATE_TRIAL': 'SELECT_USER_EVALUATION',
    'STAGE_TRIAL_MEDIA': 'STAGE_USER_EVALUATION_MEDIA',
}


class RenamedStoredNamesMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:63]:
            self.db.executescript((SQL_DIR / name).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES
              (1,'sonarr-discovery',X'7b7d',1),(2,'manual-release',X'7b7d',1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms) VALUES
              (1,1,'DISCOVER_SONARR_BACKFILL','{}','DONE',0,0,0),
              (2,1,'EVALUATE_TRIAL','{}','PENDING',0,0,0),
              (3,1,'STAGE_TRIAL_MEDIA','{}','NEEDS_REVIEW',0,0,0),
              (4,2,'VERIFY_RELEASE','{}','DONE',0,0,0);
            INSERT INTO job_history(job_id,occurred_at_ms,event_code,handler_code,handler_label,invocation_id) VALUES
              (1,1,'HANDLER_STARTED','DISCOVER_SONARR_BACKFILL','Discover','""" + 'a' * 32 + """'),
              (2,1,'HANDLER_STARTED','EVALUATE_TRIAL','Evaluate','""" + 'b' * 32 + """'),
              (4,1,'HANDLER_STARTED','VERIFY_RELEASE','Verify','""" + 'c' * 32 + """');
        """)

    def migrate(self):
        self.db.executescript(SCRIPT_63)

    def test_tables_column_and_indexes_have_their_new_names(self):
        self.migrate()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 64)
        names = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        for old, new in TABLES.items():
            with self.subTest(table=old):
                self.assertNotIn(old, names)
                self.assertIn(new, names)
        self.assertLessEqual({'idx_user_evaluation_checks_plan', 'idx_batch_user_evaluation_by_episode'}, names)
        self.assertFalse({'idx_trial_evaluations_plan', 'idx_batch_trial_by_episode'} & names)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(acquisition_budgets)')}
        self.assertIn('user_evaluation_plan_id', columns)
        self.assertNotIn('trial_plan_id', columns)

    def test_foreign_keys_follow_the_renamed_tables(self):
        self.migrate()
        referenced = {row[2] for (table,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      for row in self.db.execute(f'PRAGMA foreign_key_list({table})')}
        self.assertFalse(set(TABLES) & referenced, 'a foreign key still names an old table')
        self.assertIn('user_evaluation_plans', referenced)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        sql = ' '.join(row[0] for row in self.db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        for old in [*TABLES, 'trial_plan_id']:
            with self.subTest(name=old):
                self.assertNotRegex(sql, rf'(?<![A-Za-z0-9_]){old}(?![A-Za-z0-9_])')

    def test_job_kinds_history_and_event_source_are_rewritten(self):
        self.migrate()
        self.assertEqual(self.db.execute('SELECT job_id,kind,state FROM jobs ORDER BY job_id').fetchall(),
                         [(1, 'DISCOVER_EPISODE_RELEASES', 'DONE'), (2, 'SELECT_USER_EVALUATION', 'PENDING'),
                          (3, 'STAGE_USER_EVALUATION_MEDIA', 'NEEDS_REVIEW'), (4, 'VERIFY_RELEASE', 'DONE')])
        self.assertEqual(self.db.execute('SELECT job_id,handler_code,handler_label FROM job_history ORDER BY job_id').fetchall(),
                         [(1, 'DISCOVER_EPISODE_RELEASES', 'Discover'), (2, 'SELECT_USER_EVALUATION', 'Evaluate'),
                          (4, 'VERIFY_RELEASE', 'Verify')])
        self.assertEqual(self.db.execute('SELECT event_id,source,raw_body FROM incoming_events ORDER BY event_id').fetchall(),
                         [(1, 'release-discovery', b'{}'), (2, 'manual-release', b'{}')])
        for old in KINDS:
            with self.subTest(kind=old):
                self.assertEqual(self.db.execute('SELECT count(*) FROM jobs WHERE kind=?', (old,)).fetchone(), (0,))

    def test_guard_rolls_back_if_version_is_not_62(self):
        self.db.execute('PRAGMA user_version=62')
        with self.assertRaises(sqlite3.IntegrityError):
            self.migrate()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 62)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='trial_plans'").fetchone())
        self.assertEqual(self.db.execute("SELECT kind FROM jobs WHERE job_id=2").fetchone(), ('EVALUATE_TRIAL',))

    def test_fresh_schema_matches_sequential_upgrade(self):
        self.migrate()
        for name in SCRIPTS[64:]:  # Later versions build on v64.
            self.db.executescript((SQL_DIR / name).read_text())
        fresh = sqlite3.connect(':memory:')
        try:
            initialize(fresh)
            self.assertEqual(_structure(self.db), _structure(fresh))
        finally:
            fresh.close()


if __name__ == '__main__':
    unittest.main()
