"""Focused regression: releasing a reservation wakes one oldest eligible duplicate."""
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.nextgen.storage.episode_reservations import wake_waiters, _resume_unreserved_waiters


class ReservationHandoffTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript("""
            CREATE TABLE episode_reservations(
                reservation_id INTEGER, anidb_id INTEGER, anidb_episode_id INTEGER,
                tvdb_series_id INTEGER, tvdb_season INTEGER, tvdb_episode INTEGER,
                tvdb_episode_id INTEGER, state TEXT);
            CREATE TABLE batch_episode_reservations(state TEXT,anidb_episode_id INTEGER,
                tvdb_episode_id INTEGER,tvdb_series_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER);
            CREATE TABLE trial_evaluations(job_id INTEGER,verification_id INTEGER,selected INTEGER);
            CREATE TABLE release_verifications(verification_id INTEGER,anidb_id INTEGER,
                anidb_episode_id INTEGER,tvdb_series_id INTEGER,tvdb_season INTEGER,
                tvdb_episode INTEGER,tvdb_episode_id INTEGER);
            CREATE TABLE jobs(
                job_id INTEGER, event_id INTEGER, kind TEXT, state TEXT,
                last_reason_code TEXT, payload_json TEXT, attempt_count INTEGER,
                available_at_ms INTEGER, updated_at_ms INTEGER, activity_code TEXT,
                created_at_ms INTEGER);
            CREATE TABLE job_operations(operation_id INTEGER, state TEXT);
            CREATE TABLE job_operation_members(job_id INTEGER, operation_id INTEGER);
            INSERT INTO episode_reservations VALUES(99,19847,317547,472962,1,5,12013547,'ACTIVE');
            INSERT INTO trial_evaluations VALUES(31,301,1),(32,302,1),(33,303,1);
            INSERT INTO release_verifications VALUES
              (301,19847,317547,472962,1,5,12013547),
              (302,19847,317547,472962,1,5,12013547),
              (303,19847,317547,472962,1,5,12013547);
            INSERT INTO job_operations VALUES(1,'ACTIVE'),(2,'ACTIVE'),(3,'ACTIVE');
            INSERT INTO job_operation_members VALUES(2001,1),(2002,2),(2003,3);
            INSERT INTO jobs(job_id,event_id,kind,state,last_reason_code,
                             payload_json,attempt_count,created_at_ms)
                VALUES(2001,21,'RESERVE_EPISODE','ON_HOLD','EPISODE_RESERVED',
                       '{"evaluation_job_id":31}',1,100),
                      (2002,22,'RESERVE_EPISODE','ON_HOLD','EPISODE_RESERVED',
                       '{"evaluation_job_id":32}',1,200),
                      (2003,23,'RESERVE_EPISODE','ON_HOLD','EPISODE_RESERVED',
                       '{"evaluation_job_id":33}',1,300);
        """)

    def tearDown(self):
        self.db.close()

    def test_wake_one_and_leave_other_candidates_on_hold(self):
        facts = SimpleNamespace(selected=True,anidb_id=19847,anidb_episode_id=317547,
                                tvdb_series_id=472962,tvdb_season=1,tvdb_episode=5,
                                tvdb_episode_id=12013547)
        with patch('src.nextgen.storage.episode_reservations._facts', return_value=facts), \
             patch('src.nextgen.storage.episode_reservations.append_job_history',
                   return_value='audit'):
            self.db.execute('BEGIN')
            history = wake_waiters(self.db,99,1000,component='test')
            self.db.commit()
        self.assertEqual(history,('audit',))
        rows = self.db.execute('SELECT job_id,state FROM jobs ORDER BY job_id').fetchall()
        self.assertEqual(rows,[(2001,'PENDING'),(2002,'ON_HOLD'),(2003,'ON_HOLD')])

    def test_skip_ineligible_first_waiter(self):
        facts = SimpleNamespace(selected=True,anidb_id=19847,anidb_episode_id=317547,
                                tvdb_series_id=472962,tvdb_season=1,tvdb_episode=5,
                                tvdb_episode_id=12013547)
        def inspect(db,evaluation,event):
            return SimpleNamespace(**{**vars(facts), 'selected': evaluation != 31})
        with patch('src.nextgen.storage.episode_reservations._facts', side_effect=inspect), \
             patch('src.nextgen.storage.episode_reservations.append_job_history',
                   return_value='audit'):
            self.db.execute('BEGIN')
            wake_waiters(self.db,99,1000,component='test')
            self.db.commit()
        rows = self.db.execute('SELECT job_id,state FROM jobs ORDER BY job_id').fetchall()
        self.assertEqual(rows,[(2001,'ON_HOLD'),(2002,'PENDING'),(2003,'ON_HOLD')])


    def test_reconcile_wakes_next_after_previous_pending_contender_fails(self):
        facts = SimpleNamespace(selected=True,anidb_id=19847,anidb_episode_id=317547,
                                tvdb_series_id=472962,tvdb_season=1,tvdb_episode=5,
                                tvdb_episode_id=12013547)
        with patch('src.nextgen.storage.episode_reservations._facts', return_value=facts), \
             patch('src.nextgen.storage.episode_reservations.append_job_history',
                   return_value='audit'):
            self.db.execute('BEGIN')
            self.db.execute("UPDATE episode_reservations SET state='RELEASED'")
            wake_waiters(self.db, 99, 1000)
            # The first woken attempt failed before claiming a new reservation.
            self.db.execute("UPDATE jobs SET state='NEEDS_REVIEW' WHERE job_id=2001")
            history = _resume_unreserved_waiters(self.db, 2000)
            self.db.commit()
        self.assertEqual(history, ('audit',))
        rows = self.db.execute(
            "SELECT job_id,state FROM jobs ORDER BY job_id").fetchall()
        self.assertEqual(rows, [
            (2001,'NEEDS_REVIEW'),(2002,'PENDING'),(2003,'ON_HOLD')
        ])



if __name__ == '__main__':
    unittest.main()
