"""Offline v55 -> v56: AniDB-first trial keys, fresh parity, drop-only observations, rollback."""
import json
import sqlite3
import unittest

from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, _structure, LATEST_VERSION
from src.nextgen.storage.sqlite import initialize

SCRIPT = (SQL_DIR / '056_anidb_first_selection.sql').read_text()
HASH = 'a' * 40


class AniDBFirstMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:55]:
            self.db.executescript((SQL_DIR / name).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'test',X'61',1);
            INSERT INTO identified_anime VALUES(50,100,'Long Show');
            INSERT INTO identity_results VALUES(1,1,50,2,'{}',1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(1,1,'VERIFY_RELEASE','{}','DONE',0,0,0),(2,1,'VERIFY_RELEASE','{}','DONE',0,0,0),
                    (3,1,'DISCOVER_SONARR_BACKFILL','{"plan_id":1,"start":1}','DONE',0,0,0);
            INSERT INTO identified_episodes VALUES(5000,50),(4999,50);
        """)
        # Anchor: AniDB E1000 verified as TVDB S02E01. E999 is verified as S01E999.
        self.verify(1, 1, 1000, 5000, 2, 1)
        self.verify(2, 2, 999, 4999, 1, 999)
        self.db.execute("INSERT INTO trial_plans(plan_id,anidb_id,tvdb_series_id,tvdb_season,"
                        "anchor_verification_id,anchor_episode,backfill_count,following_count,"
                        "configuration_revision,created_at_ms) VALUES(1,50,100,2,1,1,6,3,1,0)")
        self.db.commit()

    def verify(self, vid, job, anidb, eid, season, episode):
        self.db.execute(
            "INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,"
            "release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,"
            "tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms) "
            "VALUES(?,?,1,50,?,'DECLARED_SINGLE_EPISODE',?,?,100,?,?,?,1)",
            (vid, job, 'b' * 64, anidb, eid, season, episode, 9000 + vid))

    def result(self, episode, status):
        self.db.execute("INSERT INTO sonarr_backfill_results(plan_id,tvdb_episode,job_id,status,"
                        "candidates,approved,exact,observed_at_ms) VALUES(1,?,3,?,0,0,0,1)",
                        (episode, status))

    def migrate(self):
        self.db.commit()
        self.db.executescript(SCRIPT)
        self.db.executescript((SQL_DIR / '057_anidb_first_staging.sql').read_text())
        self.db.executescript((SQL_DIR / '058_failed_job_state.sql').read_text())
        self.db.executescript((SQL_DIR / '059_identity_candidate_evidence.sql').read_text())
        self.db.executescript((SQL_DIR / '060_batch_candidate_operations.sql').read_text())
        self.db.executescript((SQL_DIR / '061_batch_candidate_stops.sql').read_text())
        self.db.executescript((SQL_DIR / '062_manual_release_archive.sql').read_text())
        return self.db

    def test_runner_applies_the_step_with_validation(self):
        from src.nextgen.storage import migrations
        self.result(1, 'CANDIDATES')
        self.db.commit()
        migrations._apply_step(self.db, SCRIPT, 56, migrations._scripts())
        self.assertEqual(self.db.execute('SELECT anidb_episode_number FROM sonarr_backfill_results').fetchall(),
                         [(1000,)])

    def test_fresh_schema_matches_upgrade(self):
        fresh = sqlite3.connect(':memory:')
        try:
            initialize(fresh)
            self.assertEqual(_structure(self.migrate()), _structure(fresh))
        finally:
            fresh.close()

    def test_anchor_and_keys_become_anidb_and_unconvertible_observations_are_dropped(self):
        self.result(1, 'CANDIDATES')      # S02E01 -> AniDB E1000 (verified)
        self.result(4, 'NOT_AIRED')       # S02E04: never verified -> dropped
        self.db.execute("INSERT INTO sonarr_episode_candidates(plan_id,tvdb_episode,rank,info_hash) "
                        "VALUES(1,1,1,?)", (HASH,))
        self.db.execute("INSERT INTO sonarr_episode_selections VALUES(1,1,9001,?,1)", (HASH,))
        self.db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,"
                        "created_at_ms,updated_at_ms) VALUES(4,1,'DISCOVER_SONARR_BACKFILL',?,"
                        "'ON_HOLD',0,0,0)", (json.dumps({'plan_id': 1, 'start': 1, 'exhausted_episode': 1}),))
        self.db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,"
                        "created_at_ms,updated_at_ms) VALUES(5,1,'DISCOVER_SONARR_BACKFILL',?,"
                        "'DONE',0,0,0)", (json.dumps({'plan_id': 1, 'start': 1, 'retry_episode': 4}),))
        db = self.migrate()
        self.assertEqual(db.execute('PRAGMA user_version').fetchone(), (LATEST_VERSION,))
        self.assertEqual(db.execute('SELECT tvdb_season,anchor_episode FROM trial_plans').fetchall(),
                         [(2, 1000)])
        self.assertEqual(db.execute('SELECT anidb_episode_number,status FROM sonarr_backfill_results').fetchall(),
                         [(1000, 'CANDIDATES')])
        self.assertEqual(db.execute('SELECT anidb_episode_number,tvdb_season,tvdb_episode,tvdb_episode_id,'
                                    'info_hash FROM sonarr_episode_selections').fetchall(),
                         [(1000, 2, 1, 9001, HASH)])
        self.assertEqual(db.execute('SELECT anidb_episode_number FROM sonarr_episode_candidates').fetchall(),
                         [(1000,)])
        self.assertEqual(json.loads(db.execute('SELECT payload_json FROM jobs WHERE job_id=4').fetchone()[0]),
                         {'plan_id': 1, 'start': 1, 'exhausted_episode': 1000})
        # A finished job without evidence keeps its historical payload untouched.
        self.assertEqual(json.loads(db.execute('SELECT payload_json FROM jobs WHERE job_id=5').fetchone()[0]),
                         {'plan_id': 1, 'start': 1, 'retry_episode': 4})
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])

    def assertAborts(self):
        before = _structure(self.db)
        rows = self.db.execute('SELECT count(*) FROM sonarr_backfill_results').fetchone()
        self.db.commit()
        with self.assertRaises(sqlite3.Error):
            self.db.executescript(SCRIPT)
        if self.db.in_transaction:
            self.db.rollback()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone(), (55,))
        self.assertEqual(_structure(self.db), before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sonarr_backfill_results').fetchone(), rows)

    def test_selection_without_stored_evidence_aborts_and_keeps_v55(self):
        self.result(4, 'NO_RESULTS')
        self.db.execute("INSERT INTO sonarr_episode_selections VALUES(1,4,9004,?,1)", (HASH,))
        self.assertAborts()

    def test_open_job_without_evidence_aborts(self):
        self.db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,"
                        "created_at_ms,updated_at_ms) VALUES(4,1,'DISCOVER_SONARR_BACKFILL',?,"
                        "'ON_HOLD',0,0,0)", (json.dumps({'plan_id': 1, 'start': 1, 'exhausted_episode': 4}),))
        self.assertAborts()

    def test_duplicate_plans_for_one_anime_abort(self):
        self.db.execute("INSERT INTO trial_plans(plan_id,anidb_id,tvdb_series_id,tvdb_season,"
                        "anchor_verification_id,anchor_episode,backfill_count,following_count,"
                        "configuration_revision,created_at_ms) VALUES(2,50,100,1,2,999,6,3,1,0)")
        self.assertAborts()

    def test_ambiguous_evidence_aborts(self):
        # A second verification claims S02E01 is AniDB E1001.
        self.db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,"
                        "created_at_ms,updated_at_ms) VALUES(6,1,'VERIFY_RELEASE','{}','DONE',0,0,0)")
        self.db.execute("INSERT INTO identified_episodes VALUES(5001,50)")
        self.verify(3, 6, 1001, 5001, 2, 1)
        self.result(1, 'NO_RESULTS')
        self.assertAborts()


if __name__ == '__main__':
    unittest.main()
