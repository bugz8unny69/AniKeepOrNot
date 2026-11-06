"""WI-11/WI-12: candidate coordination is serialized, network-free under the write lock, and evidence-only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from src.nextgen.acquisition.existing_metadata import (
    ExistingMetadata, make_existing_observer, observe_existing_metadata,
)
from src.nextgen.acquisition.metadata import ObservedBatchInventory, TorrentFile
from src.nextgen.acquisition.metainfo_source import MetadataSourceUnavailable
from src.nextgen.acquisition.submission import ClientUnavailable, Observation
from src.nextgen.batch.provider_worker import BatchCandidatePooled, make_batch_verifier
from src.nextgen.configuration import ConfigurationSnapshot
from src.nextgen.providers.http import Credentials, Options
from src.nextgen.release_contracts import ReleaseInput
from src.nextgen.storage.batch_candidates import (
    coordinate_release, get_active_candidate, get_candidate_pool, get_or_create_scope, schedule_winner,
)
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction
from src.nextgen.tests.test_batch_candidate_controller import make_series_json

AID, TID = 16999, 393400
FILES = (TorrentFile(0, 'Cyberpunk/E01.mkv', 1_000_000), TorrentFile(1, 'Cyberpunk/E02.mkv', 1_000_000))


def digest(n):
    return f'{n:040x}'


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'q.sqlite'
        db = sqlite3.connect(self.path)
        initialize(db)
        db.execute("INSERT INTO identified_anime(anidb_id,tvdb_series_id,title) VALUES(?,?,'Cyberpunk: Edgerunners')",
                   (AID, TID))
        db.commit()
        db.close()

    def connect(self, timeout=10):
        db = sqlite3.connect(self.path, timeout=timeout)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def release(self, n, title=None, seeders=10):
        """One identified delivery with its VERIFY_RELEASE job. Returns (event_id, job_id, title)."""
        title = title or f'[Group{n}] Cyberpunk Edgerunners S01 (Batch) [1080p]'
        body = json.dumps({'release_name': title, 'torrent_infohash': digest(n), 'seeders': seeders}).encode()
        db = self.connect()
        try:
            with transaction(db):
                db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,state) "
                           "VALUES(?, 'autobrr', ?, ?, 'IDENTIFIED')", (n, body, 1000 + n))
                db.execute("INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,validated_at_ms) "
                           "VALUES(?,?,2,?,?)", (n, AID, make_series_json(), 1000 + n))
                job = enqueue_job(db, component='intake', kind='VERIFY_RELEASE', payload={'anidb_id': AID},
                                  event_id=n, available_at_ms=0)
        finally:
            db.close()
        return n, job, title

    def coordinate(self, n, job, title, *, observations=None, timeout=10):
        db = self.connect(timeout)
        try:
            with transaction(db):
                return coordinate_release(db, anidb_id=AID, tvdb_series_id=TID, season=1, info_hash=digest(n),
                                          title=title, event_id=n, job_id=job, observations=observations)
        finally:
            db.close()

    def rows(self, sql, args=()):
        db = self.connect()
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()


class CoordinationTests(Fixture):
    def test_fifty_simultaneous_releases_produce_one_active_acquisition(self):
        arrivals = [self.release(n) for n in range(1, 51)]
        barrier = threading.Barrier(50)
        decisions, errors = [], []

        def arrive(event, job, title):
            barrier.wait()
            try:
                decisions.append(self.coordinate(event, job, title, timeout=30))
            except Exception as exc:  # A lost race must not be hidden either.
                errors.append(exc)
        threads = [threading.Thread(target=arrive, args=a) for a in arrivals]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(d.action == 'PROCEED' for d in decisions), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM batch_candidates WHERE state='ACTIVE'"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM batch_candidates"), [(50,)])
        self.assertEqual(self.rows("SELECT count(*) FROM batch_candidate_scopes"), [(1,)])

    def test_concurrent_arrivals_cannot_activate_two_candidates(self):
        a, b = self.release(1), self.release(2)
        for _ in range(5):
            barrier = threading.Barrier(2)
            results = []

            def arrive(arrival):
                barrier.wait()
                results.append(self.coordinate(*arrival))
            threads = [threading.Thread(target=arrive, args=(x,)) for x in (a, b)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(self.rows("SELECT count(*) FROM batch_candidates WHERE state='ACTIVE'"), [(1,)])
            self.assertEqual(sum(r.action == 'PROCEED' for r in results), 1)

    def test_a_higher_ranked_pooled_candidate_actually_starts(self):
        # X is pooled (READY) and its own verification already finished; then
        # the active candidate is gone. A worse arrival must start X, not idle.
        x = self.release(1, '[Group] Cyberpunk Edgerunners S01 (Batch) [1080p] [Dual Audio]')
        self.coordinate(*x)
        db = self.connect()
        with transaction(db):
            db.execute("UPDATE batch_candidates SET state='READY' WHERE info_hash=?", (digest(1),))
            db.execute("UPDATE batch_candidate_scopes SET active_candidate_id=NULL")
            db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?", (x[1],))
        db.close()
        y = self.release(2)
        decision = self.coordinate(*y)
        self.assertEqual(decision.action, 'POOLED')
        winner = self.rows("SELECT candidate_id FROM batch_candidates WHERE info_hash=?", (digest(1),))[0][0]
        self.assertEqual(decision.winner_candidate_id, winner)
        db = self.connect()
        with transaction(db):
            receipts = schedule_winner(db, decision.scope_id, winner, created_by_job_id=y[1])
        db.close()
        self.assertEqual(len(receipts), 1)
        jobs = self.rows("SELECT job_id,state FROM jobs WHERE kind='VERIFY_RELEASE' AND event_id=1 ORDER BY job_id")
        self.assertEqual([state for _, state in jobs], ['DONE', 'PENDING'])
        self.assertEqual(self.rows("SELECT state FROM batch_candidates WHERE info_hash=?", (digest(1),)), [('ACTIVE',)])
        # The scope's operation is the new job's operation, and running X proceeds.
        new_job = jobs[-1][0]
        op = self.rows("SELECT operation_id FROM job_operation_members WHERE job_id=?", (new_job,))[0][0]
        self.assertEqual(self.rows("SELECT operation_id FROM batch_candidate_scopes"), [(op,)])
        self.assertEqual(self.coordinate(1, new_job, x[2]).action, 'PROCEED')
        # Completing the pooled job twice (a restart) schedules nothing more.
        db = self.connect()
        with transaction(db):
            self.assertEqual(schedule_winner(db, decision.scope_id, winner), ())
        db.close()

    def test_an_open_job_for_the_winner_is_reused_not_duplicated(self):
        x = self.release(1, '[Group] Cyberpunk Edgerunners S01 (Batch) [1080p] [Dual Audio]')
        y = self.release(2)
        db = self.connect()
        with transaction(db):   # X is known to the pool but its job has not run yet.
            scope = get_or_create_scope(db, AID, TID, 1)
            from src.nextgen.storage.batch_candidates import add_candidate
            add_candidate(db, scope.scope_id, digest(1), x[2], source_event_id=1)
        db.close()
        decision = self.coordinate(*y)
        db = self.connect()
        with transaction(db):
            self.assertEqual(schedule_winner(db, decision.scope_id, decision.winner_candidate_id), ())
        db.close()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE event_id=1"), [(1,)])
        self.assertEqual(self.coordinate(*x).action, 'PROCEED')

    def test_restart_preserves_the_active_selection(self):
        a, b = self.release(1), self.release(2)
        self.assertEqual(self.coordinate(*a).action, 'PROCEED')
        self.assertEqual(self.coordinate(*b).action, 'POOLED')
        # A restarted worker re-runs both jobs: the selection is durable.
        self.assertEqual(self.coordinate(*a).action, 'PROCEED')
        self.assertEqual(self.coordinate(*b).action, 'POOLED')
        self.assertEqual(self.rows("SELECT info_hash FROM batch_candidates WHERE state='ACTIVE'"), [(digest(1),)])

    def test_observed_inventories_rank_but_never_change_state_elsewhere(self):
        a, b = self.release(1), self.release(2)
        observed = {digest(2): ExistingMetadata(ObservedBatchInventory(digest(2), FILES), 'stoppedDL', 'anime', None)}
        self.assertEqual(self.coordinate(*a, observations=observed).action, 'POOLED')
        pool = {c.info_hash: c for c in get_candidate_pool(self.connect(), 1)}
        self.assertEqual(pool[digest(2)].has_metadata, 1)
        self.assertIn('qbittorrent_existing', pool[digest(2)].metadata_json)
        self.assertEqual(pool[digest(2)].rank, 1)                 # Metadata-ready ranks first.
        self.assertEqual(self.rows("SELECT count(*) FROM batch_metadata_admissions"), [(0,)])  # Nothing adopted.


class Inspector:
    """A qBittorrent client that records every call; only reads are allowed."""
    def __init__(self, rows):
        self.rows, self.calls = list(rows), []

    def login(self):
        self.calls.append('login')

    def inspect(self, expected, *, include_files=True):
        self.calls.append('inspect')
        return self.rows.pop(0) if self.rows else None

    def __getattr__(self, name):
        raise AssertionError(f'qBittorrent mutation attempted: {name}')


def observation(state='stoppedDL', files=FILES, tags=()):
    return Observation(digest(1), state, '/srv/downloads', 'anime', tags, False, files)


class ExistingMetadataTests(unittest.TestCase):
    def test_a_stable_complete_file_list_is_read_without_mutation(self):
        client = Inspector([observation(tags=('ad-' + 'a' * 32,)), observation(tags=('ad-' + 'a' * 32,))])
        found = observe_existing_metadata(client, digest(1))
        self.assertEqual(found.inventory.files, FILES)
        self.assertEqual(found.ownership_tag, 'ad-' + 'a' * 32)
        self.assertEqual(client.calls, ['inspect', 'inspect'])

    def test_resolving_changing_or_absent_torrents_are_not_evidence(self):
        self.assertIsNone(observe_existing_metadata(Inspector([observation('metaDL')]), digest(1)))
        self.assertIsNone(observe_existing_metadata(Inspector([observation(), observation(files=FILES[:1])]), digest(1)))
        self.assertIsNone(observe_existing_metadata(Inspector([]), digest(1)))
        self.assertIsNone(observe_existing_metadata(Inspector([observation(files=())]), digest(1)))

    def test_qbittorrent_errors_are_not_swallowed(self):
        class Down(Inspector):
            def login(self):
                raise ClientUnavailable()
        config = ConfigurationSnapshot(1, {'storage': {'download_root': '/srv/downloads'},
                                           'qbittorrent': {'base_url': 'http://qb:8080'}})
        secrets = SimpleNamespace(qbittorrent_credentials=lambda: SimpleNamespace(username='u', password='p'))
        observe = make_existing_observer(secrets, client_factory=lambda options, credentials: Down([]))
        with self.assertRaises(ClientUnavailable):
            observe(config, (digest(1),))


class VerifierLockTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_qbittorrent_latency_cannot_hold_the_write_lock(self):
        event, job, title = self.release(1)
        writes = []

        def slow_observer(configuration, digests):
            time.sleep(0.2)                       # A slow qBittorrent...
            other = self.connect(timeout=0)       # ...must not block any writer.
            try:
                other.execute('BEGIN IMMEDIATE')
                other.rollback()
                writes.append('ok')
            finally:
                other.close()
            return {}

        class Stop(Exception):
            pass

        async def reader(*args, **kwargs):
            raise Stop()
        verifier = make_batch_verifier(Options('registeredclient', 1), lambda: Credentials('private'), reader,
                                       client_factory=lambda *a: None, observe_existing=slow_observer)
        source = ReleaseInput(event, 1, AID, TID, json.dumps({'release_name': title,
                                                              'torrent_infohash': digest(1)}).encode())
        config = ConfigurationSnapshot(1, {'storage': {'database_path': str(self.path)}})
        with self.assertRaises(Stop):             # Coordination finished, then the reader ran.
            await verifier(source, config, threading.Event(), claim=SimpleNamespace(job_id=job))
        self.assertEqual(writes, ['ok'])

    async def test_an_unreachable_qbittorrent_makes_the_job_wait(self):
        event, job, title = self.release(1)

        def down(configuration, digests):
            raise ClientUnavailable()

        async def reader(*args, **kwargs):
            raise AssertionError('must not read metadata')
        verifier = make_batch_verifier(Options('registeredclient', 1), lambda: Credentials('private'), reader,
                                       client_factory=lambda *a: None, observe_existing=down)
        source = ReleaseInput(event, 1, AID, TID, json.dumps({'release_name': title,
                                                              'torrent_infohash': digest(1)}).encode())
        config = ConfigurationSnapshot(1, {'storage': {'database_path': str(self.path)}})
        with self.assertRaises(MetadataSourceUnavailable):
            await verifier(source, config, threading.Event(), claim=SimpleNamespace(job_id=job))
        self.assertEqual(self.rows("SELECT count(*) FROM batch_candidates"), [(0,)])

    async def test_pooled_result_carries_the_winner(self):
        x = self.release(1, '[Group] Cyberpunk Edgerunners S01 (Batch) [1080p] [Dual Audio]')
        self.coordinate(*x)
        db = self.connect()
        with transaction(db):
            db.execute("UPDATE batch_candidates SET state='READY'")
            db.execute("UPDATE batch_candidate_scopes SET active_candidate_id=NULL")
        db.close()
        event, job, title = self.release(2)

        async def reader(*args, **kwargs):
            raise AssertionError('a pooled release must not read metadata')
        verifier = make_batch_verifier(Options('registeredclient', 1), lambda: Credentials('private'), reader,
                                       client_factory=lambda *a: None)
        source = ReleaseInput(event, 1, AID, TID, json.dumps({'release_name': title,
                                                              'torrent_infohash': digest(2)}).encode())
        config = ConfigurationSnapshot(1, {'storage': {'database_path': str(self.path)}})
        result = await verifier(source, config, threading.Event(), claim=SimpleNamespace(job_id=job))
        self.assertIs(type(result), BatchCandidatePooled)
        self.assertIsNotNone(result.winner_candidate_id)
        self.assertIsNone(get_active_candidate(self.connect(), result.scope_id))


if __name__ == '__main__':
    unittest.main()
