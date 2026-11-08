"""Regression tests for Prowlarr matching candidate history reconciliation and WAITING_FOR_DUB explanation."""
import io
import json
import unittest
from pathlib import Path

from src.anime.acquisition.audio import dual_audio_tagged
from src.anime.acquisition.media import AudioMissing, parse_probe
from src.anime.acquisition.prowlarr_search import (
    Client, DiscoveryPolicy, EpisodeCatalog, EpisodeTarget, Options,
)
from src.anime.job_contracts import Policy, Registry
from src.anime.job_report import main as report_main
from src.anime.jobs.discover_episode_releases_job import (
    KIND as DISCOVER_KIND, registration as discovery_registration,
)
from src.anime.jobs.select_user_evaluation_job import registration as user_evaluation_registration
from src.anime.storage.prowlarr_snapshots import save_snapshots
from src.anime.storage.sqlite import connect, enqueue_job, transaction
from src.anime.tests.test_prowlarr_matching_report import make_raw_prowlarr_row
from src.anime.tests.test_user_evaluation_policy import UserEvaluationCheckTests
from src.anime.worker import Worker


class ProwlarrCandidateReconciliationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = UserEvaluationCheckTests.asyncSetUp
    rows = UserEvaluationCheckTests.rows
    source = UserEvaluationCheckTests.source

    def setUpFixture(self):
        self.titles = ("Some Show", "Some Show Alt")
        self.target_ep1 = EpisodeTarget(
            tvdb_episode=1,
            tvdb_season=1,
            status="SEARCHABLE",
            anidb_episode=1,
            tvdb_episode_id=1001,
            absolute_episode=1,
        )
        self.catalog = EpisodeCatalog(self.titles, {1: self.target_ep1})

    def run_report_capture(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = report_main(list(argv))
            except SystemExit as exc:
                status = exc.code
        out_str = out.getvalue()
        err_str = err.getvalue()
        return status, out_str if out_str else err_str

    def test_job_5775_reproduction_reconciles_candidate_history_and_explains_hold(self):
        """Job #5775 reproduction: MULTi AD 720p release with already-FAILED MEDIA_AUDIO_REQUIRED hash.

        Report displays passes policy, candidate history FAILED (MEDIA_AUDIO_REQUIRED),
        zero new actionable; candidate reconciliation explains the hold.
        """
        self.setUpFixture()
        failed_hash = "2e8d012539d7" + "0" * 28

        # Build 37 raw results: 36 unmatched, row #029 matches EP1 with MULTi AD 720p
        rows_q1 = []
        for i in range(1, 38):
            if i == 29:
                rows_q1.append(make_raw_prowlarr_row(
                    "[SubsPlease] Some Show - 01 (720p) [MULTi AD].mkv",
                    indexer="Nyaa",
                    seeders=15,
                    size=450_000_000,
                    info_hash=failed_hash,
                ))
            else:
                rows_q1.append(make_raw_prowlarr_row(
                    f"[SubsPlease] Some Show - 04 (1080p) [Other_{i:02d}].mkv",
                    indexer="Nyaa",
                    seeders=10,
                    size=500_000_000,
                    info_hash=f"bb{i:02d}" + "0" * 36,
                ))

        policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, prefer_audio_tag=True)
        options = Options("http://127.0.0.1:9696", 60)
        client = Client(options, "apikey123", catalog=lambda a, t: self.catalog, policy=policy)
        client._transport = lambda req, timeout: None
        client._get = lambda q: rows_q1 if q == "Some Show 01" else []
        client.set_context(plan_id=19, search_mode="exhausted")

        ranked = client._search(self.catalog, 1, self.target_ep1)
        self.assertEqual(len(ranked), 1)

        snapshot = client.snapshots[0]
        self.assertEqual(snapshot["counts"]["raw"], 37)
        self.assertEqual(snapshot["counts"]["matched"], 1)
        self.assertEqual(snapshot["counts"]["passes_policy"], 1)
        self.assertEqual(snapshot["counts"]["dispositions"]["PASSES_POLICY"], 1)

        # Row 29 is the MULTi AD 720p release
        r29 = snapshot["results"][28]
        self.assertEqual(r29["row_number"], 29)
        self.assertEqual(r29["primary_disposition"], "PASSES_POLICY")
        self.assertTrue(r29["has_dual_audio_hint"])
        self.assertEqual(r29["resolution"], 720)

        # Seed plan #19 and historical failed candidate in DB
        self.source(7)
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("INSERT OR REPLACE INTO user_evaluation_plans VALUES(19, 123, 456, 1, 'official', 1, 10, 6, 2, 1, 0)")
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={"plan_id": 19, "start": 1, "exhausted_episode": 1},
                                     component="worker", available_at_ms=0)
                db.execute(
                    "INSERT INTO episode_release_candidates VALUES(19, 1, 1, ?, 'FAILED', 'MEDIA_AUDIO_REQUIRED')",
                    (failed_hash,)
                )
                save_snapshots(db, job_id, attempt=6, snapshots=[snapshot])
        finally:
            db.close()

        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)

        # Reconciled counts in Search results header
        self.assertIn("37 raw · 36 unmatched · 1 EP1 matched · 1 passes policy (0 newly actionable, 1 previously tried)", output)
        self.assertIn("Prior database attempts: 1", output)

        # Candidate history reconciliation section
        self.assertIn("Candidate history reconciliation & hold explanation:", output)
        self.assertIn("Policy-accepted results : 1 (1 previously tried, 0 newly actionable)", output)
        self.assertIn("Policy-rejected results : 36", output)
        self.assertIn("Previous failure reasons: MEDIA_AUDIO_REQUIRED: 1", output)
        self.assertIn("Audio diagnostic (#01 2e8d0125): Not recorded", output)
        self.assertIn("Release-title hints such as MULTi AD or Dual Audio are search hints, not verified audio streams", output)
        self.assertIn("Hold explanation (WAITING_FOR_DUB): All previous candidates failed (with at least one audio failure) and 0 new actionable releases were found. Job remains on hold; failed torrents are not re-downloaded.", output)

        # All releases table displays Policy outcome and Candidate history separately
        self.assertIn("Policy outcome", output)
        self.assertIn("Candidate history", output)
        self.assertIn("PASSES_POLICY", output)
        self.assertIn("FAILED (MEDIA_AUDIO_REQUIRED)", output)

        # Detailed reason for row #029
        self.assertIn("#029 passes policy checks; candidate history: FAILED (MEDIA_AUDIO_REQUIRED) - previously tried, not actionable", output)

        # Check JSON structure
        code_json, out_json = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr", "--json",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code_json, 0)
        data = json.loads(out_json)
        reconcile = data["matching"]["prowlarr"]["candidate_reconciliation"]
        self.assertEqual(reconcile["policy_accepted_count"], 1)
        self.assertEqual(reconcile["previously_tried_count"], 1)
        self.assertEqual(reconcile["newly_actionable_count"], 0)
        self.assertTrue(reconcile["is_waiting_for_dub"])
        self.assertIn("remains on hold", reconcile["hold_explanation"])

        release29 = data["matching"]["prowlarr"]["releases"][28]
        self.assertEqual(release29["primary_disposition"], "PASSES_POLICY")
        self.assertTrue(release29["candidate_history"]["recorded"])
        self.assertEqual(release29["candidate_history"]["state"], "FAILED")
        self.assertEqual(release29["candidate_history"]["reason_code"], "MEDIA_AUDIO_REQUIRED")

    async def test_job_5775_worker_stays_on_hold_does_not_reacquire(self):
        """Worker executing DISCOVER_EPISODE_RELEASES on exhausted Job #5775 remains ON_HOLD/WAITING_FOR_DUB without reacquiring."""
        self.setUpFixture()
        self.store.update({"acquisition": {"audio_check": "japanese_english"}}, expected_revision=1)
        self.source(7)

        # Initialize trial plan and first page
        reg_eval = Registry((user_evaluation_registration(discovery_kind=DISCOVER_KIND),))
        w_eval = Worker(self.store, self.startup, reg_eval, policy=Policy(lease_ms=30000))
        await w_eval.attempt()
        await w_eval.close()

        failed_hash = "2e8d012539d7" + "0" * 28

        db = connect(self.database)
        try:
            with transaction(db):
                event_id = db.execute("SELECT event_id FROM jobs WHERE kind=?", (DISCOVER_KIND,)).fetchone()[0]
                db.execute("UPDATE jobs SET state='DONE' WHERE kind=?", (DISCOVER_KIND,))
                db.execute(
                    "INSERT INTO episode_release_candidates VALUES(1, 1, 1, ?, 'FAILED', 'MEDIA_AUDIO_REQUIRED')",
                    (failed_hash,)
                )
                db.execute("INSERT INTO episode_release_selections VALUES(1, 1, 1, 1, 1001, ?, 1)", (failed_hash,))
                job_id = enqueue_job(db, kind=DISCOVER_KIND, component="worker", event_id=event_id,
                                     payload={"plan_id": 1, "start": 1, "exhausted_episode": 1, "delete_cache": True},
                                     available_at_ms=0)
        finally:
            db.close()

        # Mock Prowlarr returning the exact same failed hash with MULTi AD
        raw_row = make_raw_prowlarr_row(
            "[SubsPlease] Some Show - 01 (720p) [MULTi AD].mkv",
            indexer="Nyaa", seeders=15, size=450_000_000,
            info_hash=failed_hash, magnet="magnet:?xt=urn:btih:" + failed_hash,
        )

        def client_factory(conf, sec):
            policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, prefer_audio_tag=True)
            c = Client(Options("http://127.0.0.1:9696", 60), "key",
                       catalog=lambda a, t: self.catalog, policy=policy)
            c._transport = lambda req, timeout: None
            c._get = lambda q: [raw_row]
            return c

        registry = Registry((
            discovery_registration(None, client_factory=client_factory,
                                   select_candidates=True, acquire_candidates=True),
        ))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000))
        try:
            await worker.attempt()
        finally:
            await worker.close()

        # Verify job is CANCELED with WAITING_FOR_DUB
        job_state = self.rows("SELECT state, last_reason_code FROM jobs WHERE job_id=?", (job_id,))[0]
        self.assertEqual(job_state, ("CANCELED", "WAITING_FOR_DUB"))

        # Verify candidate table still has only 1 row (the failed hash) and was NOT reacquired
        candidates = self.rows("SELECT rank, info_hash, state, reason_code FROM episode_release_candidates WHERE plan_id=1 AND anidb_episode_number=1")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0], (1, failed_hash, "FAILED", "MEDIA_AUDIO_REQUIRED"))

    def test_mixed_failure_cases_explanation(self):
        """Mixed failures: one metadata timeout + one audio failure; hold explanation explains WAITING_FOR_DUB."""
        self.setUpFixture()
        hash1 = "11111111" + "0" * 32
        hash2 = "22222222" + "0" * 32

        # Prowlarr search returns row matching hash2
        raw_row = make_raw_prowlarr_row(
            "[Group] Some Show - 01 (1080p) [Dual-Audio].mkv",
            seeders=15, size=450_000_000, info_hash=hash2,
        )

        policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, prefer_audio_tag=True)
        client = Client(Options("http://127.0.0.1:9696", 60), "key",
                        catalog=lambda a, t: self.catalog, policy=policy)
        client._transport = lambda req, timeout: None
        client._get = lambda q: [raw_row] if "01" in q else []
        client.set_context(plan_id=1, search_mode="exhausted")

        client._search(self.catalog, 1, self.target_ep1)
        snapshot = client.snapshots[0]

        self.source(7)
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("INSERT OR REPLACE INTO user_evaluation_plans VALUES(1, 123, 456, 1, 'official', 1, 10, 6, 2, 1, 0)")
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={"plan_id": 1, "start": 1},
                                     component="worker", available_at_ms=0)
                # Mixed failures in candidates: rank 1 metadata timeout, rank 2 audio required
                db.execute("INSERT INTO episode_release_candidates VALUES(1, 1, 1, ?, 'FAILED', 'TORRENT_METADATA_TIMEOUT')", (hash1,))
                db.execute("INSERT INTO episode_release_candidates VALUES(1, 1, 2, ?, 'FAILED', 'MEDIA_AUDIO_REQUIRED')", (hash2,))
                save_snapshots(db, job_id, attempt=3, snapshots=[snapshot])
        finally:
            db.close()

        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn("Previous failure reasons: MEDIA_AUDIO_REQUIRED: 1, TORRENT_METADATA_TIMEOUT: 1", output)
        self.assertIn("Hold explanation (WAITING_FOR_DUB): All previous candidates failed (with at least one audio failure)", output)
        self.assertIn("FAILED (MEDIA_AUDIO_REQUIRED)", output)

    def test_multi_ad_admitted_as_hint_not_verified_audio(self):
        """MULTi AD is admitted as a search hint for probe, but is NOT evidence of Japanese+English streams."""
        title = "[Group] Some Show - 01 [720p MULTi AD].mkv"
        # 1) Release title hint check
        self.assertTrue(dual_audio_tagged(title))

        # 2) Actual probe with only French audio must fail verification
        doc_french = {
            "format": {"format_name": "matroska", "duration": "120.0"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "fra"}, "disposition": {"comment": 0}},
            ],
        }
        with self.assertRaises(AudioMissing) as exc_ctx:
            parse_probe(json.dumps(doc_french).encode(), ".mkv", audio="japanese_english")
        exc = exc_ctx.exception
        self.assertEqual(exc.observed, ("fra",))
        self.assertEqual(exc.required, ("eng", "jpn"))
        self.assertEqual(exc.missing, ("eng", "jpn"))

        # 3) Actual probe with Japanese only must also fail Japanese+English verification
        doc_japanese = {
            "format": {"format_name": "matroska", "duration": "120.0"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "ja"}, "disposition": {"comment": 0}},
            ],
        }
        with self.assertRaises(AudioMissing) as exc_ctx:
            parse_probe(json.dumps(doc_japanese).encode(), ".mkv", audio="japanese_english")
        exc = exc_ctx.exception
        self.assertEqual(exc.observed, ("jpn",))
        self.assertEqual(exc.required, ("eng", "jpn"))
        self.assertEqual(exc.missing, ("eng",))

    def test_audio_diagnostic_evidence_preserved_in_job_history_and_report(self):
        """Observed audio tags and missing required tags are preserved and rendered in report."""
        self.source(10)
        hash_val = "33333333" + "0" * 32
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("INSERT OR REPLACE INTO user_evaluation_plans VALUES(1, 123, 456, 1, 'official', 1, 10, 6, 2, 1, 0)")
                db.execute("INSERT INTO episode_reservations VALUES(1, 1, 2, 1, 1, 123, 11010, 456, 1, 10, 111010, 'official', 1, 'ACTIVE', NULL, NULL)")
                # Operation and jobs setup
                db.execute("INSERT INTO job_operations VALUES(10, 'ACTIVE', 'NONE', NULL, 0, 0)")
                job_id = enqueue_job(db, kind="VERIFY_MEDIA", payload={"preparation_job_id": 1, "download_job_id": 2},
                                     component="worker", available_at_ms=0, operation_id=10)
                db.execute("UPDATE jobs SET attempt_count=1 WHERE job_id=?", (job_id,))
                db.execute("INSERT INTO acquisition_plans(plan_id, job_id, reservation_id, info_hash, info_sha256, manifest_json, submission_marker, state, configuration_revision, prepared_at_ms) VALUES(1, ?, 1, ?, NULL, '{}', ?, 'PREPARED', 1, 0)", (job_id, hash_val, 'a'*32))
                db.execute("INSERT INTO episode_release_candidates VALUES(1, 1, 1, ?, 'FAILED', 'MEDIA_AUDIO_REQUIRED')", (hash_val,))

                # Record job_history with observed_audio and missing_audio details
                from src.anime.history import append_job_history
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW", reason_code="MEDIA_AUDIO_REQUIRED",
                    component="worker", occurred_at_ms=1000,
                    details={"attempt": 1, "observed_audio": "fra", "missing_audio": "eng,jpn"}
                )

                snap = {
                    "search_mode": "exhausted", "status": "COMPLETE", "plan_id": 1, "episode": 1,
                    "counts": {"raw": 1, "matched": 1, "dispositions": {"PASSES_POLICY": 1}},
                    "results": [make_raw_prowlarr_row("[Group] Show - 01 [720p MULTi AD].mkv", info_hash=hash_val)],
                    "policy": {"max_resolution": 1080},
                }
                save_snapshots(db, job_id, attempt=1, snapshots=[snap])
        finally:
            db.close()

        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        # History event audio check line
        self.assertIn("audio check: observed=[fra] missing required=[eng,jpn] (ffprobe verified)", output)
        # Matching candidate reconciliation audio diagnostic line
        self.assertIn("Audio diagnostic (#01 33333333): observed=[fra], missing required=[eng,jpn] (ffprobe verified)", output)


if __name__ == "__main__":
    unittest.main()
