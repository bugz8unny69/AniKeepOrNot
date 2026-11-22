"""A poll that finds nothing new leaves no trace (prod 2026-11-22).

Job 8766 had 72,804 history rows: every 30 s download check stored "job
started, step started, step finished, waiting" and counted as an attempt.
"""
import sqlite3
import unittest

from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import Outcome, Registration, Registry
from src.anime.storage.sqlite import accept_event_and_job
from src.anime.tests import test_async_worker as base
from src.anime.worker import Worker


class QuietPollTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.AsyncWorkerTests.asyncSetUp

    def query(self, sql, args=()):
        db = sqlite3.connect(self.database)
        try: return db.execute(sql, args).fetchall()
        finally: db.close()

    async def run_polls(self, outcomes):
        db = sqlite3.connect(self.database)
        try: _, job = accept_event_and_job(db, 'test', b'{"release_name":"[Group] Show - 02","torrent_infohash":"'
                                           + b'a' * 40 + b'"}', kind='DOWNLOAD_TORRENT', payload={})
        finally: db.close()
        pending = list(outcomes)
        async def handler(context): return pending.pop(0)
        spec = Registration('DOWNLOAD_TORRENT', HandlerDefinition('DOWNLOAD_TORRENT', 'Test step'), handler)
        worker = Worker(self.store, self.startup, Registry((spec,)), policy=self.policy)
        self.addAsyncCleanup(worker.close)
        while pending:
            db = sqlite3.connect(self.database); db.execute("UPDATE jobs SET available_at_ms=0 WHERE job_id=?", (job,)); db.commit(); db.close()
            self.assertTrue(await worker.attempt())
        return job

    def events(self, job):
        return [r[0] for r in self.query("SELECT event_code FROM job_history WHERE job_id=? ORDER BY history_id", (job,))]

    async def test_repeated_download_checks_store_one_wait(self):
        wait = lambda: Outcome('defer', 'TORRENT_DOWNLOAD_IN_PROGRESS', delay_ms=30000, activity_code='DOWNLOADING')
        job = await self.run_polls([wait() for _ in range(5)])
        self.assertEqual(self.events(job), ['CREATED', 'CLAIMED', 'HANDLER_STARTED', 'HANDLER_COMPLETED', 'DEFERRED'])
        self.assertEqual(self.query("SELECT state,attempt_count,activity_code FROM jobs WHERE job_id=?", (job,)),
                         [('RETRY_WAIT', 1, 'DOWNLOADING')])

    async def test_a_change_is_recorded_and_the_end_is_recorded(self):
        job = await self.run_polls([
            Outcome('defer', 'TORRENT_DOWNLOAD_WAITING', delay_ms=30000, activity_code='WAITING_FOR_DOWNLOAD'),
            Outcome('defer', 'TORRENT_DOWNLOAD_WAITING', delay_ms=30000, activity_code='WAITING_FOR_DOWNLOAD'),
            Outcome('defer', 'TORRENT_DOWNLOAD_IN_PROGRESS', delay_ms=30000, activity_code='DOWNLOADING'),
            Outcome('defer', 'TORRENT_DOWNLOAD_IN_PROGRESS', delay_ms=30000, activity_code='DOWNLOADING'),
            Outcome(value=None),
        ])
        attempt = ['CLAIMED', 'HANDLER_STARTED', 'HANDLER_COMPLETED']
        self.assertEqual(self.events(job), ['CREATED', *attempt, 'DEFERRED', *attempt, 'DEFERRED', *attempt, 'DONE'])
        self.assertEqual(self.query("SELECT json_extract(details_json,'$.attempt') FROM job_history "
                                    "WHERE job_id=? AND event_code<>'CREATED' ORDER BY history_id", (job,)),
                         [(1,)] * 4 + [(2,)] * 4 + [(3,)] * 4)

    async def test_same_wait_with_new_facts_is_recorded(self):
        job = await self.run_polls([
            Outcome('defer', 'JOB_WAITING', delay_ms=1000, facts={'blocking_job_id': 4}),
            Outcome('defer', 'JOB_WAITING', delay_ms=1000, facts={'blocking_job_id': 5}),
        ])
        self.assertEqual(self.events(job).count('DEFERRED'), 2)

    async def test_no_step_lines_at_debug(self):
        from src.anime.diagnostics import log_history
        import logging
        job = await self.run_polls([Outcome('defer', 'TORRENT_DOWNLOAD_WAITING', delay_ms=1000,
                                            activity_code='WAITING_FOR_DOWNLOAD')])
        logger = logging.getLogger('quiet-poll-test'); logger.setLevel(logging.DEBUG)
        db = sqlite3.connect(self.database)
        from src.anime.history import HistoryRecord
        rows = db.execute("SELECT history_id,event_code,reason_code,component,occurred_at_ms,job_id,details_json,"
                          "handler_code,handler_label,invocation_id FROM job_history WHERE job_id=? AND event_code IN "
                          "('CLAIMED','HANDLER_STARTED','HANDLER_COMPLETED')", (job,)).fetchall()
        db.close()
        self.assertEqual(len(rows), 3)
        with self.assertNoLogs(logger, logging.DEBUG):
            for r in rows:
                log_history(logger, HistoryRecord(r[0], 'job', r[1], r[2], r[3], r[4], None, r[5], None, r[6],
                                                  handler_code=r[7], handler_label=r[8], invocation_id=r[9]))


if __name__ == '__main__':
    unittest.main()
