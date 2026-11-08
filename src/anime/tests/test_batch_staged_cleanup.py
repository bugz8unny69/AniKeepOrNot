"""A fully staged pack queues its torrent cleanup once; partial packs never do."""
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from src.anime import batch_decision_cleanup as bdc
from src.anime.cancellation import CancellationError
from src.anime.jobs import cleanup_batch_download_job as job


class BatchStagedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE jobs(job_id INTEGER PRIMARY KEY,kind TEXT,state TEXT,
                payload_json TEXT,created_by_job_id INTEGER);
            CREATE TABLE batch_staging_publications(plan_id INTEGER,file_index INTEGER,
                job_id INTEGER,state TEXT);
            CREATE TABLE job_operation_members(operation_id INTEGER,job_id INTEGER);
            CREATE TABLE batch_user_evaluation_files(plan_id INTEGER,file_index INTEGER);
            CREATE TABLE batch_torrent_submissions(plan_id INTEGER,reservation_job_id INTEGER,
                info_hash TEXT,info_sha256 TEXT,state TEXT);
            INSERT INTO jobs VALUES(61,'STAGE_BATCH_EPISODE','DONE','{}',NULL),
                                   (62,'STAGE_BATCH_EPISODE','DONE','{}',NULL);
            INSERT INTO batch_staging_publications VALUES(15,0,61,'PUBLISHED'),(15,1,62,'PUBLISHED');
            INSERT INTO job_operation_members VALUES(7,61),(7,62);
            INSERT INTO batch_user_evaluation_files VALUES(15,0),(15,1);
            INSERT INTO batch_torrent_submissions VALUES(15,40,'h','s','OWNED_STOPPED');
        """)

    def enqueue(self, db, **kw):
        cur = db.execute('INSERT INTO jobs(kind,state,payload_json,created_by_job_id) '
                         "VALUES(?,'PENDING',?,?)",
                         (kw['kind'], __import__('json').dumps(kw['payload']),
                          kw['created_by_job_id']))
        self.delay = kw['available_at_ms']
        return cur.lastrowid

    def test_queued_once_after_last_episode_with_delay(self):
        claim = SimpleNamespace(job_id=62, event_id=None)
        with mock.patch.object(job, 'enqueue_job', self.enqueue):
            cleanup = job.enqueue_after_staging(self.db, claim, 15)
            self.assertEqual(job.staged_cleanup(self.db, 15), cleanup)
            self.assertGreater(self.delay, 0)
            with self.assertRaises(CancellationError):
                job.enqueue_after_staging(self.db, claim, 15)

    def facts(self, decision=62):
        service = bdc.BatchDecisionCleanup.__new__(bdc.BatchDecisionCleanup)
        service.decision, service.series_plan_id = decision, 15
        service.claim = SimpleNamespace(job_id=100)
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES(100,'CLEANUP_BATCH_DOWNLOAD',"
                        "'RUNNING','{}',?)", (decision,))
        with mock.patch.object(bdc, 'operation', lambda db, j: (7, 'STAGED')), \
             mock.patch.object(bdc, 'batch_publications', lambda db, op: ()), \
             mock.patch.object(bdc, 'protected_staging_roots', lambda rows: ('/trial',)):
            return service._staged_facts(self.db)

    def test_fully_staged_pack_is_cleanable(self):
        self.assertEqual(self.facts(), (7, 15, 40, 'h', 's', ('/trial',)))

    def test_partial_pack_is_never_cleaned(self):
        self.db.execute("UPDATE batch_staging_publications SET state='PLANNED' WHERE file_index=1")
        with self.assertRaises(CancellationError):
            self.facts()
        self.db.execute("UPDATE batch_staging_publications SET state='PUBLISHED'")
        self.db.execute('INSERT INTO batch_user_evaluation_files VALUES(15,2)')   # One not staged yet.
        with self.assertRaises(CancellationError):
            self.facts()

    def test_torrent_must_still_be_ours(self):
        self.db.execute("UPDATE batch_torrent_submissions SET state='RELEASED'")
        with self.assertRaises(CancellationError):
            self.facts()


if __name__ == '__main__':
    unittest.main()
