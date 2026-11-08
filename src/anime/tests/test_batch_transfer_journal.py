"""Integration regression: BatchTransferJournal.select() on a real claimed TRANSFER_BATCH.

The SQLite state is built through the real worker and journals:
VERIFY_RELEASE -> EVALUATE_BATCH -> RESERVE_BATCH (worker),
SUBMIT_BATCH (BatchSubmissionJournal to OWNED_STOPPED, then the real
registration's persist/route through JobQueue.finish), and finally a real
TRANSFER_BATCH lease. No qBittorrent, network or production database.

Regression: select() supplied 8 values for 9 INSERT placeholders (the
fingerprint was omitted), so the first batch transfer selection always raised.
"""
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from src.anime.acquisition.metadata import BatchInventory, TorrentFile
from src.anime.acquisition.settings import DownloadOptions
from src.anime.acquisition.submission import Observation
from src.anime.acquisition.transfers import TransferConflict, TransferSnapshot
from src.anime.batch.probe_transfer import selection_fingerprint
from src.anime.batch.provider_worker import BatchVerificationCompletion
from src.anime.batch.verification import verify_batch_files
from src.anime.configuration import ConfigurationStore
from src.anime.job_contracts import Outcome, Policy, Registry
from src.anime.jobs.evaluate_batch_job import registration as batch_registration
from src.anime.jobs.reserve_batch_job import registration as reserve_batch_registration
from src.anime.jobs.submit_batch_job import registration as submit_batch_registration
from src.anime.jobs.transfer_batch_job import registration as transfer_batch_registration
from src.anime.jobs.verify_release_job import registration as release_registration
from src.anime.providers.http import Credentials
from src.anime.storage.batch_submission_journal import BatchSubmissionJournal
from src.anime.storage.batch_transfer_journal import (
    BatchTransferJournal, BatchTransferRecord,
)
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import (
    accept_event_and_job, connect, enqueue_job, initialize, save_identity, transaction,
)
from src.anime.worker import Worker

FILE_SIZE = 100000000
PIECE_SIZE = 4000000  # Divides FILE_SIZE exactly: no piece spans two files.
PIECES_PER_FILE = FILE_SIZE // PIECE_SIZE
OPTIONS = DownloadOptions('http://localhost:8080', '/tmp/nextgen', 'trial', 10)


class FakeProvider:
    def episodes(self, aid):
        return {i: 1000 + i for i in range(1, 13)}

    def mapping_xml(self):
        return b'<anime-list><anime anidbid="123" tvdbid="456" defaulttvdbseason="1"/></anime-list>'

    def episode_page(self, tid, page, secret):
        return tuple((2000 + i, 1, i) for i in range(1, 13)), False


async def _unused_metainfo(*args):
    raise AssertionError('handler execution is not part of this test')


class _NoSecrets:
    def qbittorrent_credentials(self):
        raise AssertionError('handler execution is not part of this test')


class Clock:
    def __init__(self):
        # Ahead of wall time so jobs enqueued with the real clock are claimable.
        self.value = time.time_ns() // 1000000 + 1000

    def __call__(self):
        return self.value


class BatchTransferJournalSelectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.database = root / 'db.sqlite'
        self.store = ConfigurationStore(root)
        self.startup = self.store.initialize(
            {'storage': {'database_path': str(self.database)}}).snapshot
        db = connect(self.database)
        try:
            initialize(db)
            self.event, _ = accept_event_and_job(
                db, 'test',
                b'{"release_name":"Shiki (S01 + OVA)","torrent_infohash":"' + b'a' * 40 + b'"}',
                kind='VERIFY_RELEASE', payload={'anidb_id': 123})
            save_identity(db, self.event, {'schema_version': 2, 'anime': {
                'anidb_id': 123, 'tvdb_series_id': 456, 'title': 'Shiki'}})
        finally:
            db.close()
        files = tuple(TorrentFile(i, f'Shiki/Season 01/Shiki S01E{i + 1:02d}.mkv', FILE_SIZE)
                      for i in range(12))
        self.inventory = BatchInventory('a' * 40, 'b' * 64, files + (
            TorrentFile(12, 'Shiki/OVA/Shiki OVA 01.mkv', FILE_SIZE),))
        verified = verify_batch_files(
            self.inventory, client=FakeProvider(), credentials=Credentials('secret'),
            anidb_id=123, tvdb_series_id=456, season=1)

        async def verify(source, *args):
            return BatchVerificationCompletion(source, self.inventory, verified)
        worker = Worker(self.store, self.startup, Registry((
            release_registration(verify, batch_kind='EVALUATE_BATCH'),
            batch_registration(reservation_kind='RESERVE_BATCH'),
            reserve_batch_registration())), policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        for _ in range(3):
            await worker.attempt()
        self.assertEqual(self.rows('SELECT kind,state FROM jobs ORDER BY job_id'), [
            ('VERIFY_RELEASE', 'DONE'), ('EVALUATE_BATCH', 'DONE'), ('RESERVE_BATCH', 'DONE')])

        self.clock = Clock()
        self.reservation = self.rows("SELECT job_id FROM jobs WHERE kind='RESERVE_BATCH'")[0][0]
        self.submission = self._owned_stopped_submission()
        self.transfer_claim = self._claim_transfer()

    def rows(self, sql, args=()):
        db = connect(self.database)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def queue(self):
        return JobQueue(self.database, policy=Policy(lease_ms=30000), clock=self.clock)

    def _owned_stopped_submission(self):
        """A real SUBMIT_BATCH lease, confirmed OWNED_STOPPED and finished through its registration."""
        db = connect(self.database)
        try:
            with transaction(db):
                operation = db.execute(
                    'SELECT operation_id FROM job_operation_members WHERE job_id=?',
                    (self.reservation,)).fetchone()[0]
                enqueue_job(db, kind='SUBMIT_BATCH',
                            payload={'reservation_job_id': self.reservation},
                            component='worker', event_id=self.event,
                            created_by_job_id=self.reservation, operation_id=operation)
        finally:
            db.close()
        queue = self.queue()
        claim, _ = queue.claim()
        self.assertEqual(claim.kind, 'SUBMIT_BATCH')
        journal = BatchSubmissionJournal(self.database)
        candidate, _ = journal.load(claim, self.reservation, self.inventory)
        intent, permit, _ = journal.authorize(
            claim, candidate, self.inventory, OPTIONS, 5, False, 1)
        self.assertIsNotNone(permit)
        stopped = Observation(intent.inventory.info_hash, 'stoppedDL',
                              intent.options.download_root, intent.options.category,
                              ('ad-' + intent.marker,), False, intent.inventory.files)
        state, _, _ = journal.observe(claim, intent, stopped)
        self.assertEqual(state, 'OWNED_STOPPED')
        self.submission_intent = intent
        # The real registration validates provenance (persist) and creates the
        # TRANSFER_BATCH successor (route) exactly as the worker would.
        registry = Registry((
            submit_batch_registration(_unused_metainfo, _NoSecrets(),
                                      transfer_kind='TRANSFER_BATCH'),
            transfer_batch_registration(_unused_metainfo, _NoSecrets())))
        spec = registry.entries['SUBMIT_BATCH']
        invocation = uuid.uuid4().hex
        queue.start(claim, spec, invocation, registry)
        queue.finish(claim, Outcome(value={'reservation_job_id': self.reservation,
                                           'submission_job_id': claim.job_id}),
                     spec=spec, invocation=invocation, registry=registry)
        return claim.job_id

    def _claim_transfer(self):
        claim, _ = self.queue().claim()
        self.assertIsNotNone(claim)
        self.assertEqual(claim.kind, 'TRANSFER_BATCH')
        self.assertEqual(claim.data(), {'submission_job_id': self.submission})
        return claim

    def snapshot(self, intent, *, priorities=None, state='stoppedDL'):
        count = len(intent.inventory.files)
        observed = Observation(intent.inventory.info_hash, state,
                               intent.options.download_root, intent.options.category,
                               ('ad-' + intent.marker,), False, intent.inventory.files)
        ranges = tuple((i * PIECES_PER_FILE, (i + 1) * PIECES_PER_FILE - 1) for i in range(count))
        return TransferSnapshot(observed, priorities or (1,) * count, (0.0,) * count,
                                PIECE_SIZE, ranges)

    def transfer_rows(self):
        return self.rows(
            'SELECT plan_id,transfer_job_id,transfer_attempt,selected_index,piece_size,'
            'fingerprint,phase,reason_code,selected_progress,created_at_ms,updated_at_ms,'
            'start_job_id,start_attempt FROM batch_torrent_transfers')

    def selection_history(self):
        return self.rows(
            "SELECT job_id,json_extract(details_json,'$.attempt') FROM job_history "
            "WHERE event_code='TORRENT_SELECTION_RECORDED' ORDER BY history_id")

    def test_select_stores_every_insert_parameter_and_returns_one_time_permit(self):
        claim = self.transfer_claim
        journal = BatchTransferJournal(self.database, clock=self.clock)
        intent, prior = journal.load(claim, self.submission, self.inventory)
        self.assertEqual(intent.state, 'OWNED_STOPPED')
        self.assertEqual((intent.fingerprint, intent.marker, intent.candidate),
                         (self.submission_intent.fingerprint, self.submission_intent.marker,
                          self.submission_intent.candidate))
        self.assertIsNone(prior)
        snapshot = self.snapshot(intent)
        expected_fingerprint = selection_fingerprint(intent, PIECE_SIZE)

        record, permit, receipts = journal.select(claim, intent, snapshot, self.submission)

        # All nine bound INSERT values were stored (the two phase/reason
        # literals are fixed SQL text, the start fields stay NULL).
        rows = self.transfer_rows()
        self.assertEqual(len(rows), 1)
        (plan_id, job_id, attempt, index, piece_size, fingerprint, phase, reason,
         progress, created, updated, start_job, start_attempt) = rows[0]
        self.assertEqual(plan_id, intent.candidate.plan_id)
        self.assertEqual((job_id, attempt), (claim.job_id, claim.attempt))
        self.assertEqual(index, intent.candidate.probe_index)
        self.assertEqual(piece_size, PIECE_SIZE)
        self.assertEqual(fingerprint, expected_fingerprint)
        self.assertEqual(progress, snapshot.progress[index])
        self.assertEqual((created, updated), (self.clock.value, self.clock.value))
        self.assertEqual((phase, reason), ('SELECT_UNCERTAIN', 'TORRENT_SELECTION_UNCERTAIN'))
        self.assertEqual((start_job, start_attempt), (None, None))
        self.assertEqual(self.rows('PRAGMA foreign_key_check'), [])

        # The journal returns the stored record and a permit bound to it.
        self.assertEqual(record, BatchTransferRecord(
            expected_fingerprint, 'SELECT_UNCERTAIN', PIECE_SIZE, index,
            claim.job_id, claim.attempt, None, None, 0.0, 'TORRENT_SELECTION_UNCERTAIN'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(self.selection_history(), [(claim.job_id, claim.attempt)])
        self.assertEqual(permit.fingerprint, expected_fingerprint)
        with self.assertRaises(TransferConflict):
            permit.take('0' * 64, 'skip')  # Wrong fingerprint is refused.
        with self.assertRaises(TransferConflict):
            permit.take(expected_fingerprint, 'start')  # Not a selection action.
        for action in ('skip', 'wanted'):
            journal.guard(claim, intent, record, action, self.submission)
            permit.take(expected_fingerprint, action)
            with self.assertRaises(TransferConflict):
                permit.take(expected_fingerprint, action)  # One-time only.

        # Repeating select in the same attempt never issues another permit.
        again, no_permit, no_receipts = journal.select(claim, intent, snapshot, self.submission)
        self.assertEqual(again, record)
        self.assertIsNone(no_permit)
        self.assertEqual(no_receipts, ())
        self.assertEqual(len(self.transfer_rows()), 1)

    def test_restarted_worker_resumes_without_duplicate_selection(self):
        first = self.transfer_claim
        journal = BatchTransferJournal(self.database, clock=self.clock)
        intent, _ = journal.load(first, self.submission, self.inventory)
        record, permit, _ = journal.select(first, intent, self.snapshot(intent), self.submission)
        self.assertIsNotNone(permit)
        # The worker dies after the durable SELECT_UNCERTAIN record, before
        # (or while) applying the selection. The in-memory permit is lost.
        del permit, journal

        # A fresh process cannot use the old lease once it expires.
        self.clock.value += 30001
        recovered = self.queue().recover_expired()
        self.assertTrue(any(r.event_code == 'LEASE_EXPIRED' for r in recovered))
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?', (first.job_id,)),
                         [('RETRY_WAIT',)])
        self.clock.value += 24 * 60 * 60 * 1000
        second = self._claim_transfer()
        self.assertEqual(second.job_id, first.job_id)
        self.assertEqual(second.attempt, first.attempt + 1)

        restarted = BatchTransferJournal(self.database, clock=self.clock)
        intent, stored = restarted.load(second, self.submission, self.inventory)
        self.assertEqual(stored, record)
        # Re-selection only observes the existing record: no new row, no permit.
        again, no_permit, receipts = restarted.select(
            second, intent, self.snapshot(intent), self.submission)
        self.assertEqual(again, record)
        self.assertIsNone(no_permit)
        self.assertEqual(receipts, ())
        self.assertEqual(len(self.transfer_rows()), 1)
        self.assertEqual(self.selection_history(), [(first.job_id, first.attempt)])
        # The new attempt cannot authorize another selection send.
        for action in ('skip', 'wanted'):
            with self.assertRaises(TransferConflict):
                restarted.guard(second, intent, record, action, self.submission)

        # Safe resume: observing the exact applied selection confirms it, and
        # the pipeline continues to a single start permit for this attempt.
        probe = intent.candidate.probe_index
        exact = tuple(1 if i == probe else 0 for i in range(len(intent.inventory.files)))
        selected, _ = restarted.selected(
            second, intent, self.snapshot(intent, priorities=exact), self.submission)
        self.assertEqual(selected.phase, 'SELECTED')
        self.assertEqual(selected.fingerprint, record.fingerprint)
        started, start_permit, _ = restarted.start(
            second, intent, self.snapshot(intent, priorities=exact), self.submission)
        self.assertEqual(started.phase, 'START_UNCERTAIN')
        self.assertEqual((started.start_job_id, started.start_attempt),
                         (second.job_id, second.attempt))
        self.assertIsNotNone(start_permit)
        restarted.guard(second, intent, started, 'start', self.submission)
        start_permit.take(record.fingerprint, 'start')
        with self.assertRaises(TransferConflict):
            start_permit.take(record.fingerprint, 'start')
        self.assertEqual(len(self.transfer_rows()), 1)


if __name__ == '__main__':
    unittest.main()
