"""Offline cases for `adm report job` diagnostics. Read-only; no services."""
import asyncio
import json
import sqlite3
import unittest

from src.anime.job_diagnostics import diagnose
from src.anime.job_report import report
from src.anime.storage.sqlite import connect, enqueue_job, transaction
from src.anime.tests import test_episode_reservations as fixture



class JobReportDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.EpisodeReservationTests.asyncSetUp
    rows = fixture.EpisodeReservationTests.rows
    source = fixture.EpisodeReservationTests.source
    worker = fixture.EpisodeReservationTests.worker

    async def held(self):
        """Two releases of one episode through the real worker: one owner, one held."""
        self.source(10); self.source(10)
        worker = self.worker()
        await worker.attempt(); await worker.attempt()
        await asyncio.gather(worker.attempt(), worker.attempt())
        (owner,), = self.rows("SELECT owner_job_id FROM episode_reservations")
        (held,), = self.rows("SELECT job_id FROM jobs WHERE kind='RESERVE_EPISODE' AND state='ON_HOLD'")
        return owner, held

    def raw(self, sql, args=()):
        db = sqlite3.connect(self.database)  # Foreign keys off: synthetic states only.
        try:
            with db: db.execute(sql, args)
        finally: db.close()

    def text(self, job_id):
        db = connect(self.database)
        try:
            before = db.total_changes
            lines = []; report(db, job_id, out=lines.append)
            self.assertEqual(db.total_changes, before, 'report must be read-only')
            return '\n'.join(lines)
        finally: db.close()

    def diag(self, job_id):
        db = connect(self.database)
        try: return diagnose(db, job_id)
        finally: db.close()

    def op(self, job_id):
        return self.rows('SELECT operation_id FROM job_operation_members WHERE job_id=?', (job_id,))[0][0]

    def add_member(self, operation, state, reason=None, kind='PREPARE_ACQUISITION'):
        db = connect(self.database)
        try:
            with transaction(db):
                job = enqueue_job(db, kind=kind, payload={}, component='worker', available_at_ms=0)
        finally: db.close()
        self.raw('UPDATE job_operation_members SET operation_id=? WHERE job_id=?', (operation, job))
        self.raw('UPDATE jobs SET state=?,last_reason_code=?,attempt_count=1 WHERE job_id=?', (state, reason, job))
        return job

    # 1
    async def test_hold_with_valid_owner_in_progress(self):
        owner, held = await self.held()
        live = self.add_member(self.op(owner), 'RETRY_WAIT', 'TORRENT_DOWNLOAD_IN_PROGRESS')
        d = self.diag(held)
        conflict, = d['reservation']['conflicts']
        self.assertEqual((conflict['owner_job_id'], conflict['validity']), (owner, 'VALID'))
        self.assertIn(f'Job #{live}', conflict['detail'])
        self.assertTrue(d['state']['automatic'].startswith('Yes'))
        self.assertFalse(d['state']['suspected_stranded'])
        self.assertEqual(d['dependencies'][0]['type'], 'EPISODE_RESERVATION')
        text = self.text(held)
        self.assertIn(f'Blocking reservation #{conflict["reservation_id"]} ACTIVE', text)
        self.assertIn(f'waiting for reservation owned by job #{owner}', text)
        self.assertIn('=== History', text)  # Existing sections preserved.

    # 2
    async def test_hold_with_completed_owner_that_stopped_is_flagged_not_orphaned(self):
        owner, held = await self.held()  # Owner DONE, no acquisition installed: pipeline ends there.
        d = self.diag(held)
        conflict, = d['reservation']['conflicts']
        self.assertEqual(conflict['validity'], 'VALID')
        self.assertIn('Suspected stranded owner', conflict['detail'])
        self.assertNotIn('orphan', conflict['detail'].lower())
        self.assertTrue(d['state']['suspected_stranded'])
        self.assertIn(f'Operation #{self.op(owner)}', d['state']['manual'])

    async def test_hold_with_failed_owner_names_the_stopped_job(self):
        owner, held = await self.held()
        stuck = self.add_member(self.op(owner), 'NEEDS_REVIEW', 'JOB_RETRY_EXHAUSTED')
        conflict, = self.diag(held)['reservation']['conflicts']
        self.assertIn(f'Job #{stuck}', conflict['detail'])
        self.assertIn(f'resolve Job #{stuck}', conflict['manual'])
        self.assertIn(f'Job #{stuck}', ' '.join(self.diag(held)['chain']))

    async def test_released_reservation_without_wake_is_explained(self):
        owner, held = await self.held()
        self.raw("UPDATE episode_reservations SET state='RELEASED',ended_at_ms=1,ended_by_job_id=?", (owner,))
        self.raw("UPDATE job_operations SET state='STAGED' WHERE operation_id=?", (self.op(held),))
        r = self.diag(held)['reservation']
        self.assertEqual(r['conflicts'], [])
        self.assertIn('was released', r['finding'])
        self.assertFalse(r['wake_checks'][0]['ok'])  # Own operation is not ACTIVE.

    # 3
    async def test_reservation_owner_missing(self):
        owner, held = await self.held()
        self.raw('DELETE FROM job_operation_members WHERE job_id=?', (owner,))
        self.raw('DELETE FROM job_history WHERE job_id=?', (owner,))
        self.raw('DELETE FROM jobs WHERE job_id=?', (owner,))
        conflict, = self.diag(held)['reservation']['conflicts']
        self.assertEqual(conflict['validity'], 'UNCERTAIN')
        self.assertIn('not found', conflict['detail'])
        self.assertIn('not found', self.text(held))

    # 4
    async def test_job_waiting_for_a_download_prerequisite(self):
        owner, held = await self.held()
        job = self.add_member(self.op(owner), 'RETRY_WAIT', 'TORRENT_DOWNLOAD_IN_PROGRESS', kind='DOWNLOAD_TORRENT')
        self.raw("UPDATE jobs SET activity_code='WAITING_FOR_DOWNLOAD' WHERE job_id=?", (job,))
        d = self.diag(job)
        self.assertEqual(d['dependencies'][0]['type'], 'EXTERNAL_DOWNLOAD')
        self.assertIn('download', d['state']['waiting_for'])
        self.assertTrue(d['state']['automatic'].startswith('Yes'))

    # 5
    async def test_needs_review_with_exhausted_retries(self):
        owner, held = await self.held()
        job = self.add_member(self.op(owner), 'NEEDS_REVIEW', 'JOB_RETRY_EXHAUSTED')
        db = connect(self.database)
        try:
            with transaction(db):
                for _ in range(3):
                    db.execute("INSERT INTO job_history(job_id,occurred_at_ms,event_code,reason_code,details_json,component) "
                               "VALUES(?,1,'RETRY_SCHEDULED','SERVICE_UNAVAILABLE','{\"attempt\":1}','worker')", (job,))
        finally: db.close()
        d = self.diag(job)
        self.assertEqual(d['recovery']['retries_in_window'], 3)
        self.assertIn('used up', d['state']['doing'])
        self.assertTrue(d['state']['manual'].startswith('Yes'))

    # 6
    async def test_completed_step_without_successor_reports_missing_next_step(self):
        owner, held = await self.held()
        nxt = self.diag(owner)['next_step']
        self.assertEqual(nxt['status'], 'MISSING')
        self.assertIn(f'Job #{owner}', nxt['detail'])
        self.assertIn('No download or staging records', self.text(owner))

    # 7
    async def test_interrupted_attempt_is_counted(self):
        owner, held = await self.held()
        self.raw("INSERT INTO job_history(job_id,occurred_at_ms,event_code,component,details_json,handler_code,"
                 "handler_label,invocation_id) VALUES(?,5,'HANDLER_STARTED','worker','{\"attempt\":2}','X','x',?)",
                 (owner, 'a' * 32))
        self.raw("INSERT INTO job_history(job_id,occurred_at_ms,event_code,reason_code,component) "
                 "VALUES(?,6,'LEASE_EXPIRED','JOB_LEASE_EXPIRED','worker')", (owner,))
        rec = self.diag(owner)['recovery']
        self.assertEqual(len(rec['interrupted_attempts']), 1)
        self.assertEqual(rec['lease_expired_events'], 1)

    # 8
    async def test_operation_groups_jobs_by_status(self):
        owner, held = await self.held()
        operation = self.op(owner)
        a = self.add_member(operation, 'PENDING'); b = self.add_member(operation, 'NEEDS_REVIEW', 'JOB_RETRY_EXHAUSTED')
        op = self.diag(owner)['operation']
        self.assertIn(owner, op['groups']['complete'])
        self.assertIn(a, op['groups']['waiting']); self.assertIn(b, op['groups']['intervention'])
        self.assertTrue(op['progress'].startswith('Yes'))

    # 9
    async def test_missing_diagnostic_records_say_not_recorded(self):
        owner, held = await self.held()
        self.raw('DELETE FROM user_evaluation_checks')
        r = self.diag(held)['reservation']
        self.assertIsNone(r['episode'])
        self.assertTrue(any('not recorded' in n for n in r['limitations']))
        self.assertIn('Episode: Not recorded', self.text(held))

    # 10
    async def test_normal_completed_job_keeps_existing_report(self):
        owner, held = await self.held()
        d = self.diag(owner)
        self.assertEqual((d['state']['manual'], d['state']['suspected_stranded']), ('No.', False))
        text = self.text(owner)
        for section in (f'=== Job #{owner}', 'Payload:', '=== Operation #', '=== History', '=== Records and paths',
                        '=== Paths on disk', '=== Current state', 'Episode reservation:'):
            self.assertIn(section, text)
        json.dumps(d, default=str)  # JSON mode stays serializable.

    async def test_report_displays_observed_metadata_not_a_generic_wait_only(self):
        owner, _ = await self.held()
        observation = {
            'phase':'poll','initial_state':'pausedDL',
            'last_active_state':'metaDL','stopped_state':'pausedDL',
            'polls':24,'max_files':0,'resumed':True,
        }
        self.raw(
            "INSERT INTO job_history(job_id,occurred_at_ms,event_code,reason_code,"
            "details_json,component) VALUES(?,6,'DEFERRED',"
            "'TORRENT_METADATA_WAITING',?,'worker')",
            (owner,json.dumps({'attempt':2,'metadata_evidence':observation})))
        text=self.text(owner)
        self.assertIn('metadata observation (this attempt)',text)
        self.assertIn('last_active=metaDL',text)
        self.assertIn('stopped=pausedDL',text)
        self.assertIn('polls=24',text)
        self.assertIn('max_files=0',text)

    async def test_unknown_job(self):
        self.assertIsNone(self.diag(99999))
        self.assertIn('does not exist', self.text(99999))


if __name__ == '__main__':
    unittest.main()
