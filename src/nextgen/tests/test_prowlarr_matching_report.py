"""Tests for explainable Prowlarr matching reports in adm report."""
import io
import json
import os
import re
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from src.nextgen.acquisition.prowlarr_search import (
    Client, DiscoveryPolicy, EpisodeCatalog, EpisodeTarget, Options, SearchTimeout,
)
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.job_contracts import Outcome, Policy, Registry
from src.nextgen.job_report import main as report_main
from src.nextgen.jobs.discover_sonarr_backfill_job import (
    KIND as DISCOVER_KIND, registration as discovery_registration,
)
from src.nextgen.jobs.evaluate_trial_job import registration as trial_registration
from src.nextgen.sonarr_episode import VerifiedEpisode
from src.nextgen.storage.prowlarr_snapshots import (
    load_latest_snapshot, prune_snapshots, sanitize_text, save_snapshots,
)
from src.nextgen.storage.sqlite import connect, enqueue_job, initialize, transaction
from src.nextgen.tests.test_trial_policy import TrialEvaluationTests
from src.nextgen.worker import Worker


def make_raw_prowlarr_row(title, *, indexer="SubsPlease", protocol="torrent", seeders=10,
                          size=500_000_000, info_hash=None, magnet=None):
    row = {
        "title": title,
        "indexer": indexer,
        "protocol": protocol,
        "seeders": seeders,
        "size": size,
    }
    if info_hash is not None:
        row["infoHash"] = info_hash
    if magnet is not None:
        row["magnetUrl"] = magnet
    return row


class ProwlarrMatchingReportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = TrialEvaluationTests.asyncSetUp
    rows = TrialEvaluationTests.rows
    source = TrialEvaluationTests.source

    def setUpFixture(self):
        self.titles = ("FX Senshi Kurumi-chan", "FX Fighter Kurumi-chan")
        self.target_ep1 = EpisodeTarget(
            tvdb_episode=1,
            tvdb_season=1,
            status="SEARCHABLE",
            anidb_episode=1,
            tvdb_episode_id=4201,
            absolute_episode=1,
        )
        self.catalog = EpisodeCatalog(self.titles, {1: self.target_ep1})

    def run_report(self, *argv):
        """Run adm report main and capture printed lines and return code."""
        lines = []
        old_print = __builtins__["print"]
        try:
            status = report_main(list(argv))
            return status, ""
        finally:
            pass

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

    def build_115_row_fixture(self):
        """Build the synthetic 115-row fixture matching the motivating reproduction."""
        rows_q1 = []
        # 1) 10 raw Dual-Audio tagged E04/E05 rows (unmatched episode):
        for i in range(1, 11):
            ep = 4 if i <= 5 else 5
            rows_q1.append(make_raw_prowlarr_row(
                f"[Judas] FX Senshi Kurumi-chan - S01E{ep:02d} [1080p][HEVC x265 10bit][Dual-Audio] (ep {i})",
                seeders=20 + i, size=400_000_000, info_hash=f"aa{i:02d}" + "0" * 36
            ))
        # 2) 80 other unmatched rows (other anime, other episodes, or unparseable):
        for i in range(11, 91):
            rows_q1.append(make_raw_prowlarr_row(
                f"[SubsPlease] FX Senshi Kurumi-chan - 04 (1080p) [ABCD{i:04d}].mkv",
                seeders=15, size=450_000_000, info_hash=f"bb{i:02d}" + "0" * 36
            ))
        # 3) 25 matching EP1 rows:
        # 8 missing info hash:
        for i in range(1, 9):
            rows_q1.append(make_raw_prowlarr_row(
                f"[SomeGroup] FX Senshi Kurumi-chan - 01 (1080p) [NoHash_{i}].mkv",
                seeders=10, size=500_000_000, info_hash=None
            ))
        # 4 resolution rejected (e.g. 2160p or 0):
        for i in range(1, 5):
            rows_q1.append(make_raw_prowlarr_row(
                f"[4KGroup] FX Senshi Kurumi-chan - 01 (2160p) [4k_{i}].mkv",
                seeders=10, size=500_000_000, info_hash=f"cc{i:02d}" + "0" * 36
            ))
        # 2 oversized (> 1500 MB):
        for i in range(1, 3):
            rows_q1.append(make_raw_prowlarr_row(
                f"[BigGroup] FX Senshi Kurumi-chan - 01 (1080p) [Big_{i}].mkv",
                seeders=10, size=2_000_000_000, info_hash=f"dd{i:02d}" + "0" * 36
            ))
        # 1 seeders below minimum (< 5 seeders):
        rows_q1.append(make_raw_prowlarr_row(
            "[DeadGroup] FX Senshi Kurumi-chan - 01 (1080p) [Dead].mkv",
            seeders=2, size=500_000_000, info_hash="ee01" + "0" * 36
        ))
        # 10 missing Dual-Audio title hint (passes hash, 1080p, size, seeders, but Japanese-only):
        for i in range(1, 11):
            rows_q1.append(make_raw_prowlarr_row(
                f"[SubsPlease] FX Senshi Kurumi-chan - 01 (1080p) [Sub_{i}].mkv",
                seeders=15, size=500_000_000, info_hash=f"ff{i:02d}" + "0" * 36
            ))

        self.assertEqual(len(rows_q1), 115)
        return rows_q1

    def test_synthetic_115_row_two_query_report(self):
        """1. Synthetic 115-row fixture: verify --short --include-matching prowlarr prints all 115 rows."""
        self.setUpFixture()
        rows_q1 = self.build_115_row_fixture()
        rows_q2 = []

        query_results = {
            "FX Senshi Kurumi-chan 01": rows_q1,
            "FX Fighter Kurumi-chan 01": rows_q2,
        }

        policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, max_episode_mb=1500, prefer_audio_tag=True)
        options = Options("http://127.0.0.1:9696", 60)
        client = Client(options, "apikey123", catalog=lambda a, t: self.catalog, policy=policy)
        client._transport = lambda req, timeout: None
        client._get = lambda q: query_results.get(q, [])
        client.set_context(plan_id=1)

        ranked = client._search(self.catalog, 1, self.target_ep1)
        self.assertEqual(len(ranked), 25)  # 25 matched EP1
        self.assertEqual(len(client.snapshots), 1)
        snapshot = client.snapshots[0]

        counts = snapshot["counts"]
        self.assertEqual(counts["raw"], 115)
        self.assertEqual(counts["unmatched"], 90)
        self.assertEqual(counts["matched"], 25)
        self.assertEqual(counts["passes_policy"], 0)
        self.assertEqual(counts["new_eligible"], 0)
        self.assertEqual(counts["raw_dual_audio"], 10)
        self.assertEqual(counts["matched_dual_audio"], 0)

        disps = counts["dispositions"]
        self.assertEqual(disps["UNMATCHED_TITLE_OR_EPISODE"], 90)
        self.assertEqual(disps["MISSING_DUAL_AUDIO_HINT"], 10)
        self.assertEqual(disps["MISSING_INFO_HASH"], 8)
        self.assertEqual(disps["RESOLUTION_OUT_OF_POLICY"], 4)
        self.assertEqual(disps["EPISODE_SIZE_TOO_LARGE"], 2)
        self.assertEqual(disps["INSUFFICIENT_SEEDERS"], 1)
        self.assertEqual(disps["PASSES_POLICY"], 0)
        self.assertEqual(sum(disps.values()), 115)

        # Save snapshot into test database for job 4282
        self.source(7)
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("INSERT OR REPLACE INTO trial_plans VALUES(1, 123, 456, 1, 'official', 1, 10, 6, 2, 1, 0)")
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={"plan_id": 1, "start": 1},
                                     component="worker", available_at_ms=0)
                # Seed 9 historical candidates
                for rank in range(1, 10):
                    db.execute(
                        "INSERT INTO sonarr_episode_candidates VALUES(1,1,?,?,'FAILED','MEDIA_AUDIO_REQUIRED')",
                        (rank, f"00{rank:02d}" + "0" * 36)
                    )
                save_snapshots(db, job_id, attempt=8, snapshots=[snapshot])
        finally:
            db.close()

        # Run report in short mode
        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn("=== Prowlarr matching · Job #", output)
        self.assertIn("115 raw · 90 unmatched · 25 EP1 matched · 0 passes policy (0 newly actionable)", output)
        self.assertIn("10 raw Dual-Audio hints; 0 matching EP1 with a Dual-Audio hint", output)
        self.assertIn("Prior database attempts: 9", output)
        self.assertIn("All releases (115 of 115):", output)

        # Verify every row 001..115 is present without truncation
        for i in range(1, 116):
            self.assertIn(f"  {i:03d} ", output, f"Row {i:03d} should be in table")
            self.assertIn(f"#{i:03d} ", output, f"Row #{i:03d} should be in detailed reasons")

        self.assertNotIn("... 113 more lines", output)
        self.assertNotIn("...", output.split("Detailed reasons:")[0].split("All releases")[1])

    def test_ten_raw_dual_audio_e04_e05_are_unmatched(self):
        """2. Ten raw Dual-Audio tagged E04/E05 rows must be classified as episode mismatches."""
        self.setUpFixture()
        rows_q1 = self.build_115_row_fixture()
        policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, max_episode_mb=1500, prefer_audio_tag=True)
        options = Options("http://127.0.0.1:9696", 60)
        client = Client(options, "apikey123", catalog=lambda a, t: self.catalog, policy=policy)
        client._transport = lambda req, timeout: None
        client._get = lambda q: rows_q1 if "01" in q else []

        client._search(self.catalog, 1, self.target_ep1)
        snapshot = client.snapshots[0]
        results = snapshot["results"]

        # First 10 rows are the E04/E05 Dual-Audio tagged releases
        for i in range(10):
            row = results[i]
            self.assertTrue(row["has_dual_audio_hint"])
            self.assertFalse(row["matches_episode"])
            self.assertEqual(row["primary_disposition"], "UNMATCHED_TITLE_OR_EPISODE")
            self.assertIn("parsed episode S01E0", row["reason"])
            self.assertIn("requested S01E01", row["reason"])

    def test_twenty_five_matching_failure_distribution_and_multi_predicate(self):
        """3. 25 matching E01 rows with first-failure distribution and multiple failed predicates."""
        self.setUpFixture()
        rows_q1 = self.build_115_row_fixture()

        policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, max_episode_mb=1500, prefer_audio_tag=True)
        options = Options("http://127.0.0.1:9696", 60)
        client = Client(options, "apikey123", catalog=lambda a, t: self.catalog, policy=policy)
        client._transport = lambda req, timeout: None
        client._get = lambda q: rows_q1 if "01" in q else []

        client._search(self.catalog, 1, self.target_ep1)
        snapshot = client.snapshots[0]

        disps = snapshot["counts"]["dispositions"]
        self.assertEqual(disps["MISSING_DUAL_AUDIO_HINT"], 10)
        self.assertEqual(disps["MISSING_INFO_HASH"], 8)
        self.assertEqual(disps["RESOLUTION_OUT_OF_POLICY"], 4)
        self.assertEqual(disps["EPISODE_SIZE_TOO_LARGE"], 2)
        self.assertEqual(disps["INSUFFICIENT_SEEDERS"], 1)

        # Check multi-predicate row (index 104, row 105): insufficient seeders AND missing dual audio
        row105 = snapshot["results"][104]
        self.assertEqual(row105["primary_disposition"], "INSUFFICIENT_SEEDERS")
        self.assertIn("INSUFFICIENT_SEEDERS", row105["failed_checks"])
        self.assertIn("MISSING_DUAL_AUDIO_HINT", row105["failed_checks"])
        self.assertIn("seeders 2 below minimum 5", row105["reason"])
        self.assertIn("missing Dual-Audio title hint", row105["reason"])

    def test_duplicate_hashes_across_queries(self):
        """4. Multiple search aliases with duplicate hashes: retain every raw row, mark duplicates."""
        self.setUpFixture()
        shared_hash = "1111222233334444555566667777888899990000"
        row1 = make_raw_prowlarr_row("[Group] FX Senshi Kurumi-chan - 01 [1080p]",
                                     seeders=10, size=500_000_000, info_hash=shared_hash)
        row2 = make_raw_prowlarr_row("[Group] FX Fighter Kurumi-chan - 01 [1080p]",
                                     seeders=10, size=500_000_000, info_hash=shared_hash)

        policy = DiscoveryPolicy(max_resolution=1080, min_seeders=5, prefer_audio_tag=False)
        options = Options("http://127.0.0.1:9696", 60)
        client = Client(options, "apikey123", catalog=lambda a, t: self.catalog, policy=policy)
        client._transport = lambda req, timeout: None
        client._get = lambda q: [row1] if q == "FX Senshi Kurumi-chan 01" else ([row2] if q == "FX Fighter Kurumi-chan 01" else [])

        ranked = client._search(self.catalog, 1, self.target_ep1)
        self.assertEqual(len(ranked), 1)  # Deduplicated in ranked return

        snapshot = client.snapshots[0]
        results = snapshot["results"]
        self.assertEqual(len(results), 2)  # BOTH raw rows retained!

        self.assertFalse(results[0]["is_duplicate"])
        self.assertEqual(results[0]["primary_disposition"], "PASSES_POLICY")

        self.assertTrue(results[1]["is_duplicate"])
        self.assertEqual(results[1]["duplicate_of"], 1)
        self.assertEqual(results[1]["primary_disposition"], "DUPLICATE_RESULT")
        self.assertIn("duplicate of #001", results[1]["reason"])

    async def test_on_hold_waiting_for_dub_retains_snapshot(self):
        """5. Search that goes to ON_HOLD / WAITING_FOR_DUB retains its snapshot."""
        self.setUpFixture()
        self.store.update({"acquisition": {"audio_check": "japanese_english"}}, expected_revision=1)
        self.source(7)

        # Run EVALUATE_TRIAL to create trial_plans and first page job
        reg_eval = Registry((trial_registration(discovery_kind=DISCOVER_KIND),))
        w_eval = Worker(self.store, self.startup, reg_eval, policy=Policy(lease_ms=30000))
        await w_eval.attempt()
        await w_eval.close()

        db = connect(self.database)
        try:
            with transaction(db):
                event_id = db.execute("SELECT event_id FROM jobs WHERE kind=?", (DISCOVER_KIND,)).fetchone()[0]
                db.execute("UPDATE jobs SET state='DONE' WHERE kind=?", (DISCOVER_KIND,))
                db.execute("INSERT INTO sonarr_episode_candidates VALUES(1, 2, 1, '1111111111111111111111111111111111111111', 'FAILED', 'MEDIA_AUDIO_REQUIRED')")
                db.execute("INSERT INTO sonarr_episode_selections VALUES(1, 2, 1, 2, 2002, '1111111111111111111111111111111111111111', 1)")
                job_id = enqueue_job(db, kind=DISCOVER_KIND, component="worker", event_id=event_id,
                                     payload={"plan_id": 1, "start": 1, "exhausted_episode": 2, "delete_cache": True},
                                     available_at_ms=0)
        finally:
            db.close()

        # Mock Prowlarr client with 1 Japanese-only matching release
        raw_row = make_raw_prowlarr_row("[Sub] FX Senshi Kurumi-chan - 02 (1080p).mkv",
                                        seeders=10, size=500_000_000,
                                        info_hash="2222222222222222222222222222222222222222")
        catalog_ep2 = EpisodeCatalog(("FX Senshi Kurumi-chan",), {
            2: EpisodeTarget(tvdb_episode=2, status="SEARCHABLE", anidb_episode=2, tvdb_episode_id=2002, absolute_episode=2, tvdb_season=1)
        })

        def fake_client_factory(conf, sec):
            policy = DiscoveryPolicy(prefer_audio_tag=True)
            c = Client(Options("http://127.0.0.1:9696", 60), "key",
                       catalog=lambda a, t: catalog_ep2, policy=policy)
            c._transport = lambda req, timeout: None
            c._get = lambda q: [raw_row] if q == "FX Senshi Kurumi-chan 02" else []
            return c

        registry = Registry((
            discovery_registration(None, client_factory=fake_client_factory,
                                   select_candidates=True, acquire_candidates=True),
        ))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000))
        try:
            await worker.attempt()
        finally:
            await worker.close()

        # Job must be ON_HOLD / WAITING_FOR_DUB
        job_state = self.rows("SELECT state, last_reason_code FROM jobs WHERE job_id=?", (job_id,))[0]
        self.assertEqual(job_state, ("ON_HOLD", "WAITING_FOR_DUB"))

        # Snapshot must exist in database!
        db = connect(self.database)
        try:
            snapshot = load_latest_snapshot(db, job_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["counts"]["raw"], 1)
            self.assertEqual(snapshot["counts"]["dispositions"]["MISSING_DUAL_AUDIO_HINT"], 1)
        finally:
            db.close()

        # adm report must be able to read it
        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn("=== Prowlarr matching · Job #", output)
        self.assertIn("1 raw · 0 unmatched · 1 EP2 matched · 0 passes policy (0 newly actionable)", output)

    async def test_provenance_cached_magnet_retry_has_no_search_run(self):
        """6. Cached-magnet retry does not search provider, so no snapshot is recorded."""
        self.setUpFixture()
        self.source(7)
        self.store.update({"acquisition": {"audio_check": "japanese_english"}}, expected_revision=1)

        # Run EVALUATE_TRIAL to create trial_plans and first page job
        reg_eval = Registry((trial_registration(discovery_kind=DISCOVER_KIND),))
        w_eval = Worker(self.store, self.startup, reg_eval, policy=Policy(lease_ms=30000))
        await w_eval.attempt()
        await w_eval.close()

        db = connect(self.database)
        try:
            with transaction(db):
                event_id = db.execute("SELECT event_id FROM jobs WHERE kind=?", (DISCOVER_KIND,)).fetchone()[0]
                db.execute("UPDATE jobs SET state='DONE' WHERE kind=?", (DISCOVER_KIND,))
                # Seed candidate failed for metadata timeout
                db.execute("INSERT INTO sonarr_episode_candidates VALUES(1, 2, 1, '1111111111111111111111111111111111111111', 'FAILED', 'TORRENT_METADATA_TIMEOUT')")
                db.execute("INSERT INTO sonarr_episode_selections VALUES(1, 2, 1, 2, 2002, '1111111111111111111111111111111111111111', 1)")
                # Seed cached torrent intent
                db.execute("""INSERT INTO sonarr_magnet_intents (
                    job_id, plan_id, anidb_episode_number, info_hash, marker, api_major, base_url, save_path, category, timeout_seconds, state, created_at_ms, updated_at_ms
                ) VALUES (1, 1, 2, '1111111111111111111111111111111111111111', '0123456789abcdef0123456789abcdef', 4, 'http://qb', '/downloads', 'test', 15, 'RELEASED', 0, 0)""")

                job_id = enqueue_job(db, kind=DISCOVER_KIND, component="worker", event_id=event_id,
                                     payload={"plan_id": 1, "start": 1, "exhausted_episode": 2, "delete_cache": False},
                                     available_at_ms=0)
        finally:
            db.close()

        searched = []
        def fake_client_factory(conf, sec):
            c = Client(Options("http://127.0.0.1:9696", 60), "key",
                       catalog=lambda a, t: self.catalog, policy=DiscoveryPolicy())
            c.search = lambda *args: searched.append(args)
            return c

        registry = Registry((
            discovery_registration(None, client_factory=fake_client_factory,
                                   select_candidates=True, acquire_candidates=True),
        ))
        worker = Worker(self.store, self.startup, registry, policy=Policy(lease_ms=30000))
        try:
            await worker.attempt()
        finally:
            await worker.close()

        # No provider search should have run
        self.assertEqual(len(searched), 0)

        # No search snapshot should exist for this job
        db = connect(self.database)
        try:
            snapshot = load_latest_snapshot(db, job_id)
            self.assertIsNone(snapshot)
        finally:
            db.close()

        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn("Not recorded for this attempt (older search or capture unavailable)", output)

    def test_multiple_attempts_default_to_latest(self):
        """7. Multiple attempts: latest selected by default, previous distinguishable."""
        db = connect(self.database)
        try:
            with transaction(db):
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={"plan_id": 1, "start": 1},
                                     component="worker", available_at_ms=0)
                snap1 = {
                    "search_mode": "initial", "status": "COMPLETE", "counts": {"raw": 5, "dispositions": {"NEW_ELIGIBLE": 5}},
                    "results": [make_raw_prowlarr_row(f"Title {i}") for i in range(5)], "policy": {"max_resolution": 720}
                }
                snap2 = {
                    "search_mode": "retry", "status": "COMPLETE", "counts": {"raw": 10, "dispositions": {"NEW_ELIGIBLE": 10}},
                    "results": [make_raw_prowlarr_row(f"Title {i}") for i in range(10)], "policy": {"max_resolution": 1080}
                }
                save_snapshots(db, job_id, attempt=1, snapshots=[snap1], now_ms=1000)
                save_snapshots(db, job_id, attempt=2, snapshots=[snap2], now_ms=2000)
        finally:
            db.close()

        # Default gives latest (attempt 2)
        code, out_latest = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn("attempt 2", out_latest)
        self.assertIn("10 raw", out_latest)
        self.assertIn("max 1080p", out_latest)

        # --attempt 1 gives attempt 1
        code, out_att1 = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "--attempt", "1",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn("attempt 1", out_att1)
        self.assertIn("5 raw", out_att1)
        self.assertIn("max 720p", out_att1)

    def test_missing_snapshot_and_partial_queries(self):
        """8. Missing snapshot reports Not recorded; partial query failure reports Partial."""
        db = connect(self.database)
        try:
            with transaction(db):
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={"plan_id": 1, "start": 1},
                                     component="worker", available_at_ms=0)
        finally:
            db.close()

        # Missing snapshot
        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn("Not recorded for this attempt (older search or capture unavailable). No Prowlarr request was made.", output)

        # Partial query failure: Query 1 succeeds, Query 2 times out
        self.setUpFixture()
        policy = DiscoveryPolicy()
        options = Options("http://127.0.0.1:9696", 60)
        client = Client(options, "apikey123", catalog=lambda a, t: self.catalog, policy=policy)
        client._transport = lambda req, timeout: None

        def mock_get(q):
            if q == "FX Senshi Kurumi-chan 01":
                return [make_raw_prowlarr_row("[Sub] FX Senshi Kurumi-chan - 01 (1080p)")]
            raise SearchTimeout()

        client._get = mock_get
        with self.assertRaises(SearchTimeout):
            client._search(self.catalog, 1, self.target_ep1)

        self.assertEqual(len(client.snapshots), 1)
        snapshot = client.snapshots[0]
        self.assertEqual(snapshot["status"], "PARTIAL")
        self.assertEqual(snapshot["error"], "PROWLARR_SEARCH_TIMEOUT")
        self.assertEqual(len(snapshot["queries"]), 2)
        self.assertEqual(snapshot["queries"][0]["status"], "SUCCESS")
        self.assertEqual(snapshot["queries"][1]["status"], "PROWLARR_SEARCH_TIMEOUT")

        # Save partial snapshot and test report
        db = connect(self.database)
        try:
            with transaction(db):
                save_snapshots(db, job_id, attempt=1, snapshots=[snapshot])
        finally:
            db.close()

        code, out_partial = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn("status: PARTIAL", out_partial)
        self.assertIn("PROWLARR_SEARCH_TIMEOUT", out_partial)

    def test_json_mode_and_compress_dest(self):
        """9. JSON mode output, and --compress / --dest safe handling."""
        self.setUpFixture()
        rows_q1 = self.build_115_row_fixture()
        policy = DiscoveryPolicy(prefer_audio_tag=True)
        client = Client(Options("http://127.0.0.1:9696", 60), "apikey", catalog=lambda a, t: self.catalog, policy=policy)
        client._get = lambda q: rows_q1 if q == "FX Senshi Kurumi-chan 01" else []
        client._search(self.catalog, 1, self.target_ep1)
        snapshot = client.snapshots[0]

        db = connect(self.database)
        try:
            with transaction(db):
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={"plan_id": 1, "start": 1},
                                     component="worker", available_at_ms=0)
                save_snapshots(db, job_id, attempt=1, snapshots=[snapshot])
        finally:
            db.close()

        # Short JSON
        code, out_json = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--include-matching", "prowlarr", "--json",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        data = json.loads(out_json)
        self.assertEqual(data["job_id"], job_id)
        self.assertTrue(data["matching"]["prowlarr"]["recorded"])
        self.assertEqual(len(data["matching"]["prowlarr"]["releases"]), 115)
        # Verify no secret or private URL
        self.assertNotIn("apikey", out_json)
        self.assertNotIn("http://127.0.0.1", out_json)

        # Full JSON
        code, out_full_json = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id), "--include-matching", "prowlarr", "--json"
        )
        self.assertEqual(code, 0)
        full_data = json.loads(out_full_json)
        self.assertIn("state", full_data)
        self.assertIn("matching", full_data)
        self.assertIn("prowlarr", full_data["matching"])

        # Test --dest
        dest_file = self.root / "output-report.txt"
        code, out_dest = self.run_report_capture(
            "--config-dir", str(self.root),
            "--dest", str(dest_file),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertTrue(dest_file.exists())
        self.assertEqual(oct(os.stat(dest_file).st_mode & 0o777), "0o600")
        dest_content = dest_file.read_text(encoding="utf-8")
        self.assertIn("All releases (115 of 115):", dest_content)

        # Test --compress
        code, out_comp = self.run_report_capture(
            "--config-dir", str(self.root),
            "--compress", "--dest", str(self.root),
            "--short", "--include-matching", "prowlarr",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        tar_file = self.root / f"job-report-{job_id}.tar.gz"
        self.assertTrue(tar_file.exists())
        self.assertEqual(oct(os.stat(tar_file).st_mode & 0o777), "0o600")
        with tarfile.open(tar_file, "r:gz") as archive:
            member = archive.getmember(f"job-report-{job_id}.txt")
            extracted = archive.extractfile(member).read().decode("utf-8")
            self.assertIn("All releases (115 of 115):", extracted)

    def test_security_sanitization_and_bounded_storage(self):
        """10. Strips ANSI/control chars, sanitizes dangerous text, bounds rows and safe prune."""
        dirty = "\x1b[31;1mDangerous\x1b[0m Title \x00\x07With \x1b]0;Evil\x07Text"
        clean = sanitize_text(dirty)
        self.assertEqual(clean, "Dangerous Title With Text")

        # Bound rows over 1000
        lots_of_rows = [make_raw_prowlarr_row(f"Title {i}") for i in range(1100)]
        snap = {
            "search_mode": "initial", "status": "COMPLETE", "results": lots_of_rows
        }
        db = connect(self.database)
        try:
            with transaction(db):
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={}, component="worker", available_at_ms=0)
                save_snapshots(db, job_id, attempt=1, snapshots=[snap], now_ms=1_000_000)
                saved = load_latest_snapshot(db, job_id)
                self.assertTrue(saved["truncated"])
                self.assertEqual(len(saved["results"]), 1000)
                self.assertEqual(saved["total_raw_count"], 1100)

                # Test pruning
                # 1) Job in ON_HOLD must NOT be pruned
                db.execute("UPDATE jobs SET state='ON_HOLD' WHERE job_id=?", (job_id,))
                prune_snapshots(db, keep_per_job=1, terminal_age_days=0, now_ms=10_000_000_000)
                self.assertIsNotNone(load_latest_snapshot(db, job_id))

                # 2) Terminal job older than terminal_age_days IS pruned
                db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?", (job_id,))
                prune_snapshots(db, keep_per_job=1, terminal_age_days=0, now_ms=10_000_000_000)
                self.assertIsNone(load_latest_snapshot(db, job_id))
        finally:
            db.close()

    def test_regression_existing_report_unchanged(self):
        """11. adm report job output is unchanged unless --include-matching is specified."""
        db = connect(self.database)
        try:
            with transaction(db):
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={"plan_id": 1, "start": 1},
                                     component="worker", available_at_ms=0)
        finally:
            db.close()

        # Regular job report without matching flag
        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        self.assertIn(f"=== Job #{job_id}", output)
        self.assertIn("=== Current state", output)
        self.assertNotIn("Prowlarr matching", output)

    def test_no_network_calls_during_report(self):
        """12. Zero network / HTTP calls made during report generation."""
        import socket
        real_connect = socket.socket.connect

        def forbidden_connect(*args, **kwargs):
            raise AssertionError("Network connection attempted during read-only report!")

        socket.socket.connect = forbidden_connect
        try:
            db = connect(self.database)
            try:
                with transaction(db):
                    job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={}, component="worker", available_at_ms=0)
            finally:
                db.close()

            code, output = self.run_report_capture(
                "--config-dir", str(self.root),
                "--short", "--include-matching", "prowlarr",
                "job", "--id", str(job_id)
            )
            self.assertEqual(code, 0)
        finally:
            socket.socket.connect = real_connect

    def test_cli_short_alias_and_validation(self):
        """13. Short form alias 'adm report --short --id ...' works; validation errors on misuse."""
        db = connect(self.database)
        try:
            with transaction(db):
                job_id = enqueue_job(db, kind=DISCOVER_KIND, payload={}, component="worker", available_at_ms=0)
        finally:
            db.close()

        # Short form alias without "job"
        code, output = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "--id", str(job_id), "--include-matching", "prowlarr"
        )
        self.assertEqual(code, 0)
        self.assertIn(f"=== Prowlarr matching · Job #{job_id}", output)

        # --short without --include-matching must error
        code, err = self.run_report_capture(
            "--config-dir", str(self.root),
            "--short", "job", "--id", str(job_id)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("--short requires --include-matching <section>", err)

        # --include-matching invalid must error
        code, err = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id), "--include-matching", "invalid_provider"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("invalid choice: 'invalid_provider'", err)


if __name__ == "__main__":
    unittest.main()
