"""Series Reject uses pinned stage identity, not paths or torrent names from HTTP."""
import json
import sqlite3
import unittest
from types import SimpleNamespace

from src.nextgen.jobs.reject_series_job import _targets, _enqueue_cleanup
from src.nextgen.staging import StagingReview


class RejectedSeriesTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE jobs(job_id INTEGER,kind TEXT,state TEXT,event_id INTEGER,payload_json TEXT);
            CREATE TABLE anime_review_decisions(anidb_id INTEGER,job_id INTEGER,
                decision TEXT,cleanup_state TEXT);
            CREATE TABLE job_operations(operation_id INTEGER,state TEXT);
            CREATE TABLE job_operation_members(operation_id INTEGER,job_id INTEGER);
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,
                target_job_id INTEGER,job_id INTEGER);
            CREATE TABLE episode_reservations(reservation_id INTEGER,anidb_id INTEGER,
                owner_job_id INTEGER);
            CREATE TABLE acquisition_plans(plan_id INTEGER,reservation_id INTEGER);
            CREATE TABLE staging_publications(plan_id INTEGER,job_id INTEGER,
                state TEXT,manifest_json TEXT,receipts_json TEXT);
            CREATE TABLE batch_staging_publications(plan_id INTEGER,file_index INTEGER,
                job_id INTEGER,state TEXT);
            CREATE TABLE batch_episode_reservations(plan_id INTEGER,file_index INTEGER,
                anidb_id INTEGER,owner_job_id INTEGER);
            INSERT INTO jobs VALUES(900,'REJECT_SERIES','RUNNING',NULL,
                '{"anidb_id":10,"operation_ids":[11,12]}');
            INSERT INTO jobs VALUES(101,'STAGE_USER_EVALUATION_MEDIA','DONE',1,'{}');
            INSERT INTO jobs VALUES(102,'STAGE_USER_EVALUATION_MEDIA','DONE',2,'{}');
            INSERT INTO anime_review_decisions VALUES(10,900,'REJECT','PENDING');
            INSERT INTO job_operations VALUES(11,'STAGED'),(12,'STAGED');
            INSERT INTO job_operation_members VALUES(11,101),(12,102);
            INSERT INTO episode_reservations VALUES(1,10,101),(2,10,102);
            INSERT INTO acquisition_plans VALUES(201,1),(202,2);
            INSERT INTO staging_publications VALUES
                (201,101,'PUBLISHED','{"root":"/trial","entries":[]}','[]'),
                (202,102,'PUBLISHED','{"root":"/trial","entries":[]}','[]');
        """)
        self.queue = SimpleNamespace(_owns=lambda *args: None, now=lambda: 1)
        self.claim = SimpleNamespace(job_id=900)

    def test_snapshot_contains_every_staged_acquisition(self):
        state, targets = _targets(self.db,self.queue,self.claim,10,(11,12))
        self.assertEqual(state,'PENDING')
        self.assertEqual([row[0] for row in targets],[11,12])
        self.assertEqual([row[2] for row in targets],[201,202])
        self.assertEqual([row[4][0] for row in targets],[101,102])

    def test_reject_never_accepts_a_different_anime_or_keep(self):
        self.db.execute('UPDATE episode_reservations SET anidb_id=99 WHERE reservation_id=2')
        with self.assertRaises(StagingReview):
            _targets(self.db,self.queue,self.claim,10,(11,12))
        self.db.execute('UPDATE episode_reservations SET anidb_id=10 WHERE reservation_id=2')
        self.db.execute("INSERT INTO management_actions VALUES(12,'KEEP',102,999)")
        with self.assertRaises(StagingReview):
            _targets(self.db,self.queue,self.claim,10,(11,12))

    def test_existing_single_cleanup_jobs_are_observed_not_duplicated(self):
        self.db.execute("INSERT INTO jobs VALUES(301,'CLEANUP_DECIDED_DOWNLOAD',"
                        "'DONE',1,'{\"staging_job_id\":101}')")
        self.db.execute("INSERT INTO jobs VALUES(302,'CLEANUP_DECIDED_DOWNLOAD',"
                        "'DONE',2,'{\"staging_job_id\":102}')")
        _, sources = _targets(self.db,self.queue,self.claim,10,(11,12))
        self.assertEqual(_enqueue_cleanup(self.db,self.queue,self.claim,sources),
                         ['DONE','DONE'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0],5)


if __name__ == '__main__':
    unittest.main()
