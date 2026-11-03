"""No CI required; offline test of the read-only AniDB-first preflight."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('anidb_selection_preflight',
                                              Path(__file__).with_name('anidb_selection_preflight.py'))
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)
main, preflight = fix.main, fix.preflight
from src.nextgen.storage.sqlite import initialize

HASH = 'a' * 40


def verification(db, vid, *, aid=50, anidb=1000, tid=100, season=2, episode=1, eid=None):
    db.execute(
        "INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,"
        "release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,"
        "tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms) "
        "VALUES(?,?,1,?,?,'DECLARED_SINGLE_EPISODE',?,?,?,?,?,?,1)",
        (vid, vid, aid, 'b' * 64, anidb, eid or 9000 + vid, tid, season, episode, 7000 + vid))


def plan(db, pid, *, aid=50, tid=100, season=2, anchor=1, vid=1):
    db.execute(
        "INSERT INTO trial_plans(plan_id,anidb_id,tvdb_series_id,tvdb_season,"
        "anchor_verification_id,anchor_episode,backfill_count,following_count,"
        "configuration_revision,created_at_ms) VALUES(?,?,?,?,?,?,6,1,1,1)",
        (pid, aid, tid, season, vid, anchor))


def result(db, pid, episode, status='NO_RESULTS'):
    db.execute(
        "INSERT INTO sonarr_backfill_results(plan_id,tvdb_episode,job_id,status,candidates,"
        "approved,exact,observed_at_ms) VALUES(?,?,1,?,0,0,0,1)", (pid, episode, status))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        initialize(self.db)
        self.db.execute('PRAGMA foreign_keys=OFF')
        # Anchor: AniDB E1000 verified as TVDB S02E01.
        verification(self.db, 1)
        plan(self.db, 1)

    def tearDown(self):
        self.db.close()

    def test_verified_rows_convert_and_report_is_clean(self):
        verification(self.db, 2, anidb=1001, episode=2)
        result(self.db, 1, 2, 'CANDIDATES')
        self.db.execute("INSERT INTO sonarr_episode_selections VALUES(1,2,7002,?,1)", (HASH,))
        self.db.execute("INSERT INTO sonarr_episode_candidates(plan_id,tvdb_episode,rank,info_hash) "
                        "VALUES(1,2,1,?)", (HASH,))
        report = preflight(self.db)
        self.assertEqual(report['blocking'], [])
        self.assertEqual(report['anchors'], [{'plan_id': 1, 'tvdb': [2, 1], 'anidb_episode_number': 1000}])
        self.assertEqual(report['rows']['sonarr_episode_selections'], 1)

    def test_observation_without_stored_evidence_blocks(self):
        result(self.db, 1, 5)  # NO_RESULTS: nothing ever verified S02E05
        report = preflight(self.db)
        self.assertEqual(len(report['unmapped']), 1)
        self.assertEqual(report['unmapped'][0]['table'], 'sonarr_backfill_results')
        self.assertEqual(report['unmapped'][0]['placement'], [2, 5])
        self.assertTrue(report['blocking'])

    def test_duplicate_plans_for_one_anime_block(self):
        verification(self.db, 3, anidb=1, season=1, episode=1)
        plan(self.db, 2, season=1, vid=3)
        report = preflight(self.db)
        self.assertEqual(report['duplicate_plans'], [{'anidb_id': 50, 'plan_ids': [1, 2]}])
        self.assertTrue(report['blocking'])

    def test_ambiguous_evidence_and_open_job_payload_are_reported(self):
        verification(self.db, 4, anidb=1003, episode=4)
        verification(self.db, 5, anidb=1004, episode=4)
        self.db.execute(
            "INSERT INTO jobs(job_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms) "
            "VALUES(10,'DISCOVER_SONARR_BACKFILL',?,'ON_HOLD',1,1,1)",
            (json.dumps({'plan_id': 1, 'start': 1, 'exhausted_episode': 4}),))
        report = preflight(self.db)
        self.assertEqual(report['open_jobs'][0]['exhausted_episode'], [4, None])
        self.assertIn('ambiguous', report['unmapped'][0]['reason'])

    def test_wrong_version_blocks_without_reading(self):
        self.db.execute('PRAGMA user_version=53')
        self.assertEqual(preflight(self.db)['blocking'],
                         ['schema version 53, expected %s' % fix.SCHEMA_VERSION])

    def test_cli_is_read_only_and_uses_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'copy.sqlite3'
            self.db.commit()
            disk = sqlite3.connect(path)
            self.db.backup(disk)
            disk.close()
            before = path.read_bytes()
            self.assertEqual(main(['--database', str(path)]), 0)
            disk = sqlite3.connect(path)
            result(disk, 1, 5)
            disk.commit()
            disk.close()
            before = path.read_bytes()
            self.assertEqual(main(['--database', str(path), '--json']), 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(main(['--database', str(Path(tmp) / 'missing.sqlite3')]), 2)


if __name__ == '__main__':
    unittest.main()
