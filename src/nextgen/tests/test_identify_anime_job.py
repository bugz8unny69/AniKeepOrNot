"""Offline identity-handler/worker integration; authored but not executed."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import Outcome, Policy, Registration, Registry, Successor
from src.nextgen.jobs.identify_anime_job import (
    KIND, IdentifiedSeries, ProviderDeferred, ProviderUnavailable, registration,
)
from src.nextgen.storage.job_queue import LeaseLost, QueueUnavailable
from src.nextgen.storage.sqlite import (
    accept_event, connect, enqueue_job, initialize, persist_identity, save_identity, transaction,
)
from src.nextgen.worker import Worker


def candidate(aid=123, tid=456, episodes=None):
    return {"schema_version":2,"anime":{"anidb_id":aid,"tvdb_series_id":tid,"title":"Test Anime"},
            "episodes":[] if episodes is None else episodes,"attributes":{}}


class IdentifyAnimeJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root/"queue.sqlite"
        self.store = ConfigurationStore(self.root)
        self.startup = self.store.initialize({"storage":{"database_path":str(self.database)}}).snapshot
        db = connect(self.database)
        initialize(db)
        db.close()

    def enqueue(self, payload=None, raw_body=b'private incoming delivery'):
        db = connect(self.database)
        try:
            event = accept_event(db,"test-source",raw_body)
            with transaction(db):
                job = enqueue_job(db,kind=KIND,payload={} if payload is None else payload,
                                  component="intake",event_id=event,available_at_ms=0)
            return event,job
        finally:
            db.close()

    def rows(self, sql, parameters=()):
        db = connect(self.database)
        try:
            return db.execute(sql,parameters).fetchall()
        finally:
            db.close()

    def worker(self, spec, *more):
        worker = Worker(self.store,self.startup,Registry((spec,*more)),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        return worker

    async def test_s02_review_persists_matching_reason_without_guessing_anidb_identity(self):
        from src.nextgen.providers.identity import make_resolver
        from src.nextgen.providers.http import Credentials, Options
        name = "[Dyna] Higurashi When They Cry S02 - 01 [Dual Audio]"
        _, job = self.enqueue(raw_body=json.dumps({"release_name": name}).encode())
        from src.nextgen.storage.sqlite import IdentityNotReady
        class NoSeasonEntry:
            # Season 1 resolves, but anime-lists records no Season 2 entry.
            def title_candidates(self, aliases): return {15100: ("Higurashi When They Cry",)}
            def anime(self, aid): return {"anidb_id": aid, "title": "Higurashi", "titles": ("Higurashi When They Cry",)}
            def mapping(self, aid): return 80000
            def season_entry(self, tid, season, episode=None): raise IdentityNotReady()
        resolver = make_resolver(Options("registeredclient", 1),
                                 lambda: Credentials("private-tvdb-test-key"),
                                 client_factory=lambda *args: NoSeasonEntry())
        worker = self.worker(registration(resolver))
        self.assertTrue(await worker.attempt())
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),
                         [("NEEDS_REVIEW","IDENTITY_INCOMPLETE")])
        record = self.rows("SELECT details_json FROM job_history WHERE job_id=? AND event_code='NEEDS_REVIEW'",(job,))
        self.assertEqual(len(record),1)
        details = json.loads(record[0][0])
        self.assertEqual(details["attempt"],1)
        trace = details["matching"]
        self.assertEqual(trace["season"],2)
        # Season 1's entry is never approved for Season 2.
        self.assertIn({"step":"season_scope","status":"passed","note":"base_series_season_lookup"},trace["checks"])
        self.assertIn("anime_lists",[c["step"] for c in trace["checks"] if c["status"]=="failed"])
        self.assertNotIn("recognized_season",[c["step"] for c in trace["checks"] if c["status"]=="failed"])
        self.assertIn({"step":"recognized_season","status":"skipped","note":"not_previously_recognized"},trace["checks"])
        self.assertNotIn("identity_verified",[c["step"] for c in trace["checks"] if c["status"]=="passed"])
        self.assertNotIn("private-tvdb-test-key",record[0][0])
        self.assertNotIn("webhook_token",record[0][0])

    async def test_series_only_identity_and_release_handoff_commit_together(self):
        event,job = self.enqueue()
        requests = []
        async def resolve(request,configuration,cancelled):
            requests.append(request)
            self.assertEqual(request.raw_body,b'private incoming delivery')
            self.assertEqual(configuration.desired_revision,1)
            return candidate()
        async def next_handler(context):
            self.fail("a single attempt must not run the successor inline")
        release = Registration("VERIFY_RELEASE",HandlerDefinition("VERIFY_RELEASE","Verify release"),next_handler)
        worker = self.worker(registration(resolve,release_kind=release.kind),release)
        receipts = []
        with patch("src.nextgen.worker.log_history",side_effect=lambda logger,record:receipts.append(record)):
            self.assertTrue(await worker.attempt())
        self.assertEqual(requests[0].event_id,event)
        self.assertNotIn("private incoming delivery",repr(requests))
        self.assertEqual(self.rows("SELECT state FROM incoming_events"),[("IDENTIFIED",)])
        self.assertEqual(self.rows("SELECT kind,state,event_id,created_by_job_id FROM jobs ORDER BY job_id"),
                         [(KIND,"DONE",event,None),("VERIFY_RELEASE","PENDING",event,job)])
        self.assertEqual(self.rows("SELECT count(*) FROM identified_episodes"),[(0,)])
        self.assertEqual(self.rows("SELECT event_id,anidb_id FROM identity_results"),[(event,123)])
        payload = self.rows("SELECT payload_json FROM jobs WHERE created_by_job_id=?",(job,))[0][0]
        self.assertEqual(json.loads(payload),{"anidb_id":123})
        self.assertEqual(sum(record.event_code=="SERIES_IDENTIFIED" for record in receipts),1)
        self.assertEqual(self.rows("SELECT event_code FROM job_history WHERE job_id=? AND handler_code IS NOT NULL ORDER BY history_id",(job,)),
                         [("HANDLER_STARTED",),("HANDLER_COMPLETED",)])

    async def test_untrusted_payload_cannot_supply_verified_identity(self):
        async def resolve(*args):
            self.fail("payload identities are not provider evidence")
        _,job = self.enqueue({"verified":True,"series":candidate()})
        await self.worker(registration(resolve)).attempt()
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),
                         [("NEEDS_REVIEW","JOB_PAYLOAD_INVALID")])
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results"),[(0,)])

    async def test_invalid_provider_candidate_never_persists(self):
        for invalid in ({},candidate(tid=0),dict(candidate(),attributes="not-a-dict"),dict(candidate(),attributes={123:"bad_key"})):
            with self.subTest(invalid=invalid):
                async def resolve(*args): return invalid
                _,job = self.enqueue()
                await self.worker(registration(resolve)).attempt()
                self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),
                                 [("NEEDS_REVIEW","IDENTITY_INVALID")])
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results"),[(0,)])

    async def test_romaji_title_identity_enrichment_persists(self):
        event, job = self.enqueue()
        enriched = dict(candidate(), attributes={"romaji_title": "Shingeki no Kyojin"})
        async def resolve(*args): return enriched
        worker = self.worker(registration(resolve))
        self.assertTrue(await worker.attempt())
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE job_id=?", (job,)), [("DONE",)])
        self.assertEqual(self.rows("SELECT romaji_title FROM anime_title_metadata WHERE anidb_id=123"),
                         [("Shingeki no Kyojin",)])

    async def test_provider_wait_and_failure_have_distinct_retry_histories(self):
        for error,reason,marker in ((ProviderDeferred(2000),"JOB_WAITING","DEFERRED"),
                                    (ProviderUnavailable(),"SERVICE_UNAVAILABLE","RETRY_SCHEDULED")):
            async def resolve(*args): raise error
            _,job = self.enqueue()
            worker = self.worker(registration(resolve))
            worker.queue.clock = lambda:1000
            await worker.attempt()
            self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),
                             [("RETRY_WAIT",reason)])
            self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE job_id=? AND event_code=?",(job,marker)),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results"),[(0,)])

    async def test_conflicting_canonical_identity_is_reviewed_without_stopping_worker(self):
        first,_ = self.enqueue()
        db = connect(self.database)
        try: save_identity(db,first,candidate())
        finally: db.close()
        _,job = self.enqueue()
        async def resolve(*args): return candidate(tid=999)
        worker = self.worker(registration(resolve))
        # The earlier event already has identical facts; this resolver conflicts
        # there too. Both expected conflicts must leave the worker usable.
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),
                         [("NEEDS_REVIEW","IDENTITY_CONFLICT")])
        self.assertEqual(self.rows("SELECT tvdb_series_id FROM identified_anime"),[(456,)])
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results"),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='HANDLER_COMPLETED'"),[(2,)])

    async def test_bad_route_rolls_back_identity_event_and_successor(self):
        event,job = self.enqueue()
        async def resolve(*args): return candidate()
        spec = replace(registration(resolve),route=lambda result:(Successor.create("NOT_INSTALLED",{}),))
        worker = self.worker(spec)
        with patch("src.nextgen.worker.log_history") as logged:
            with self.assertRaises(ValueError): await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM incoming_events WHERE event_id=?",(event,)),[("PENDING",)])
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results"),[(0,)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE job_id=?",(job,)),[("RUNNING",)])
        self.assertEqual(self.rows("SELECT count(*) FROM event_history WHERE event_code='SERIES_IDENTIFIED'"),[(0,)])
        self.assertNotIn("SERIES_IDENTIFIED",[call.args[1].event_code for call in logged.call_args_list])

    async def test_identical_existing_result_is_reused_without_duplicate_event_audit(self):
        event,_ = self.enqueue()
        db = connect(self.database)
        try: save_identity(db,event,candidate())
        finally: db.close()
        async def resolve(*args): return candidate()
        await self.worker(registration(resolve)).attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results"),[(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM event_history WHERE event_code='SERIES_IDENTIFIED'"),[(1,)])
        self.assertEqual(self.rows("SELECT state FROM jobs"),[("DONE",)])

    async def test_stale_completion_cannot_store_identity(self):
        event,_ = self.enqueue()
        async def resolve(*args): return candidate()
        spec = registration(resolve)
        worker = self.worker(spec)
        worker.queue.clock = lambda:1000
        claim,_ = worker.queue.claim()
        worker.queue.start(claim,spec,"a"*32,worker.registry)
        result = IdentifiedSeries(event,json.dumps(candidate()))
        worker.queue.clock = lambda:31000
        with self.assertRaises(LeaseLost):
            worker.queue.finish(claim,Outcome(value=result),spec=spec,invocation="a"*32,registry=worker.registry)
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results"),[(0,)])

    async def test_large_delivery_and_incoming_lease_are_not_bypassed(self):
        async def resolve(*args): self.fail("source is not eligible")
        for leased in (False,True):
            event,job = self.enqueue(raw_body=b'x'*65537 if not leased else b'body')
            if leased:
                db = connect(self.database)
                try:
                    with db:
                        db.execute("UPDATE incoming_events SET state='RUNNING',lease_token='other',lease_expires_at_ms=9999999999999 WHERE event_id=?",(event,))
                finally: db.close()
            await self.worker(registration(resolve)).attempt()
            self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),
                             [("NEEDS_REVIEW","IDENTITY_INCOMPLETE")])

    async def test_caller_transaction_owns_identity_rollback(self):
        event,_ = self.enqueue()
        db = connect(self.database)
        try:
            with self.assertRaises(RuntimeError):
                with transaction(db):
                    result,audit = persist_identity(db,event,candidate())
                    self.assertIsInstance(result,int)
                    self.assertEqual(audit.event_code,"SERIES_IDENTIFIED")
                    raise RuntimeError("abort caller")
        finally: db.close()
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results"),[(0,)])
        self.assertEqual(self.rows("SELECT state FROM incoming_events"),[("PENDING",)])


    async def test_episode_owner_conflict_rolls_back_partial_new_anime(self):
        first,_ = self.enqueue()
        db = connect(self.database)
        try: save_identity(db,first,candidate(episodes=[{"anidb_episode_id":987}]))
        finally: db.close()
        event,job = self.enqueue()
        async def resolve(request,*args):
            if request.event_id == first:
                return candidate(episodes=[{"anidb_episode_id":987}])
            return candidate(aid=789,episodes=[{"anidb_episode_id":987}])
        worker = self.worker(registration(resolve))
        await worker.attempt()
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),
                         [("NEEDS_REVIEW","IDENTITY_CONFLICT")])
        self.assertEqual(self.rows("SELECT anidb_id FROM identified_anime"),[(123,)])
        self.assertEqual(self.rows("SELECT count(*) FROM identity_results WHERE event_id=?",(event,)),[(0,)])
        self.assertEqual(self.rows("SELECT state FROM incoming_events WHERE event_id=?",(event,)),[("PENDING",)])

    async def test_resolver_setup_requires_async_and_installed_successor(self):
        with self.assertRaises(ValueError): registration(lambda *args:candidate())
        async def resolve(*args): return candidate()
        with self.assertRaises(ValueError): registration(resolve,release_kind=KIND)
        with self.assertRaises(ValueError): Registry((registration(resolve,release_kind="VERIFY_RELEASE"),))


    async def test_source_storage_failure_is_not_reclassified_as_provider_retry(self):
        async def resolve(*args): self.fail("storage could not supply a request")
        _,job = self.enqueue()
        worker = self.worker(registration(resolve))
        with patch("src.nextgen.jobs.identify_anime_job._request",side_effect=QueueUnavailable()):
            with self.assertRaises(QueueUnavailable): await worker.attempt()
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE job_id=?",(job,)),[("RUNNING",None)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='RETRY_SCHEDULED'"),[(0,)])
