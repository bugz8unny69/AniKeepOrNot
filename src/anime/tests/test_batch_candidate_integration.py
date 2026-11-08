"""Integration: prior owned torrents, cancellation without resources, repeated observations."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from src.anime.acquisition.existing_metadata import ExistingMetadata
from src.anime.acquisition.metadata import ObservedBatchInventory, TorrentFile
from src.anime.acquisition.metainfo_source import make_batch_reader
from src.anime.cancellation_service import CancellationService
from src.anime.storage.batch_candidates import coordinate_release
from src.anime.storage.cancellation import CancellationStore
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import connect, enqueue_job, transaction
from src.anime.tests import test_api_v1 as v1
from src.anime.tests.test_batch_candidate_controller import make_series_json
from src.anime.tests.test_torrent_submission import Secrets

AID, TID = 16999, 393400
MARKER = '9' * 32
FILES = (TorrentFile(0, 'Cyberpunk/E01.mkv', 1_000_000), TorrentFile(1, 'Cyberpunk/E02.mkv', 1_000_000))


def digest(n):
    return f'{n:040x}'


class Recorder:
    """qBittorrent for cleanup: deletion is recorded, never real."""
    calls = []

    def __init__(self, *args):
        pass

    def login(self):
        Recorder.calls.append(('login',))

    def delete_owned(self, info_hash, marker, save_path):
        Recorder.calls.append(('delete_owned', info_hash, marker, save_path))
        return 'deleted'

    def __getattr__(self, name):
        raise AssertionError(f'unexpected qBittorrent call during cleanup: {name}')


class Base(unittest.IsolatedAsyncioTestCase):
    asyncSetUp_api = v1.ApiV1Tests.asyncSetUp
    request = v1.ApiV1Tests.request

    async def asyncSetUp(self):
        await self.asyncSetUp_api()
        self.store.update({'qbittorrent': {'base_url': 'http://qb:8080'},
                           'storage': {'download_root': str(self.root / 'downloads')}},
                          expected_revision=self.store.read().revision)
        Recorder.calls = []

    def sql(self, statement, args=()):
        db = connect(self.database)
        try:
            with db:
                return db.execute(statement, args).fetchall()
        finally:
            db.close()

    def deliver(self, n, title=None, *, hash_n=None, seeders=10):
        """An identified season pack and its VERIFY_RELEASE job (not yet coordinated)."""
        title = title or f'[Group{n}] Cyberpunk Edgerunners S01 (Batch) [1080p]'
        db = connect(self.database)
        try:
            with transaction(db):
                if not db.execute("SELECT 1 FROM identified_anime WHERE anidb_id=?", (AID,)).fetchone():
                    db.execute("INSERT INTO identified_anime(anidb_id,tvdb_series_id,title) "
                               "VALUES(?,?,'Cyberpunk: Edgerunners')", (AID, TID))
                db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,state) "
                           "VALUES(?, 'autobrr', ?, ?, 'IDENTIFIED')",
                           (n, json.dumps({'release_name': title, 'torrent_infohash': digest(hash_n or n),
                                           'seeders': seeders}).encode(), 1000 + n))
                db.execute("INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,validated_at_ms) "
                           "VALUES(?,?,2,?,?)", (n, AID, make_series_json(), 1000 + n))
                job = enqueue_job(db, component='intake', kind='VERIFY_RELEASE', payload={'anidb_id': AID},
                                  event_id=n, available_at_ms=0)
        finally:
            db.close()
        return job, title

    def coordinate(self, n, job, title, *, hash_n=None, observations=None):
        db = connect(self.database)
        try:
            with transaction(db):
                return coordinate_release(db, anidb_id=AID, tvdb_series_id=TID, season=1,
                                          info_hash=digest(hash_n or n), title=title, event_id=n, job_id=job,
                                          observations=observations)
        finally:
            db.close()

    def candidates(self):
        return {h: (s, m) for h, s, m in self.sql("SELECT info_hash,state,has_metadata FROM batch_candidates")}

    def cleanup(self, job):
        return CancellationService(self.store.read(), Secrets(), client_factory=Recorder).cleanup(job)


class PriorOwnedTorrentTests(Base):
    def admit(self, job, n):
        """A torrent this application admitted earlier: journal-owned, stopped, file list known."""
        folder = self.root / 'downloads' / f'ad-{MARKER}'
        folder.mkdir(parents=True)
        (folder / 'partial.mkv').write_bytes(b'x')
        inventory = ObservedBatchInventory(digest(n), FILES).json()
        self.sql("INSERT INTO batch_metadata_admissions(event_id,verification_job_id,info_hash,ownership_marker,"
                 "base_url,save_path,category,timeout_seconds,api_major,state,inventory_json,created_at_ms,"
                 "updated_at_ms) VALUES(?,?,?,?, 'http://qb:8080', ?, 'anime', 30, 4, 'OWNED_STOPPED', ?, 1, 1)",
                 (n, job, digest(n), MARKER, str(folder), inventory))
        return folder

    async def test_a_prior_owned_torrent_is_evidence_reused_by_its_journal_and_deleted_only_by_cleanup(self):
        x_job, x_title = self.deliver(1, seeders=1)
        y_job, y_title = self.deliver(2, seeders=99)       # More seeders, but no known file list.
        folder = self.admit(x_job, 1)
        self.assertEqual(self.coordinate(2, y_job, y_title).action, 'POOLED')
        # The admitted inventory is evidence: X ranks first despite fewer seeders.
        self.assertEqual(self.candidates(), {digest(1): ('READY', 1), digest(2): ('READY', 0)})
        self.assertEqual(self.coordinate(1, x_job, x_title).action, 'PROCEED')

        # Evaluation reads X through its own journal, never the external shortcut.
        class Qb:
            metadata_strategy, major = 'legacy', 4

            def __init__(self, *args):
                pass

            def login(self):
                pass

            def __getattr__(self, name):
                raise AssertionError(f'not via the shared client: {name}')

        class OwnedBatch:
            metadata_strategy, calls = 'legacy', []

            def __init__(self, options, credentials):
                pass

            def login(self):
                OwnedBatch.calls.append('login')

            def inspect_owned_batch_metadata(self, d, marker, root, *, require_stopped=True):
                OwnedBatch.calls.append(('inspect_owned', d, marker, require_stopped))
                return ObservedBatchInventory(d, FILES)

            def __getattr__(self, name):
                raise AssertionError(f'qBittorrent mutation attempted: {name}')
        reader = make_batch_reader(client_factory=Qb, secrets=Secrets())
        with mock.patch('src.anime.batch.qb_client.BatchClient', OwnedBatch):
            inventory = await reader(digest(1), self.store.read(), threading.Event(),
                                     claim=SimpleNamespace(job_id=x_job), event_id=1, evaluation=True)
        self.assertEqual(inventory, ObservedBatchInventory(digest(1), FILES))
        self.assertEqual(OwnedBatch.calls, ['login', ('inspect_owned', digest(1), MARKER, True)])

        # Cancel X: it owns a torrent, so a cleanup job does the deleting.
        status, result, _ = await self.request('POST', f'/api/v1/jobs/{x_job}/cancel', {}, token=v1.ADMIN)
        self.assertEqual(status, 202)
        self.assertTrue(folder.exists())                          # Nothing deleted by the API itself.
        self.assertEqual(self.cleanup(x_job)['cleanup_state'], 'COMPLETE')
        self.assertEqual(Recorder.calls, [('login',), ('delete_owned', digest(1), MARKER, str(folder))])
        self.assertFalse(folder.exists())                         # Only the exact ad-<marker> folder.
        self.assertEqual(self.sql("SELECT state FROM batch_metadata_admissions"), [('RELEASED',)])
        self.assertEqual(self.candidates(), {digest(1): ('CANCELED', 1), digest(2): ('ACTIVE', 0)})
        self.assertNotIn(digest(2), [c[1] for c in Recorder.calls if len(c) > 1])   # Y never touched.


class CancellationWithoutResourcesTests(Base):
    async def test_running_work_with_nothing_to_delete_is_cleaned_without_qbittorrent_and_moves_on(self):
        a_job, a_title = self.deliver(1)
        b_job, b_title = self.deliver(2)
        self.assertEqual(self.coordinate(1, a_job, a_title).action, 'PROCEED')
        self.coordinate(2, b_job, b_title)
        self.sql("UPDATE jobs SET state='DONE' WHERE job_id=?", (b_job,))
        claim, _ = JobQueue(self.database).claim()                # A is running.
        status, _, _ = await self.request('POST', f'/api/v1/jobs/{a_job}/cancel', {}, token=v1.ADMIN)
        self.assertEqual(status, 202)                              # Running: cleanup waits for it.
        CancellationStore(self.database).acknowledge(claim)
        self.assertEqual(self.cleanup(a_job)['cleanup_state'], 'COMPLETE')
        self.assertEqual(Recorder.calls, [])                       # Nothing owned: qBittorrent not contacted.
        self.assertEqual(self.candidates(), {digest(1): ('CANCELED', 0), digest(2): ('ACTIVE', 0)})

    async def test_an_immediate_cancel_and_a_cleanup_job_end_in_the_same_state(self):
        # Immediate path: nothing running and nothing to delete.
        a_job, a_title = self.deliver(1)
        b_job, b_title = self.deliver(2)
        self.coordinate(1, a_job, a_title)
        self.coordinate(2, b_job, b_title)
        self.sql("UPDATE jobs SET state='DONE' WHERE job_id=?", (b_job,))
        status, result, _ = await self.request('POST', f'/api/v1/jobs/{a_job}/cancel', {}, token=v1.ADMIN)
        self.assertEqual((status, result['data']['completed']), (200, True))
        self.assertEqual(self.sql("SELECT disposition,reason_code FROM batch_candidate_stops"),
                         [('CANCELED', 'JOB_CANCELED')])
        self.assertEqual(self.candidates(), {digest(1): ('CANCELED', 0), digest(2): ('ACTIVE', 0)})
        self.assertEqual(self.sql("SELECT state,cleanup_state FROM job_operations o JOIN job_operation_members m "
                                  "USING(operation_id) WHERE m.job_id=?", (a_job,)), [('CANCELED', 'COMPLETE')])


class RepeatedObservationTests(Base):
    def observed(self, n):
        return {digest(n): ExistingMetadata(ObservedBatchInventory(digest(n), FILES), 'stoppedDL', 'tv', None)}

    async def test_the_same_release_seen_again_upgrades_evidence_once_and_keeps_its_state(self):
        a_job, a_title = self.deliver(1, seeders=99)
        r1_job, r_title = self.deliver(2, '[GroupR] Cyberpunk Edgerunners S01 (Batch) [1080p]', hash_n=7, seeders=5)
        self.assertEqual(self.coordinate(1, a_job, a_title).action, 'PROCEED')      # A is active.
        self.coordinate(2, r1_job, r_title, hash_n=7)
        self.assertEqual(self.candidates()[digest(7)], ('READY', 0))
        # R is announced again; this time qBittorrent already has its file list.
        r2_job, _ = self.deliver(3, r_title, hash_n=7, seeders=5)
        self.assertEqual(self.coordinate(3, r2_job, r_title, hash_n=7, observations=self.observed(7)).action,
                         'POOLED')                                                    # A stays active.
        self.assertEqual(self.candidates()[digest(7)], ('READY', 1))
        row = self.sql("SELECT candidate_id,rank,updated_at_ms,metadata_json FROM batch_candidates WHERE info_hash=?",
                       (digest(7),))
        # Seen a third time with the same evidence: nothing about it changes.
        r3_job, _ = self.deliver(4, r_title, hash_n=7, seeders=5)
        self.coordinate(4, r3_job, r_title, hash_n=7, observations=self.observed(7))
        self.assertEqual(self.sql("SELECT candidate_id,rank,updated_at_ms,metadata_json FROM batch_candidates "
                                  "WHERE info_hash=?", (digest(7),)), row)
        self.assertEqual(self.sql("SELECT count(*) FROM batch_candidates WHERE info_hash=?", (digest(7),)), [(1,)])
        self.assertEqual(self.sql("SELECT count(*) FROM batch_candidate_events WHERE info_hash=?", (digest(7),)),
                         [(3,)])                                                       # Every delivery recorded.
        self.assertEqual(self.sql("SELECT state FROM batch_candidates WHERE state='ACTIVE'"), [('ACTIVE',)])

    async def test_a_failed_release_seen_again_stays_failed(self):
        a_job, a_title = self.deliver(1)
        self.coordinate(1, a_job, a_title)
        self.sql("UPDATE batch_candidates SET state='FAILED', reason_code='TORRENT_METADATA_TIMEOUT'")
        self.sql("UPDATE batch_candidate_scopes SET active_candidate_id=NULL")
        again_job, _ = self.deliver(2, a_title, hash_n=1)
        self.assertEqual(self.coordinate(2, again_job, a_title, hash_n=1, observations=self.observed(1)).action,
                         'POOLED')
        self.assertEqual(self.sql("SELECT state,reason_code,has_metadata FROM batch_candidates"),
                         [('FAILED', 'TORRENT_METADATA_TIMEOUT', 1)])   # Evidence kept; never retried this cycle.


if __name__ == '__main__':
    unittest.main()
