"""Regression tests for WI-21 & WI-22:
- Reusing authoritative saved episode context for Prowlarr fallback/exhausted candidate discovery
- Explaining pre-search failures, distinguishing provider outages from identity conflicts
- Excluding tried audio-failed hashes and recovering exhausted candidates (Plan #19 / Job #7264)
- Actionable diagnostics in adm report job when no snapshot exists
"""
import io
import json
import sqlite3
import unittest

from src.anime.acquisition.episode_context import (
    EpisodeSearchContext, EpisodeContextMissing, EpisodeContextConflict,
    load_authoritative_episode_context,
)
from src.anime.acquisition.prowlarr_search import (
    Client, DiscoveryPolicy, EpisodeCatalog, EpisodeTarget, Options,
    SearchReview, SearchUnavailable, SearchTimeout, SearchConfiguration, SearchUnauthorized,
)
from src.anime.job_contracts import Policy, Registry
from src.anime.job_report import report_prowlarr_matching, prowlarr_matching_json
from src.anime.jobs.discover_episode_releases_job import (
    KIND as DISCOVER_KIND, registration as discovery_registration,
)
from src.anime.providers.errors import ProviderUnavailable
from src.anime.episode_identity import VerifiedEpisode
from src.anime.episode_identity import EpisodeIdentityInvalid
from src.anime.storage.sqlite import accept_event_and_job, connect, enqueue_job, save_identity, transaction
from src.anime.tests.test_prowlarr_matching_report import make_raw_prowlarr_row
from src.anime.tests.test_user_evaluation_policy import UserEvaluationCheckTests
from src.anime.worker import Worker


def seed_plan(db, plan_id, aid, tid, anchor_ep=1, title='Test Anime', backfill_count=0, following_count=4):
    event, release_job = accept_event_and_job(db, 'test', b'declared release', kind='VERIFY_RELEASE', payload={'anidb_id': aid})
    identity = save_identity(db, event, {'schema_version': 2, 'anime': {'anidb_id': aid, 'tvdb_series_id': tid, 'title': title}})
    eid = 10000 + anchor_ep
    with transaction(db):
        db.execute("UPDATE jobs SET state='DONE', attempt_count=1 WHERE job_id=?", (release_job,))
        db.execute('INSERT OR IGNORE INTO identified_episodes VALUES(?, ?)', (eid, aid))
        v_id = db.execute(
            "INSERT INTO release_verifications(job_id, identity_result_id, anidb_id, release_sha256, verification_scope, "
            "anidb_episode_number, anidb_episode_id, tvdb_series_id, tvdb_season, tvdb_episode, tvdb_episode_id, verified_at_ms) "
            "VALUES(?, ?, ?, ?, 'DECLARED_SINGLE_EPISODE', ?, ?, ?, 1, ?, ?, 1)",
            (release_job, identity, aid, 'a'*64, anchor_ep, eid, tid, anchor_ep, eid + 100000)
        ).lastrowid
        db.execute(
            "INSERT INTO user_evaluation_plans(plan_id, anidb_id, tvdb_series_id, tvdb_season, tvdb_order, "
            "anchor_verification_id, anchor_episode, backfill_count, following_count, configuration_revision, created_at_ms) "
            "VALUES(?, ?, ?, 1, 'official', ?, ?, ?, ?, 1, 1000)",
            (plan_id, aid, tid, v_id, anchor_ep, backfill_count, following_count)
        )
    return event, release_job, v_id


class BackfillIdentityContextTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = UserEvaluationCheckTests.asyncSetUp
    rows = UserEvaluationCheckTests.rows

    def setUpFixture(self, aid=19142, tid=459942, ep=2, tvdb_ep_id=12018298):
        self.aid = aid
        self.tid = tid
        self.ep = ep
        self.tvdb_ep_id = tvdb_ep_id
        self.titles = ("Test Anime Series", "Test Anime Series Alt")

    async def test_job_7264_reproduction_authoritative_context_bypasses_offline_catalog(self):
        """Job #7264 reproduction: Plan #19, ep 2 (TVDB S01E02, ID 12018298).

        6 existing candidates FAILED with MEDIA_AUDIO_REQUIRED.
        Catalog provider is OFFLINE (raises ProviderUnavailable).
        Authoritative saved episode context allows Prowlarr search to proceed directly,
        finds new Dual Audio candidate, appends at rank 7, and leaves previous 6 candidates FAILED.
        """
        self.setUpFixture()
        self.store.update({"acquisition": {"audio_check": "japanese_english"}}, expected_revision=1)

        failed_hashes = [f"{i:02d}" + "a" * 38 for i in range(1, 7)]
        new_candidate_hash = "77" + "b" * 38

        db = connect(self.database)
        try:
            event, release_job, v_id = seed_plan(db, 19, self.aid, self.tid, anchor_ep=1, title=self.titles[0])
            with transaction(db):
                db.execute(
                    "INSERT INTO anime_title_metadata(anidb_id, english_title, romaji_title) VALUES(?, ?, ?)",
                    (self.aid, self.titles[0], self.titles[1])
                )
                db.execute(
                    "INSERT INTO recognized_seasons(tvdb_series_id, tvdb_season, anidb_id, display_title, "
                    "evidence_verification_id, recognized_at_ms) VALUES(?, 1, ?, ?, ?, 1000)",
                    (self.tid, self.aid, self.titles[0], v_id)
                )
                db.execute(
                    "INSERT INTO recognized_episodes(tvdb_series_id, tvdb_season, anidb_id, anidb_episode_number, "
                    "anidb_episode_id, tvdb_episode, tvdb_episode_id, evidence_verification_id, recognized_at_ms) "
                    "VALUES(?, 1, ?, ?, 99002, ?, ?, ?, 1000)",
                    (self.tid, self.aid, self.ep, self.ep, self.tvdb_ep_id, v_id)
                )

                # Seed the 6 failed candidates from Job #7264
                for rank, h in enumerate(failed_hashes, 1):
                    db.execute(
                        "INSERT INTO episode_release_candidates(plan_id, anidb_episode_number, rank, info_hash, state, reason_code) "
                        "VALUES(19, ?, ?, ?, 'FAILED', 'MEDIA_AUDIO_REQUIRED')",
                        (self.ep, rank, h)
                    )

                # Selection points to the last tried failed candidate
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id, anidb_episode_number, tvdb_season, tvdb_episode, "
                    "tvdb_episode_id, info_hash, selected_at_ms) VALUES(19, ?, 1, ?, ?, ?, 1000)",
                    (self.ep, self.ep, self.tvdb_ep_id, failed_hashes[-1])
                )

                # Enqueue exhausted candidate discovery job #7264
                job_id = enqueue_job(
                    db, kind=DISCOVER_KIND, component="worker", event_id=event,
                    payload={"plan_id": 19, "start": 1, "exhausted_episode": self.ep, "delete_cache": True},
                    available_at_ms=0
                )
        finally:
            db.close()

        # Mock Prowlarr client with an OFFLINE catalog that raises ProviderUnavailable
        def catalog_factory_offline(conf):
            def catalog(aid, tid):
                raise ProviderUnavailable("AniDB provider offline")
            return catalog

        row_failed = make_raw_prowlarr_row(
            f"[Group] {self.titles[0]} - 02 [Dual-Audio] (1080p).mkv",
            indexer="Nyaa", seeders=20, size=500_000_000,
            info_hash=failed_hashes[0], magnet="magnet:?xt=urn:btih:" + failed_hashes[0],
        )
        row_new = make_raw_prowlarr_row(
            f"[NewGroup] {self.titles[0]} - 02 [Dual-Audio] (1080p).mkv",
            indexer="Nyaa", seeders=25, size=600_000_000,
            info_hash=new_candidate_hash, magnet="magnet:?xt=urn:btih:" + new_candidate_hash,
        )

        def client_factory(conf, sec):
            policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, prefer_audio_tag=True)
            c = Client(Options("http://127.0.0.1:9696", 60), "key",
                       catalog=catalog_factory_offline(conf), policy=policy)
            c._transport = lambda req, timeout: None
            c._get = lambda q: [row_failed, row_new]
            return c

        registry = Registry((
            discovery_registration(None, client_factory=client_factory,
                                   select_candidates=True, acquire_candidates=True),
        ))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000))
        claim = await worker.attempt()
        self.assertIsNotNone(claim)
        # A fresh candidate must be routed through the original VERIFY_RELEASE
        # pipeline. It must not be acquired directly from Prowlarr discovery.
        fallback_claim = await worker.attempt()
        self.assertIsNotNone(fallback_claim)
        await worker.close()

        # Verify candidate recovery
        db = connect(self.database)
        try:
            job_state = db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
            self.assertEqual(job_state, "DONE")

            candidates = db.execute(
                "SELECT rank, info_hash, state, reason_code FROM episode_release_candidates "
                "WHERE plan_id=19 AND anidb_episode_number=? ORDER BY rank",
                (self.ep,)
            ).fetchall()
            self.assertEqual(len(candidates), 7)

            # First 6 candidates remain FAILED with MEDIA_AUDIO_REQUIRED
            for i in range(6):
                self.assertEqual(candidates[i][0], i + 1)
                self.assertEqual(candidates[i][1], failed_hashes[i])
                self.assertEqual(candidates[i][2], "FAILED")
                self.assertEqual(candidates[i][3], "MEDIA_AUDIO_REQUIRED")

            # 7th candidate is the new discovery
            self.assertEqual(candidates[6][0], 7)
            self.assertEqual(candidates[6][1], new_candidate_hash)
            # The second worker attempt activates the candidate through the
            # original fallback handoff, so it is now ACTIVE rather than READY.
            self.assertEqual(candidates[6][2], "ACTIVE")
            self.assertIsNone(candidates[6][3])

            successor = db.execute(
                "SELECT kind, payload_json FROM jobs WHERE kind=? AND job_id > ? ORDER BY job_id DESC LIMIT 1",
                (DISCOVER_KIND, job_id)
            ).fetchone()
            self.assertIsNotNone(successor)
            self.assertEqual(json.loads(successor[1]), {"plan_id": 19, "start": 1, "fallback_episode": self.ep})
            fallback_job = db.execute(
                "SELECT job_id, state FROM jobs WHERE kind=? AND created_by_job_id=?",
                (DISCOVER_KIND, job_id)
            ).fetchone()
            self.assertIsNotNone(fallback_job)
            self.assertEqual(fallback_job[1], "DONE")
            verify_jobs = db.execute(
                "SELECT job_id FROM jobs WHERE kind='VERIFY_RELEASE' AND created_by_job_id=?",
                (fallback_job[0],)
            ).fetchall()
            self.assertEqual(len(verify_jobs), 1, 'New release must enter VERIFY_RELEASE exactly once')
        finally:
            db.close()

    async def test_exhausted_search_excludes_tried_hashes_and_waits_for_dub(self):
        """When Prowlarr search only returns already-failed hashes, job enters WAITING_FOR_DUB with snapshots."""
        self.setUpFixture()
        self.store.update({"acquisition": {"audio_check": "japanese_english"}}, expected_revision=1)

        failed_hash = "11" + "a" * 38

        db = connect(self.database)
        try:
            event, release_job, v_id = seed_plan(db, 19, self.aid, self.tid, anchor_ep=1, title=self.titles[0])
            with transaction(db):
                db.execute(
                    "INSERT INTO episode_release_candidates(plan_id, anidb_episode_number, rank, info_hash, state, reason_code) "
                    "VALUES(19, ?, 1, ?, 'FAILED', 'MEDIA_AUDIO_REQUIRED')",
                    (self.ep, failed_hash)
                )
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id, anidb_episode_number, tvdb_season, tvdb_episode, "
                    "tvdb_episode_id, info_hash, selected_at_ms) VALUES(19, ?, 1, ?, ?, ?, 1000)",
                    (self.ep, self.ep, self.tvdb_ep_id, failed_hash)
                )
                job_id = enqueue_job(
                    db, kind=DISCOVER_KIND, component="worker", event_id=event,
                    payload={"plan_id": 19, "start": 1, "exhausted_episode": self.ep, "delete_cache": True},
                    available_at_ms=0
                )
        finally:
            db.close()

        row_only_failed = make_raw_prowlarr_row(
            f"[Group] {self.titles[0]} - 02 [Dual-Audio] (1080p).mkv",
            indexer="Nyaa", seeders=20, size=500_000_000,
            info_hash=failed_hash, magnet="magnet:?xt=urn:btih:" + failed_hash,
        )

        def client_factory(conf, sec):
            policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, prefer_audio_tag=True)
            c = Client(Options("http://127.0.0.1:9696", 60), "key",
                       catalog=lambda a, t: None, policy=policy)
            c._transport = lambda req, timeout: None
            c._get = lambda q: [row_only_failed]
            return c

        registry = Registry((
            discovery_registration(None, client_factory=client_factory,
                                   select_candidates=True, acquire_candidates=True),
        ))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000))
        await worker.attempt()
        await worker.close()

        db = connect(self.database)
        try:
            job_row = db.execute("SELECT state, last_reason_code FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self.assertEqual(job_row[0], "CANCELED")
            self.assertEqual(job_row[1], "WAITING_FOR_DUB")

            c_count = db.execute("SELECT count(*) FROM episode_release_candidates WHERE plan_id=19").fetchone()[0]
            self.assertEqual(c_count, 1)

            snap_count = db.execute("SELECT count(*) FROM prowlarr_search_snapshots WHERE job_id=?", (job_id,)).fetchone()[0]
            self.assertGreaterEqual(snap_count, 1)
        finally:
            db.close()

    def test_provider_outage_retries_while_identity_conflict_stops_for_review(self):
        """Distinguish provider outage (lets ProviderUnavailable raise) from identity conflict (SearchReview with failed_check)."""
        self.setUpFixture()

        # 1. When legacy VerifiedEpisode is used and catalog raises ProviderUnavailable -> it bubbles up
        def catalog_offline(aid, tid):
            raise ProviderUnavailable()

        policy = DiscoveryPolicy()
        options = Options("http://127.0.0.1:9696", 60)
        client_offline = Client(options, "apikey", catalog=catalog_offline, policy=policy)

        legacy_identity = VerifiedEpisode(self.tid, self.tvdb_ep_id, 1, self.ep)
        with self.assertRaises(ProviderUnavailable):
            client_offline.search(self.aid, self.ep, legacy_identity)

        # 2. When catalog returns conflicting placement -> stops with SearchReview and EPISODE_PLACEMENT_CONFLICT
        target_conflict = EpisodeTarget(
            tvdb_episode=99,  # Mismatched episode number (expected 2)
            tvdb_season=1,
            status="SEARCHABLE",
            anidb_episode=self.ep,
            tvdb_episode_id=self.tvdb_ep_id,
            absolute_episode=99,
        )
        catalog_conflict = EpisodeCatalog(self.titles, {self.ep: target_conflict})
        client_conflict = Client(options, "apikey", catalog=lambda a, t: catalog_conflict, policy=policy)

        with self.assertRaises(SearchReview) as cm:
            client_conflict.search(self.aid, self.ep, legacy_identity)
        self.assertIn("EPISODE_PLACEMENT_CONFLICT", cm.exception.failed_check)

    async def test_empty_search_queries_stops_for_review_with_failed_check(self):
        """When no search queries can be built from anime titles, stops for review with SEARCH_QUERIES_EMPTY."""
        self.setUpFixture()
        policy = DiscoveryPolicy()
        options = Options("http://127.0.0.1:9696", 60)
        client = Client(options, "apikey", policy=policy)

        # Context with Japanese Kanji only (no ascii titles), which causes search_aliases to return ()
        context = EpisodeSearchContext(
            tvdb_series_id=self.tid,
            tvdb_episode_id=self.tvdb_ep_id,
            tvdb_season=1,
            tvdb_episode=self.ep,
            anidb_id=self.aid,
            anidb_episode=self.ep,
            titles=("薬屋のひとりごと",),
        )
        with self.assertRaises(SearchReview) as cm:
            client.search(self.aid, self.ep, context)
        self.assertIn("SEARCH_QUERIES_EMPTY", cm.exception.failed_check)

    def test_adm_report_job_explains_pre_search_failure_without_snapshot(self):
        """adm report job explains why no Prowlarr request occurred when snapshot is None."""
        self.setUpFixture()

        db = connect(self.database)
        try:
            event, release_job, v_id = seed_plan(db, 19, self.aid, self.tid, anchor_ep=1, title=self.titles[0])
            with transaction(db):
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id, anidb_episode_number, tvdb_season, tvdb_episode, "
                    "tvdb_episode_id, info_hash, selected_at_ms) VALUES(19, ?, 1, ?, ?, ?, 1000)",
                    (self.ep, self.ep, self.tvdb_ep_id, "aa" * 20)
                )
                job_id = enqueue_job(
                    db, kind=DISCOVER_KIND, component="worker", event_id=event,
                    payload={"plan_id": 19, "start": 1, "exhausted_episode": self.ep},
                    available_at_ms=0
                )
                db.execute(
                    "UPDATE jobs SET state='NEEDS_REVIEW', last_reason_code='SERVICE_RESPONSE_INVALID' WHERE job_id=?",
                    (job_id,)
                )
                details = {
                    "attempt": 1,
                    "failed_check": "src/anime/acquisition/prowlarr_search.py:891 search EPISODE_PLACEMENT_CONFLICT",
                }
                db.execute(
                    "INSERT INTO job_history(job_id, event_code, reason_code, details_json, occurred_at_ms) "
                    "VALUES(?, 'OUTCOME_RECORDED', 'SERVICE_RESPONSE_INVALID', ?, 2000)",
                    (job_id, json.dumps(details))
                )

            out_lines = []
            report_prowlarr_matching(db, job_id, out=out_lines.append)
            output = "\n".join(out_lines)

            self.assertIn("No Prowlarr request was made (SERVICE_RESPONSE_INVALID)", output)
            self.assertIn("EPISODE_PLACEMENT_CONFLICT", output)
            self.assertIn(f"AniDB {self.aid}", output)
            self.assertIn(f"TVDB {self.tid}", output)
            self.assertIn(f"TVDB S01E02", output)
            self.assertIn(f"TVDB ID {self.tvdb_ep_id}", output)

            data = prowlarr_matching_json(db, job_id)
            self.assertFalse(data["recorded"])
            self.assertEqual(data["no_request_reason"], "SERVICE_RESPONSE_INVALID")
            self.assertIn("EPISODE_PLACEMENT_CONFLICT", data["failed_check"])
            self.assertEqual(data["plan_id"], 19)
            self.assertEqual(data["episode"], self.ep)
            self.assertEqual(data["anidb_id"], self.aid)
            self.assertEqual(data["tvdb_series_id"], self.tid)
        finally:
            db.close()

    def test_episode_context_validation_and_provenance(self):
        """EpisodeSearchContext validation bounds and provenance loading."""
        self.setUpFixture()

        # Rejects non-positive or empty
        with self.assertRaises(EpisodeIdentityInvalid):
            EpisodeSearchContext(
                tvdb_series_id=0,
                tvdb_episode_id=self.tvdb_ep_id,
                tvdb_season=1,
                tvdb_episode=self.ep,
                anidb_id=self.aid,
                anidb_episode=self.ep,
                titles=self.titles,
            )

        with self.assertRaises(EpisodeIdentityInvalid):
            EpisodeSearchContext(
                tvdb_series_id=self.tid,
                tvdb_episode_id=self.tvdb_ep_id,
                tvdb_season=1,
                tvdb_episode=self.ep,
                anidb_id=0,
                anidb_episode=self.ep,
                titles=self.titles,
            )

        with self.assertRaises(EpisodeIdentityInvalid):
            EpisodeSearchContext(
                tvdb_series_id=self.tid,
                tvdb_episode_id=self.tvdb_ep_id,
                tvdb_season=1,
                tvdb_episode=self.ep,
                anidb_id=self.aid,
                anidb_episode=self.ep,
                titles=(),
            )

        # Loading from DB
        db = connect(self.database)
        try:
            event, release_job, v_id = seed_plan(db, 19, self.aid, self.tid, anchor_ep=1, title=self.titles[0])
            with transaction(db):
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id, anidb_episode_number, tvdb_season, tvdb_episode, "
                    "tvdb_episode_id, info_hash, selected_at_ms) VALUES(19, ?, 1, ?, ?, ?, 1000)",
                    (self.ep, self.ep, self.tvdb_ep_id, "aa" * 20)
                )

            ctx = load_authoritative_episode_context(db, 19, self.ep)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.tvdb_series_id, self.tid)
            self.assertEqual(ctx.tvdb_episode_id, self.tvdb_ep_id)
            self.assertEqual(ctx.tvdb_season, 1)
            self.assertEqual(ctx.tvdb_episode, self.ep)
            self.assertEqual(ctx.anidb_id, self.aid)
            self.assertEqual(ctx.anidb_episode, self.ep)
            self.assertEqual(ctx.titles[0], self.titles[0])
            self.assertEqual(ctx.absolute_episode, self.ep)
            self.assertEqual(ctx.status, "SEARCHABLE")
            self.assertFalse(ctx.verified)
            self.assertIsNone(ctx.verification_id)
            self.assertEqual(ctx.provenance, "SAVED_SELECTION")

            with self.assertRaises(EpisodeContextMissing):
                load_authoritative_episode_context(db, 999, self.ep)
            with self.assertRaises(EpisodeContextMissing):
                load_authoritative_episode_context(db, 19, 999)
        finally:
            db.close()

    def test_deferred_prowlarr_facts_are_valid_without_contract_regression(self):
        from src.anime.job_contracts import Outcome
        from src.anime.storage.episode_discovery import AUTOMATIC_RETRY_MS
        for reason in ('RELEASE_SEARCH_TIMEOUT', 'SERVICE_UNAVAILABLE'):
            with self.subTest(reason=reason):
                outcome = Outcome('defer',reason,delay_ms=AUTOMATIC_RETRY_MS,
                    facts={'failed_check': 'src/anime/acquisition/prowlarr_search.py:63 search PROWLARR_TIMEOUT'})
                self.assertEqual(outcome.facts['failed_check'].split()[-1],'PROWLARR_TIMEOUT')
        with self.assertRaises(ValueError):
            Outcome('complete', facts={'failed_check': 'src/anime/acquisition/prowlarr_search.py:63 search PROWLARR_TIMEOUT'})

    def test_saved_search_context_never_calls_provider_catalog(self):
        self.setUpFixture()
        queries=[]
        def unavailable(*args):
            raise AssertionError('Provider catalog accessed with saved episode context')
        client=Client(Options('http://127.0.0.1:9696',60),'key',
                      catalog=unavailable,policy=DiscoveryPolicy())
        client._get=lambda query: queries.append(query) or []
        context=EpisodeSearchContext(
            tvdb_series_id=self.tid,tvdb_episode_id=self.tvdb_ep_id,
            tvdb_season=1,tvdb_episode=2,anidb_id=self.aid,
            anidb_episode=2,titles=('Original Series','English Series','Romaji Series'),
            english_title='English Series',verified=False)
        result=client.search(self.aid,2,context)
        self.assertEqual(result.tvdb_episode_id,self.tvdb_ep_id)
        self.assertTrue(any('Original Series' in q for q in queries))
        self.assertTrue(any('English Series' in q for q in queries))
        self.assertTrue(any('Romaji Series' in q for q in queries))

    def test_persisted_identity_conflict_fails_closed(self):
        self.setUpFixture()
        db=connect(self.database)
        try:
            _,_,anchor=seed_plan(db,19,self.aid,self.tid,anchor_ep=1,title=self.titles[0])
            with transaction(db):
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,"
                    "tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms) VALUES(19,2,1,2,?,?,1000)",
                    (self.tvdb_ep_id,"aa"*20))
                db.execute(
                    "INSERT INTO recognized_seasons(tvdb_series_id,tvdb_season,anidb_id,"
                    "display_title,evidence_verification_id,recognized_at_ms) "
                    "VALUES(?,1,?,?,?,1000)",
                    (self.tid,self.aid,self.titles[0],anchor))
                db.execute(
                    "INSERT INTO recognized_episodes(tvdb_series_id,tvdb_season,anidb_id,"
                    "anidb_episode_number,anidb_episode_id,tvdb_episode,tvdb_episode_id,"
                    "evidence_verification_id,recognized_at_ms) VALUES(?,1,?,?,99002,99,?,?,1000)",
                    (self.tid,self.aid,self.ep,self.tvdb_ep_id+1,anchor))
            from src.anime.storage.episode_discovery import _selected
            for callback in (lambda: load_authoritative_episode_context(db,19,2),
                             lambda: _selected(db,19,2)):
                with self.assertRaises(EpisodeContextConflict) as ex:
                    callback()
                self.assertEqual(ex.exception.context_left,
                                 f'selection:S1E2#{self.tvdb_ep_id}')
                self.assertEqual(ex.exception.context_right,
                                 f'recognized:S1E99#{self.tvdb_ep_id+1}')
        finally:
            db.close()

    def test_storage_failure_is_not_missing_identity(self):
        class Broken:
            def execute(self,*args):
                raise sqlite3.OperationalError('unavailable')
        with self.assertRaises(sqlite3.OperationalError):
            load_authoritative_episode_context(Broken(),19,2)

    def test_independent_episode_verification_and_provenance(self):
        self.setUpFixture()
        db=connect(self.database)
        try:
            _,_,vid=seed_plan(db,19,self.aid,self.tid,anchor_ep=2,title=self.titles[0])
            with transaction(db):
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,"
                    "tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms) VALUES(19,2,1,2,?,?,1000)",
                    (110002,"bb"*20))
            ctx=load_authoritative_episode_context(db,19,2)
            self.assertTrue(ctx.verified)
            self.assertEqual(ctx.verification_id,vid)
            self.assertEqual(ctx.provenance,'SAVED_SELECTION')
            from dataclasses import replace
            for values in ({'provenance':'ARBITRARY'}, {'verified':False},
                           {'verification_id':None}):
                with self.subTest(values=values), self.assertRaises(EpisodeIdentityInvalid):
                    replace(ctx,**values)
        finally:
            db.close()

    async def test_persisted_conflict_worker_review_with_identity_facts(self):
        self.setUpFixture()
        self.store.update({'acquisition': {'audio_check': 'japanese_english'}},
                          expected_revision=1)
        db=connect(self.database)
        try:
            event,_,anchor=seed_plan(db,19,self.aid,self.tid,anchor_ep=1,title=self.titles[0])
            with transaction(db):
                failed="aa"*20
                db.execute(
                    "INSERT INTO episode_release_candidates(plan_id,anidb_episode_number,rank,info_hash,"
                    "state,reason_code) VALUES(19,2,1,?,'FAILED','MEDIA_AUDIO_REQUIRED')",
                    (failed,))
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,"
                    "tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms) VALUES(19,2,1,2,?,?,1000)",
                    (self.tvdb_ep_id,failed))
                db.execute(
                    "INSERT INTO recognized_seasons(tvdb_series_id,tvdb_season,anidb_id,"
                    "display_title,evidence_verification_id,recognized_at_ms) "
                    "VALUES(?,1,?,?,?,1000)",
                    (self.tid,self.aid,self.titles[0],anchor))
                db.execute(
                    "INSERT INTO recognized_episodes(tvdb_series_id,tvdb_season,anidb_id,"
                    "anidb_episode_number,anidb_episode_id,tvdb_episode,tvdb_episode_id,"
                    "evidence_verification_id,recognized_at_ms) VALUES(?,1,?,?,99002,99,?,?,1000)",
                    (self.tid,self.aid,2,self.tvdb_ep_id+1,anchor))
                job=enqueue_job(db,kind=DISCOVER_KIND,component='worker',event_id=event,
                                payload={'plan_id':19,'start':1,'exhausted_episode':2,
                                         'delete_cache':True},available_at_ms=0)
        finally:
            db.close()
        def provider_offline(*a):
            raise AssertionError('Conflicting saved evidence must stop before catalog use')
        def factory(configuration,secrets):
            return Client(Options('http://127.0.0.1:9696',60),'key',
                          catalog=provider_offline,policy=DiscoveryPolicy(prefer_audio_tag=True))
        worker=Worker(self.store,self.startup,Registry((
            discovery_registration(None,client_factory=factory,
                                   select_candidates=True,acquire_candidates=True),
        )),policy=Policy(lease_ms=30000))
        await worker.attempt()
        await worker.close()
        db=connect(self.database)
        try:
            state,reason=db.execute(
                'SELECT state,last_reason_code FROM jobs WHERE job_id=?',(job,)
            ).fetchone()
            self.assertEqual((state,reason),('NEEDS_REVIEW','EPISODE_CONTEXT_CONFLICT'))
            details=db.execute(
                "SELECT details_json FROM job_history WHERE job_id=? AND reason_code=?",
                (job,'EPISODE_CONTEXT_CONFLICT')).fetchone()
            self.assertIsNotNone(details)
            detail=json.loads(details[0])
            self.assertIn('CONTEXT_CONFLICT',detail['failed_check'])
            self.assertIn('selection:S1E2',detail['context_left'])
            self.assertIn('recognized:S1E99',detail['context_right'])
        finally:
            db.close()

    def _prowlarr_worker(self, error_type):
        self.setUpFixture()
        self.store.update({'acquisition': {'audio_check': 'japanese_english'}},
                          expected_revision=1)
        failed_hash="aa"*20
        db=connect(self.database)
        try:
            event,_,_=seed_plan(db,19,self.aid,self.tid,anchor_ep=1,title=self.titles[0])
            with transaction(db):
                db.execute(
                    "INSERT INTO episode_release_candidates(plan_id,anidb_episode_number,rank,info_hash,"
                    "state,reason_code) VALUES(19,2,1,?,'FAILED','MEDIA_AUDIO_REQUIRED')",
                    (failed_hash,))
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,"
                    "tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms) VALUES(19,2,1,2,?,?,1000)",
                    (self.tvdb_ep_id,failed_hash))
                job=enqueue_job(db,kind=DISCOVER_KIND,component='worker',event_id=event,
                                payload={'plan_id':19,'start':1,'exhausted_episode':2,
                                         'delete_cache':True},available_at_ms=0)
        finally:
            db.close()
        failed_check='src/anime/acquisition/prowlarr_search.py:63 search PROWLARR_FAILURE'
        def factory(configuration,secrets):
            client=Client(Options('http://127.0.0.1:9696',60),'key',
                          catalog=lambda *args: (_ for _ in ()).throw(
                              AssertionError('Provider catalog unexpectedly called')),
                          policy=DiscoveryPolicy(prefer_audio_tag=True))
            def unavailable(query):
                raise error_type(failed_check=failed_check)
            client._get=unavailable
            return client
        worker=Worker(self.store,self.startup,Registry((
            discovery_registration(None,client_factory=factory,
                                   select_candidates=True,acquire_candidates=True),
        )),policy=Policy(lease_ms=30000))
        return job,failed_check,worker

    async def _run_prowlarr_retry_diagnostic(self, error_type):
        job,failed_check,worker=self._prowlarr_worker(error_type)
        try:
            await worker.attempt()
            db=connect(self.database)
            try:
                state,reason=db.execute('SELECT state,last_reason_code FROM jobs WHERE job_id=?',
                                        (job,)).fetchone()
                self.assertEqual((state,reason),('RETRY_WAIT',error_type.reason_code))
                first=db.execute(
                    "SELECT details_json FROM job_history WHERE job_id=? AND event_code='DEFERRED'",
                    (job,)).fetchone()
                self.assertIsNotNone(first)
                self.assertEqual(json.loads(first[0])['failed_check'],failed_check)
                with transaction(db):
                    db.execute('UPDATE jobs SET available_at_ms=0 WHERE job_id=?',(job,))
            finally:
                db.close()
            await worker.attempt()
        finally:
            await worker.close()
        db=connect(self.database)
        try:
            state,reason=db.execute('SELECT state,last_reason_code FROM jobs WHERE job_id=?',
                                    (job,)).fetchone()
            self.assertEqual((state,reason),('BLOCKED',error_type.reason_code))
            rows=db.execute(
                "SELECT event_code,details_json FROM job_history WHERE job_id=? "
                "AND event_code IN ('DEFERRED','BLOCKED') ORDER BY history_id",(job,)
            ).fetchall()
            self.assertEqual([row[0] for row in rows],['DEFERRED','BLOCKED'])
            self.assertTrue(all(json.loads(row[1])['failed_check']==failed_check for row in rows))
        finally:
            db.close()

    async def test_prowlarr_timeout_defers_then_blocks_with_diagnostics(self):
        await self._run_prowlarr_retry_diagnostic(SearchTimeout)

    async def test_prowlarr_unavailable_waits_for_the_service_with_diagnostics(self):
        # Refactor (service health): a Prowlarr outage is a service wait, not a
        # defer-then-block. NextGen's point stays: the failed check is recorded.
        await self._run_prowlarr_service_wait(SearchUnavailable)

    async def _run_prowlarr_service_wait(self, error_type):
        job, failed_check, worker = self._prowlarr_worker(error_type)
        try:
            await worker.attempt()
        finally:
            await worker.close()
        db = connect(self.database)
        try:
            self.assertEqual(db.execute('SELECT state,last_reason_code FROM jobs WHERE job_id=?', (job,)).fetchone(),
                             ('RETRY_WAIT', 'SERVICE_UNAVAILABLE'))
            details = json.loads(db.execute(
                "SELECT details_json FROM job_history WHERE job_id=? AND event_code='RETRY_SCHEDULED'",
                (job,)).fetchone()[0])
            self.assertEqual((details['service'], details['failed_check']), ('prowlarr', failed_check))
        finally:
            db.close()

    def test_job_7264_spaced_titles_survive_recognition_keys(self):
        """Reproduce the literal collapsed Prowlarr query from Job #7264."""
        from src.anime.providers.parsing import norm
        from src.anime.acquisition.prowlarr_search import search_aliases

        self.setUpFixture()
        japanese = '目覚めたら最強装備と宇宙船持ちだったので'
        english = 'Reborn as a Space Mercenary I Woke Up Piloting the Strongest Starship'
        romaji = 'Mezametara Saikyou Soubi to Uchuusen Mochi Datta no de'
        compact = norm(english)
        self.assertEqual(
            compact, 'rebornasaspacemercenaryiwokeuppilotingthestrongeststarship'
        )

        db = connect(self.database)
        try:
            _, _, verification = seed_plan(
                db, 19, self.aid, self.tid, anchor_ep=1, title=japanese
            )
            with transaction(db):
                db.execute(
                    "INSERT INTO recognized_seasons(tvdb_series_id,tvdb_season,anidb_id,"
                    "display_title,evidence_verification_id,recognized_at_ms) "
                    "VALUES(?,1,?,?,?,1000)",
                    (self.tid, self.aid, japanese, verification),
                )
                db.execute(
                    "INSERT INTO recognized_aliases(alias_key,tvdb_season,tvdb_series_id,anidb_id) "
                    "VALUES(?,1,?,?)", (compact, self.tid, self.aid)
                )
                db.execute(
                    "INSERT INTO anime_title_metadata(anidb_id,english_title,romaji_title) "
                    "VALUES(?,?,?)", (self.aid, english, romaji)
                )
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id,anidb_episode_number,"
                    "tvdb_season,tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms) "
                    "VALUES(19,2,1,2,?,?,1000)",
                    (self.tvdb_ep_id, 'aa' * 20)
                )
            context = load_authoritative_episode_context(db, 19, 2)
        finally:
            db.close()

        self.assertEqual(context.titles[0], japanese)
        self.assertIn(english, context.titles)
        self.assertIn(romaji, context.titles)
        self.assertNotIn(compact, context.titles)
        self.assertEqual(context.english_title, english)

        # No catalogue or live provider request should rebuild known identity.
        requests = []
        def unavailable_catalog(*args):
            raise AssertionError('Saved identity must not reload provider catalogue')
        client = Client(
            Options('http://127.0.0.1:9696', 60), 'key',
            catalog=unavailable_catalog,
            policy=DiscoveryPolicy(prefer_audio_tag=True),
        )
        client._get = lambda query: requests.append(query) or []
        result = client.search(self.aid, self.ep, context)
        self.assertEqual(result.tvdb_episode_id, self.tvdb_ep_id)
        self.assertIn(f'{english} S01E02', requests)
        self.assertTrue(any('Dual-Audio' in query for query in requests))
        self.assertTrue(all(compact not in query for query in requests))
        self.assertLessEqual(len(requests), 6)

        # A legacy compact value must also lose deduplication precedence to
        # the exact same alias when a human-readable spelling is available.
        self.assertEqual(
            search_aliases((compact, english), english_title=english),
            (english,)
        )

    def test_recognition_key_alone_is_not_a_searchable_alias(self):
        """Do not silently send an unspaced key when no readable Latin title exists."""
        from src.anime.providers.parsing import norm

        self.setUpFixture()
        japanese = '目覚めたら最強装備と宇宙船持ちだったので'
        english = 'Reborn as a Space Mercenary I Woke Up Piloting the Strongest Starship'
        compact = norm(english)
        db = connect(self.database)
        try:
            _, _, verification = seed_plan(
                db, 19, self.aid, self.tid, anchor_ep=1, title=japanese
            )
            with transaction(db):
                db.execute(
                    "INSERT INTO recognized_seasons(tvdb_series_id,tvdb_season,anidb_id,"
                    "display_title,evidence_verification_id,recognized_at_ms) "
                    "VALUES(?,1,?,?,?,1000)",
                    (self.tid, self.aid, japanese, verification),
                )
                db.execute(
                    "INSERT INTO recognized_aliases(alias_key,tvdb_season,tvdb_series_id,anidb_id) "
                    "VALUES(?,1,?,?)", (compact, self.tid, self.aid)
                )
                db.execute(
                    "INSERT INTO episode_release_selections(plan_id,anidb_episode_number,"
                    "tvdb_season,tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms) "
                    "VALUES(19,2,1,2,?,?,1000)",
                    (self.tvdb_ep_id, 'aa' * 20),
                )
            context = load_authoritative_episode_context(db, 19, self.ep)
        finally:
            db.close()

        self.assertNotIn(compact, context.titles)
        requested = []
        client = Client(Options('http://127.0.0.1:9696', 60), 'key',
                        policy=DiscoveryPolicy(prefer_audio_tag=True))
        client._get = lambda query: requested.append(query) or []
        with self.assertRaises(SearchReview) as error:
            client.search(self.aid, self.ep, context)
        self.assertIn('SEARCH_QUERIES_EMPTY', error.exception.failed_check)
        self.assertEqual(requested, [])
