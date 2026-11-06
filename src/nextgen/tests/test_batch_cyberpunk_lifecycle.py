"""WI-18: Cyberpunk Season 1 through the real worker, queue, management API and cleanup.

Real: the worker loop, job queue and transitions, VERIFY_RELEASE with the
batch verifier and the series-scoped controller, automatic failure cleanup,
the management API Cancel, the CLEANUP_CANCELED_OPERATION job and
CancellationService, DISCOVER_BATCH_PROWLARR, and mark_staged.

Stand-ins (each has its own tests): qBittorrent (read-only file lists), the
metadata reader, the AniDB/TVDB episode proof, Prowlarr's HTTP answers, and
EVALUATE_BATCH plus STAGE_BATCH_EPISODE. The stand-in stage job makes the
same two writes as mark_staged after its publication proof (operation
STAGED, candidate succeeded through succeed_operation_candidate); that
proof itself is covered by the staging tests.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.nextgen.acquisition.metadata import BatchInventory, TorrentFile
from src.nextgen.acquisition.metadata_service import MetadataSourceTimeout
from src.nextgen.acquisition.prowlarr_search import BatchSearchResult, SearchUnavailable
from src.nextgen.acquisition.submission import Observation
from src.nextgen.batch.provider_worker import make_batch_verifier
from src.nextgen.batch.verification import VerifiedBatch, VerifiedBatchFile
from src.nextgen.acquisition.existing_metadata import make_existing_observer
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import Outcome, Policy, Registration, Registry, Successor
from src.nextgen.jobs.cleanup_canceled_operation_job import registration as cleanup_registration
from src.nextgen.jobs.discover_batch_prowlarr_job import registration as discovery_registration
from src.nextgen.jobs.verify_release_job import registration as release_registration
from src.nextgen.providers.http import Credentials, Options
from src.nextgen.providers.releases import make_verifier
from src.nextgen.storage.batch_candidates import succeed_operation_candidate
from src.nextgen.storage.sqlite import connect, enqueue_job, transaction
from src.nextgen.tests import test_api_v1 as v1
from src.nextgen.tests.test_batch_candidate_controller import make_series_json
from src.nextgen.tests.test_torrent_submission import Secrets
from src.nextgen.worker import Worker

AID, TID = 16999, 393400
MB = 1_000_000

# Five distinct season packs; each arrives ten times (fifty deliveries).
A = 'a' * 40   # Dual Audio, file list already in qBittorrent -> ranked first; metadata never arrives.
B = 'b' * 40   # Dual Audio -> the user cancels it while it runs.
C = 'c' * 40   # Dual Audio, but an episode is over the size limit.
D = 'd' * 40   # French dub without dual audio -> audio incompatible.
E = 'e' * 40   # Plain 1080p, the most seeded remaining -> succeeds and is staged.
RELEASES = {
    A: ('[GroupA] Cyberpunk Edgerunners S01 (Batch) [1080p] [Dual Audio]', 5),
    B: ('[GroupB] Cyberpunk Edgerunners S01 (Batch) [1080p] [Dual Audio]', 40),
    C: ('[GroupC] Cyberpunk Edgerunners S01 (Batch) [1080p] [Dual Audio]', 30),
    D: ('[GroupD] Cyberpunk Edgerunners S01 (Batch) [1080p] [French]', 90),
    E: ('[GroupE] Cyberpunk Edgerunners S01 (Batch) [1080p]', 20),
}
SIZES = {A: 50 * MB, B: 50 * MB, C: 900 * MB, D: 50 * MB, E: 50 * MB}


class ReadOnlyQb:
    """qBittorrent for candidate evidence: only A's torrent exists. Writes fail the test."""
    def __init__(self, options=None, credentials=None):
        pass

    def login(self):
        pass

    def inspect(self, digest, *, include_files=True):
        if digest != A:
            return None
        return Observation(A, 'stoppedDL', '/srv/other', 'tv', (), False,
                           (TorrentFile(0, 'Cyberpunk/E01.mkv', 50 * MB),))

    def __getattr__(self, name):
        raise AssertionError(f'qBittorrent mutation attempted: {name}')


class Prowlarr:
    """search_batch answers in order: an exception, or a BatchSearchResult."""
    def __init__(self, answers):
        self.answers, self.calls, self.snapshots, self.logger = list(answers), 0, [], None

    def set_context(self, **kwargs):
        pass

    def search_batch(self, titles, season, *, tried=()):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class CyberpunkLifecycle(unittest.IsolatedAsyncioTestCase):
    asyncSetUp_api = v1.ApiV1Tests.asyncSetUp
    request = v1.ApiV1Tests.request

    async def asyncSetUp(self):
        await self.asyncSetUp_api()
        self.store.update({'acquisition': {'audio_check': 'japanese_english', 'max_episode_mb': 100},
                           'prowlarr': {'base_url': 'http://prowlarr:9696'},
                           'qbittorrent': {'base_url': 'http://qb:8080'},
                           'storage': {'download_root': '/srv/downloads'}},
                          expected_revision=self.store.read().revision)
        self.reads = {}            # Metadata reads ("downloads") per torrent.
        self.b_running, self.b_release = asyncio.Event(), asyncio.Event()
        self.prowlarr = Prowlarr([])

    # --- composition ---------------------------------------------------------
    def registry(self):
        async def reader(digest, configuration, cancelled, *, claim=None, event_id=None, evaluation=False):
            self.reads[digest] = self.reads.get(digest, 0) + 1
            if digest == A:
                raise MetadataSourceTimeout()
            if digest == B:
                self.b_running.set()
                await self.b_release.wait()
            return b'metainfo'

        batch = make_batch_verifier(Options('registeredclient', 1), lambda: Credentials('private'), reader,
                                    client_factory=lambda *a: None,
                                    observe_existing=make_existing_observer(
                                        SimpleNamespace(qbittorrent_credentials=lambda: None),
                                        client_factory=ReadOnlyQb))
        verify = make_verifier(Options('registeredclient', 1), lambda: Credentials('private'),
                               batch_verifier=batch, client_factory=lambda *a: None)

        async def evaluate(context):
            return Outcome(value=context.claim.job_id)

        async def stage(context):
            return Outcome(value=context.claim.job_id)

        def staged(db, claim, value):
            # The two writes mark_staged makes once its publication proof
            # holds (that proof needs the real download/staging tables and
            # is covered by the staging tests): the operation is STAGED and
            # its active batch candidate succeeds through the same hook.
            op = db.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?",
                            (claim.job_id,)).fetchone()[0]
            db.execute("UPDATE job_operations SET state='STAGED' WHERE operation_id=?", (op,))
            succeed_operation_candidate(db, op)
            return ()

        return Registry((
            release_registration(verify, batch_kind='EVALUATE_BATCH'),
            Registration('EVALUATE_BATCH', HandlerDefinition('EVALUATE_BATCH', 'Evaluate batch'), evaluate,
                         route=lambda value: (Successor.create('STAGE_BATCH_EPISODE', {'evaluation_job_id': value}),),
                         successor_kinds=('STAGE_BATCH_EPISODE',)),
            Registration('STAGE_BATCH_EPISODE', HandlerDefinition('STAGE_BATCH_EPISODE', 'Stage episode'), stage,
                         persist=staged),
            cleanup_registration(Secrets()),
            discovery_registration(SimpleNamespace(), client_factory=lambda configuration, secrets: self.prowlarr),
        ))

    def worker(self):
        """A fresh worker on the same database: creating one again is a restart."""
        worker = Worker(self.store, self.startup, self.registry(), policy=Policy(lease_ms=30000, poll_ms=10))
        self.addAsyncCleanup(worker.close)
        self.addCleanup(self.b_release.set)     # Runs first: a failed test never leaves B blocked.
        return worker

    # --- data ----------------------------------------------------------------
    def sql(self, statement, args=()):
        db = connect(self.database)
        try:
            with db:
                return db.execute(statement, args).fetchall()
        finally:
            db.close()

    def arrive(self, releases, *, season=1, start=1):
        """Identified deliveries with their VERIFY_RELEASE jobs, ten per release."""
        db = connect(self.database)
        try:
            with transaction(db):
                if not db.execute("SELECT 1 FROM identified_anime WHERE anidb_id=?", (AID,)).fetchone():
                    db.execute("INSERT INTO identified_anime(anidb_id,tvdb_series_id,title) "
                               "VALUES(?,?,'Cyberpunk: Edgerunners')", (AID, TID))
                event = start
                for round_ in range(10):
                    for digest, (title, seeders) in releases.items():
                        db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,state) "
                                   "VALUES(?, 'autobrr', ?, ?, 'IDENTIFIED')",
                                   (event, json.dumps({'release_name': title.replace('S01', f'S{season:02d}'),
                                                       'torrent_infohash': digest, 'seeders': seeders}).encode(),
                                    1000 + event))
                        db.execute("INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,"
                                   "validated_at_ms) VALUES(?,?,2,?,?)", (event, AID, make_series_json(), 1000 + event))
                        enqueue_job(db, component='intake', kind='VERIFY_RELEASE', payload={'anidb_id': AID},
                                    event_id=event, available_at_ms=0)
                        event += 1
        finally:
            db.close()

    def candidates(self, season=1):
        return dict(self.sql("SELECT c.info_hash,c.state FROM batch_candidates c "
                             "JOIN batch_candidate_scopes s USING(scope_id) WHERE s.tvdb_season=?", (season,)))

    def check_invariants(self):
        # Never two active candidates for a season; never two runs of one release.
        for (count,) in self.sql("SELECT count(*) FROM batch_candidates WHERE state='ACTIVE' GROUP BY scope_id"):
            self.assertLessEqual(count, 1)
        self.assertTrue(all(n == 1 for n in self.reads.values()), self.reads)
        self.assertEqual(self.sql("SELECT count(*) FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR' "
                                  "AND state IN ('PENDING','RUNNING','RETRY_WAIT','ON_HOLD')")[0][0] <= 1, True)

    async def drain(self, worker):
        while True:
            task = asyncio.ensure_future(worker.attempt())
            waiter = asyncio.ensure_future(self.b_running.wait())
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            if task not in done:
                return task                  # B is running right now.
            self.check_invariants()
            if not task.result():
                return None

    def stop_reason(self, digest):
        return self.sql("SELECT s.disposition, s.reason_code FROM batch_candidate_stops s "
                        "JOIN batch_candidates c USING(candidate_id) WHERE c.info_hash=?", (digest,))


class CyberpunkSeasonOneTests(CyberpunkLifecycle):
    async def test_fifty_releases_one_controller_through_failure_cancel_rejections_and_success(self):
        self.arrive(RELEASES)
        with patch('src.nextgen.batch.provider_worker.read_batch_inventory',
                   lambda raw, digest: BatchInventory(digest, 'f' * 64,
                                                      (TorrentFile(0, 'Cyberpunk/E01.mkv', SIZES[digest]),))), \
             patch('src.nextgen.batch.provider_worker.verify_batch_files',
                   lambda inventory, **kw: VerifiedBatch(inventory.info_hash, 'f' * 64, AID, TID, 1,
                                                         (VerifiedBatchFile(0, 'Cyberpunk/E01.mkv', 1, 1, 1001,
                                                                            1, 1, 2001),))):
            # 1. Fifty deliveries, one controller, one active candidate. A
            #    ranks first: Dual Audio and its file list is already known.
            #    A never gets metadata: TORRENT_METADATA_TIMEOUT, then safe
            #    cleanup, then the next known candidate (B) starts.
            running = await self.drain(self.worker())
            self.assertIsNotNone(running, 'B should be running')
            self.assertEqual(self.sql("SELECT count(*) FROM batch_candidate_scopes"), [(1,)])
            self.assertEqual(self.stop_reason(A), [('FAILED', 'TORRENT_METADATA_TIMEOUT')])
            self.assertEqual(self.candidates(), {A: 'FAILED', B: 'ACTIVE', C: 'READY', D: 'READY', E: 'READY'})
            a_op = self.sql("SELECT s.operation_id FROM batch_candidate_stops s JOIN batch_candidates c "
                            "USING(candidate_id) WHERE c.info_hash=?", (A,))[0][0]
            self.assertEqual(self.sql("SELECT state,cleanup_state FROM job_operations WHERE operation_id=?", (a_op,)),
                             [('CANCELED', 'COMPLETE')])

            # 2. The user cancels B through the management API while it runs.
            b_job = self.sql("SELECT j.job_id FROM jobs j WHERE j.state='RUNNING' AND j.kind='VERIFY_RELEASE'")[0][0]
            status, result, _ = await self.request('POST', f'/api/v1/jobs/{b_job}/cancel', {}, token=v1.ADMIN)
            self.assertEqual(status, 202)
            self.b_release.set()
            await running
            self.b_running = asyncio.Event()   # B does not run again.

            # 3. Restart the worker. Cleanup finishes, then C starts (too big),
            #    then D (French, no dual audio), then E succeeds and is staged.
            self.assertIsNone(await self.drain(self.worker()))
        self.assertEqual(self.stop_reason(B), [('CANCELED', 'JOB_CANCELED')])
        self.assertEqual(self.stop_reason(C), [('FAILED', 'ACQUISITION_SIZE_LIMIT')])
        # D (French, no dual audio) ranks below every compatible release under
        # the Japanese/English policy, so E is tried first; D is never fetched.
        ranks = dict(self.sql("SELECT info_hash, rank FROM batch_candidates WHERE state='READY'"))
        self.assertEqual(list(ranks), [D])
        self.assertEqual(self.candidates(), {A: 'FAILED', B: 'CANCELED', C: 'FAILED', D: 'READY', E: 'SUCCEEDED'})
        self.assertEqual(self.sql("SELECT state FROM batch_candidate_scopes"), [('SUCCEEDED',)])
        self.assertEqual(self.sql("SELECT state FROM jobs WHERE kind='STAGE_BATCH_EPISODE'"), [('DONE',)])
        # Each release was fetched at most once; D never.
        self.assertEqual(self.reads, {A: 1, B: 1, C: 1, E: 1})
        # Prowlarr was never needed: known candidates came first.
        self.assertEqual(self.sql("SELECT count(*) FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'"), [(0,)])
        # Every cleanup completed; nothing left half-cleaned.
        self.assertEqual(self.sql("SELECT count(*) FROM job_operations WHERE state IN ('CANCEL_REQUESTED','CANCELED') "
                                  "AND cleanup_state<>'COMPLETE'"), [(0,)])


class CyberpunkProwlarrFallbackTests(CyberpunkLifecycle):
    async def test_exhausted_candidates_search_prowlarr_and_a_failure_is_not_zero_results(self):
        bad = {A: RELEASES[A], C: RELEASES[C], D: RELEASES[D]}
        self.arrive(bad)
        self.prowlarr = Prowlarr([SearchUnavailable(), BatchSearchResult(())])
        with patch('src.nextgen.batch.provider_worker.read_batch_inventory',
                   lambda raw, digest: BatchInventory(digest, 'f' * 64,
                                                      (TorrentFile(0, 'Cyberpunk/E01.mkv', SIZES[digest]),))), \
             patch('src.nextgen.batch.provider_worker.verify_batch_files',
                   lambda inventory, **kw: VerifiedBatch(inventory.info_hash, 'f' * 64, AID, TID, 1,
                                                         (VerifiedBatchFile(0, 'Cyberpunk/E01.mkv', 1, 1, 1001,
                                                                            1, 1, 2001),))):
            # Both known candidates fail; one Prowlarr search is scheduled.
            self.assertIsNone(await self.drain(self.worker()))
            self.assertEqual(self.candidates(), {A: 'FAILED', C: 'FAILED', D: 'FAILED'})
            # Tried last, D is rejected for its audio before any metadata is fetched.
            self.assertEqual(self.stop_reason(D), [('FAILED', 'MEDIA_AUDIO_REQUIRED')])
            self.assertEqual(self.prowlarr.calls, 1)
            # Prowlarr was unavailable: the season is NOT exhausted, the search waits.
            self.assertEqual(self.sql("SELECT state FROM batch_candidate_scopes"), [('ACTIVE',)])
            self.assertEqual(self.sql("SELECT state,last_reason_code FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'"),
                             [('RETRY_WAIT', 'PROWLARR_UNAVAILABLE')])
            # Restarts never add a second search.
            self.assertIsNone(await self.drain(self.worker()))
            self.assertEqual(self.sql("SELECT count(*) FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'"), [(1,)])
            # The wait ends; this time every query succeeds and finds nothing.
            self.sql("UPDATE jobs SET available_at_ms=0 WHERE kind='DISCOVER_BATCH_PROWLARR'")
            self.assertIsNone(await self.drain(self.worker()))
        self.assertEqual(self.prowlarr.calls, 2)
        self.assertEqual(self.sql("SELECT state FROM batch_candidate_scopes"), [('EXHAUSTED',)])
        self.assertEqual(self.sql("SELECT state FROM jobs WHERE kind='DISCOVER_BATCH_PROWLARR'"), [('DONE',)])
        self.assertEqual(self.reads, {A: 1, C: 1})            # D never fetched.


if __name__ == '__main__':
    unittest.main()
