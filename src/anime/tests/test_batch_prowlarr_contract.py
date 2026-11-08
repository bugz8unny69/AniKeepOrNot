"""WI-14/WI-15: Prowlarr magnets survive the batch pipeline; a failed search is never "exhausted"."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.anime.acquisition.metainfo_source import _event_magnet
from src.anime.acquisition.prowlarr_search import (
    BatchSearchResult, Client, DiscoveryPolicy, Options, Release, SearchUnauthorized, SearchUnavailable,
)
from src.anime.configuration import ConfigurationSnapshot
from src.anime.jobs.discover_batch_prowlarr_job import KIND, registration
from src.anime.storage.batch_candidates import (
    activate_candidate, add_candidate, fail_candidate, get_active_candidate, get_or_create_scope,
    ingest_batch_prowlarr_results, prepare_batch_prowlarr_search,
)
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import enqueue_job, initialize, transaction

AID, TID = 16999, 393400
OLD, NEW, ARRIVED = '1' * 40, '4' * 40, '7' * 40
TRACKERS = ('&tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce'
            '&tr=http%3A%2F%2Fprivate.example%2Fannounce%3Fpasskey%3Dsecret')


def magnet(digest, extra=TRACKERS):
    return f'magnet:?xt=urn:btih:{digest}&dn=Cyberpunk+Edgerunners+S01{extra}'


def release(digest=NEW, url=None, title='[Group] Cyberpunk Edgerunners - S01 [Dual Audio 1080p]'):
    return Release(title=title, indexer='Nyaa', resolution=1080, seeders=20, size=8_000_000,
                   tvdb_episode_id=None, info_hash=digest, magnet_url=url or magnet(digest),
                   eligible_for_handoff=True)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'q.sqlite'
        db = sqlite3.connect(self.path)
        initialize(db)
        db.close()
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id,tvdb_series_id,title) VALUES(?,?,'Cyberpunk Edgerunners')",
                       (AID, TID))
            db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,state) "
                       "VALUES(1,'autobrr',X'61',1000,'IDENTIFIED')")
            db.execute("INSERT INTO identity_results(result_id,event_id,anidb_id,schema_version,series_json,validated_at_ms) "
                       "VALUES(1,1,?,2,?,1000)", (AID, json.dumps({'schema_version': 2, 'anime': {
                           'anidb_id': AID, 'tvdb_series_id': TID, 'title': 'Cyberpunk Edgerunners'}, 'episodes': []})))
            self.scope = get_or_create_scope(db, AID, TID, 1)
            add_candidate(db, self.scope.scope_id, OLD, '[Group] Cyberpunk S01 [1080p]', source_event_id=1)
            activate_candidate(db, self.scope.scope_id, OLD)
            fail_candidate(db, self.scope.scope_id, OLD, 'TORRENT_METADATA_TIMEOUT')

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def rows(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).fetchall()

    def ingest(self, result):
        with self.connect() as db:
            params = prepare_batch_prowlarr_search(db, self.scope.scope_id)
        with self.connect() as db, transaction(db):
            return ingest_batch_prowlarr_results(db, params, result)


class MagnetContractTests(Base):
    async def test_tracker_hints_survive_ingestion_selection_handoff_and_metadata(self):
        self.ingest(BatchSearchResult((release(),)))
        with self.connect() as db:
            active = get_active_candidate(db, self.scope.scope_id)
        self.assertEqual(active.info_hash, NEW)                                  # Selected.
        body = json.loads(self.rows("SELECT raw_body FROM incoming_events WHERE event_id=?",
                                    (active.source_event_id,))[0][0])
        self.assertNotIn('magnet_url', body)                                     # One canonical field.
        self.assertIn('tracker.example', body['magnet_uri'])                     # Public tracker kept...
        self.assertNotIn('passkey', body['magnet_uri'])                          # ...credentialed one dropped.
        job = self.rows("SELECT event_id FROM jobs WHERE kind='VERIFY_RELEASE'")  # Handed off.
        self.assertEqual(job, [(active.source_event_id,)])
        # The batch metadata reader admits exactly this magnet, not a bare hash.
        self.assertEqual(_event_magnet(str(self.path), active.source_event_id, NEW), body['magnet_uri'])

    async def test_a_magnet_for_another_torrent_is_rejected(self):
        client = Client(Options('http://p:9696', 10), 'key', catalog=lambda *a: None,
                        policy=DiscoveryPolicy(max_resolution=1080, min_seeders=1))
        rows = [{'title': '[Group] Cyberpunk Edgerunners - S01 [1080p]', 'infoHash': NEW, 'seeders': 9,
                 'size': 1, 'magnetUrl': magnet('9' * 40)}]
        with patch.object(client, '_get', return_value=rows):
            result = client.search_batch(['Cyberpunk Edgerunners'], 1)
        self.assertEqual(result.releases, ())
        self.assertGreaterEqual(client.snapshots[0]['counts']['CONFLICTING_INFO_HASH'], 1)

    async def test_search_keeps_the_original_magnet(self):
        client = Client(Options('http://p:9696', 10), 'key', catalog=lambda *a: None,
                        policy=DiscoveryPolicy(max_resolution=1080, min_seeders=1))
        rows = [{'title': '[Group] Cyberpunk Edgerunners - S01 [1080p]', 'infoHash': NEW, 'seeders': 9,
                 'size': 1, 'magnetUrl': magnet(NEW)}]
        with patch.object(client, '_get', return_value=rows):
            result = client.search_batch(['Cyberpunk Edgerunners'], 1)
        self.assertEqual(result.releases[0].magnet_url, magnet(NEW))


def search_client(responses):
    """A real Client whose queries answer in order: a list of rows, or an exception."""
    client = Client(Options('http://p:9696', 10), 'key', catalog=lambda *a: None,
                    policy=DiscoveryPolicy(max_resolution=1080, min_seeders=1))
    answers = list(responses)

    def get(query):
        answer = answers.pop(0) if answers else []
        if isinstance(answer, Exception):
            raise answer
        return answer
    client._get = get
    return client


ROW = {'title': '[Group] Cyberpunk Edgerunners - S01 [1080p]', 'infoHash': NEW, 'seeders': 9, 'size': 1}


class SearchOutcomeTests(unittest.TestCase):
    def test_every_query_failing_is_unavailable_not_empty(self):
        client = search_client([SearchUnavailable()] * 10)
        with self.assertRaises(SearchUnavailable):
            client.search_batch(['Cyberpunk Edgerunners'], 1)
        self.assertEqual(client.snapshots[0]['status'], 'FAILED')
        self.assertTrue(all(q['status'] == 'SERVICE_UNAVAILABLE' for q in client.snapshots[0]['queries']))

    def test_a_successful_empty_search_is_complete(self):
        result = search_client([[]] * 10).search_batch(['Cyberpunk Edgerunners'], 1)
        self.assertEqual((result.releases, result.complete), ((), True))

    def test_partial_failures_keep_results_and_diagnostics(self):
        client = search_client([SearchUnavailable(), [ROW]])
        result = client.search_batch(['Cyberpunk Edgerunners'], 1)
        self.assertEqual([r.info_hash for r in result.releases], [NEW])
        self.assertFalse(result.complete)
        self.assertEqual(result.failed[0][1], 'SERVICE_UNAVAILABLE')
        snapshot = client.snapshots[0]
        self.assertEqual(snapshot['status'], 'PARTIAL')
        self.assertEqual([q['status'] for q in snapshot['queries']][:2], ['SERVICE_UNAVAILABLE', 'SUCCESS'])

    def test_a_rejected_api_key_is_not_swallowed(self):
        with self.assertRaises(SearchUnauthorized):
            search_client([SearchUnauthorized()]).search_batch(['Cyberpunk Edgerunners'], 1)


class DiscoveryJobTests(Base):
    async def run_job(self, search, *, prowlarr='http://p:9696'):
        with self.connect() as db, transaction(db):
            enqueue_job(db, component='worker', kind=KIND, event_id=1, available_at_ms=0,
                        payload={'scope_id': self.scope.scope_id, 'anidb_id': AID, 'tvdb_series_id': TID,
                                 'tvdb_season': 1})
        queue = JobQueue(str(self.path))
        claim, _ = queue.claim()
        client = Mock()
        client.snapshots = []
        client.search_batch.side_effect = search
        config = ConfigurationSnapshot(1, {'storage': {'database_path': str(self.path)},
                                           'prowlarr': {'base_url': prowlarr}})
        reg = registration(Mock(), client_factory=lambda c, s: client)
        context = SimpleNamespace(claim=claim, configuration=config, cancelled=threading.Event(),
                                  data=claim.data, logger=None)
        return await reg.execute(context), client

    def scope_state(self):
        return self.rows("SELECT state FROM batch_candidate_scopes")[0][0]

    async def test_unavailable_prowlarr_is_a_service_wait_and_never_exhausts(self):
        # An outage is reported for Prowlarr; the worker turns it into the standard
        # service wait (no retry spent, resumes when Prowlarr is back).
        outcome, _ = await self.run_job(SearchUnavailable())
        self.assertEqual((outcome.kind, outcome.reason_code, outcome.facts),
                         ('retry', 'SERVICE_UNAVAILABLE', {'service': 'prowlarr'}))
        self.assertEqual(self.scope_state(), 'ACTIVE')

    async def test_a_timeout_waits_once_and_never_exhausts(self):
        from src.anime.acquisition.prowlarr_search import SearchTimeout
        outcome, _ = await self.run_job(SearchTimeout())
        self.assertEqual((outcome.kind, outcome.reason_code), ('defer', 'RELEASE_SEARCH_TIMEOUT'))
        self.assertEqual(self.scope_state(), 'ACTIVE')

    async def test_partial_failure_with_nothing_found_waits(self):
        outcome, _ = await self.run_job(
            lambda *a, **k: BatchSearchResult((), complete=False, failed=(('q', 'RELEASE_SEARCH_TIMEOUT'),)))
        self.assertEqual((outcome.kind, outcome.reason_code), ('defer', 'RELEASE_SEARCH_TIMEOUT'))
        self.assertEqual(self.scope_state(), 'ACTIVE')

    async def test_a_complete_empty_search_exhausts(self):
        outcome, _ = await self.run_job(lambda *a, **k: BatchSearchResult(()))
        self.assertEqual(outcome.kind, 'complete')
        self.assertEqual(self.scope_state(), 'EXHAUSTED')

    async def test_automatic_search_off_exhausts_without_searching(self):
        outcome, client = await self.run_job(AssertionError('must not search'), prowlarr='')
        self.assertEqual(outcome.kind, 'complete')
        self.assertEqual(self.scope_state(), 'EXHAUSTED')
        client.search_batch.assert_not_called()

    async def test_an_autobrr_arrival_during_the_search_stays_the_only_active_candidate(self):
        def search(*args, **kwargs):
            # A new Autobrr release arrives and is activated while Prowlarr answers.
            with self.connect() as db, transaction(db):
                add_candidate(db, self.scope.scope_id, ARRIVED, '[Sub] Cyberpunk Edgerunners S01 [1080p]',
                              source_event_id=1)
                activate_candidate(db, self.scope.scope_id, ARRIVED)
            return BatchSearchResult((release(),))
        outcome, _ = await self.run_job(search)
        self.assertEqual(outcome.kind, 'complete')
        self.assertEqual(self.rows("SELECT info_hash,state FROM batch_candidates WHERE state IN ('ACTIVE','READY') "
                                   "ORDER BY info_hash"), [(NEW, 'READY'), (ARRIVED, 'ACTIVE')])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='VERIFY_RELEASE'"), [(0,)])


if __name__ == '__main__':
    unittest.main()
