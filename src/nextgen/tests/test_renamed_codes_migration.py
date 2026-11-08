"""Offline v63 -> v64: renamed and removed reason and event codes, history and JSON included."""
import json
import runpy
import sqlite3
import unittest
from pathlib import Path

from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.nextgen.storage.sqlite import initialize

SCRIPT_64 = (SQL_DIR / '064_renamed_codes.sql').read_text()
AUDIT = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'docs' / 'refactor' / 'reason_code_proposals.py'))


class RenamedCodesMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:63]:
            self.db.executescript((SQL_DIR / name).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,last_reason_code)
              VALUES(1,'release-discovery',CAST('{"note":"TRIAL_SELECTED"}' AS BLOB),1,'TRIAL_SELECTED');
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms,last_reason_code) VALUES
              (1,1,'DISCOVER_EPISODE_RELEASES','{"expected_reason":"SONARR_DISCOVERY_EPISODE_NOT_FOUND"}','BLOCKED',0,0,0,'SONARR_DISCOVERY_EPISODE_NOT_FOUND'),
              (2,1,'DISCOVER_EPISODE_RELEASES','{}','NEEDS_REVIEW',0,0,0,'PROWLARR_SEARCH_TIMEOUT'),
              (3,1,'VERIFY_RELEASE','{}','DONE',0,0,0,'IDENTITY_CONFLICT');
            INSERT INTO job_history(job_id,occurred_at_ms,event_code,reason_code,details_json) VALUES
              (1,1,'SONARR_BACKFILL_OBSERVED','SONARR_EPISODES_NOT_LISTED',NULL),
              (2,2,'NEEDS_REVIEW','JOB_RETRY_EXHAUSTED','{"cause_reason_code":"PROWLARR_SEARCH_TIMEOUT","note":"TRIAL_SELECTED is text"}'),
              (3,3,'ACTIVITY_CHANGED',NULL,NULL);
        """)

    def migrate(self):
        self.db.executescript(SCRIPT_64)

    def test_codes_in_columns_and_history_are_rewritten(self):
        self.migrate()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 64)
        self.assertEqual(self.db.execute('SELECT job_id,last_reason_code FROM jobs ORDER BY job_id').fetchall(),
                         [(1, 'EPISODE_DISCOVERY_OUTDATED'), (2, 'RELEASE_SEARCH_TIMEOUT'), (3, 'IDENTITY_CONFLICT')])
        self.assertEqual(self.db.execute('SELECT event_code,reason_code FROM job_history ORDER BY history_id').fetchall(),
                         [('EPISODE_DISCOVERY_RECORDED', 'EPISODE_DISCOVERY_OUTDATED'),
                          ('NEEDS_REVIEW', 'JOB_RETRY_EXHAUSTED'), ('DEFERRED', None)])
        self.assertEqual(self.db.execute('SELECT last_reason_code FROM incoming_events').fetchone(), ('USER_EVALUATION_SELECTED',))

    def test_codes_inside_json_are_rewritten_but_not_free_text_or_raw_bodies(self):
        self.migrate()
        details = json.loads(self.db.execute('SELECT details_json FROM job_history WHERE job_id=2').fetchone()[0])
        self.assertEqual(details, {'cause_reason_code': 'RELEASE_SEARCH_TIMEOUT', 'note': 'TRIAL_SELECTED is text'})
        payload = json.loads(self.db.execute('SELECT payload_json FROM jobs WHERE job_id=1').fetchone()[0])
        self.assertEqual(payload, {'expected_reason': 'EPISODE_DISCOVERY_OUTDATED'})
        self.assertEqual(self.db.execute('SELECT raw_body FROM incoming_events').fetchone()[0], b'{"note":"TRIAL_SELECTED"}')

    def test_tables_whose_checks_name_codes_are_rebuilt_with_the_new_codes(self):
        # Row preservation through the rebuild is covered by the upgrade-chain tests
        # (test_anidb_first_migration and others), which carry data to the latest version.
        sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name='user_evaluation_checks'").fetchone()[0]
        self.assertIn('TRIAL_SELECTED', sql)
        self.migrate()
        for table in ('user_evaluation_checks', 'torrent_submissions', 'batch_torrent_submissions'):
            with self.subTest(table=table):
                sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                self.assertNotRegex(sql, r'TRIAL_|TORRENT_PROVENANCE_CONFLICT')
        self.assertIn("'USER_EVALUATION_SELECTED'",
                      self.db.execute("SELECT sql FROM sqlite_master WHERE name='user_evaluation_checks'").fetchone()[0])
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertIsNotNone(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_user_evaluation_checks_plan'").fetchone())

    def test_no_old_code_is_left_anywhere(self):
        self.migrate()
        old = {k for t in (AUDIT['REASONS'], AUDIT['EVENTS']) for k, v in t.items() if k != v[0]} | set(AUDIT['REMOVED'])
        for (table,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            columns = [c[1] for c in self.db.execute(f'PRAGMA table_info({table})') if c[2] == 'TEXT']
            for column in columns:
                for code in old:
                    with self.subTest(table=table, column=column, code=code):
                        hits = self.db.execute(
                            f"SELECT count(*) FROM {table} WHERE {column}=? OR instr({column},?)>0",
                            (code, f'"{code}"')).fetchone()[0]
                        self.assertEqual(hits, 0)

    def test_guard_rolls_back_if_version_is_not_63(self):
        self.db.execute('PRAGMA user_version=62')
        with self.assertRaises(sqlite3.IntegrityError):
            self.migrate()
        self.db.execute('PRAGMA foreign_keys=ON')
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 62)
        self.assertEqual(self.db.execute('SELECT last_reason_code FROM jobs WHERE job_id=2').fetchone(),
                         ('PROWLARR_SEARCH_TIMEOUT',))

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
