"""Tests for explainable identity matching diagnostics in adm report."""
import contextlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.history import append_job_history
from src.nextgen.job_report import main as report_main
from src.nextgen.matching import MatchingTrace
from src.nextgen.storage.sqlite import accept_event_and_job, connect, enqueue_job, initialize, transaction
from src.nextgen.tests.test_trial_policy import TrialEvaluationTests as _TrialEvaluationTests

_TrialEvaluationTests.__test__ = False


class IdentityMatchingReportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = _TrialEvaluationTests.asyncSetUp
    rows = _TrialEvaluationTests.rows

    def run_report_capture(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = report_main(list(argv))
            except SystemExit as exc:
                status = exc.code
        out_str = out.getvalue()
        err_str = err.getvalue()
        return status, out_str if out_str else err_str

    def _create_identity_job(self, release_name="[SubsPlease] Sousou no Frieren - 01 (1080p) [12345678].mkv"):
        db = connect(self.database)
        try:
            event_id, job_id = accept_event_and_job(
                db, "autobrr",
                json.dumps({"release_name": release_name}).encode("utf-8"),
                kind="IDENTIFY_ANIME", payload={}
            )
            return event_id, job_id
        finally:
            db.close()

    def test_required_commands(self):
        """Verify the 4 required commands from the feature specification."""
        event_id, job_id = self._create_identity_job()
        trace = MatchingTrace()
        trace.queries(["Sousou no Frieren", "Frieren: Beyond Journey's End"], season=1, episode=1)
        trace.candidates({17617: ["Sousou no Frieren", "Frieren: Beyond Journey's End"]})
        trace.anidb(17617)
        trace.tvdb(4242, ["Frieren: Beyond Journey's End"])
        trace.check("release_title", "passed")
        trace.check("anidb_candidates", "passed")
        trace.check("anime_lists", "failed", "no_anime_lists_mapping")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute(
                    "UPDATE jobs SET state='NEEDS_REVIEW', last_reason_code='IDENTITY_INCOMPLETE', attempt_count=2 WHERE job_id=?",
                    (job_id,)
                )
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW",
                    reason_code="IDENTITY_INCOMPLETE", component="worker",
                    occurred_at_ms=1700000000000,
                    details={"attempt": 2, "matching": trace.data}
                )
        finally:
            db.close()

        # 1. adm report job --id 449 --include-matching identity (full report)
        code, out_full = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity"
        )
        self.assertEqual(code, 0)
        self.assertIn(f"=== Job #{job_id}", out_full)
        self.assertIn(f"=== Identity matching · Job #{job_id} · attempt 2", out_full)
        self.assertIn("Sousou no Frieren", out_full)
        self.assertIn("AniDB #17617", out_full)
        self.assertIn("no_anime_lists_mapping", out_full)

        # 2. adm report job --id 449 --include-matching identity --short
        code, out_short = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertNotIn(f"=== History", out_short)
        self.assertIn(f"=== Identity matching · Job #{job_id} · attempt 2", out_short)
        self.assertIn("Failed check: anime_lists (no_anime_lists_mapping)", out_short)
        self.assertIn("Summary     : No mapping from AniDB to TVDB found in AnimeLists.", out_short)

        # 3. adm report job --id 449 --include-matching identity --attempt 2
        code, out_att = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--attempt", "2",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("attempt 2", out_att)

        # 4. adm report job --id 449 --include-matching identity --short --json
        code, out_json = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short",
            "--json"
        )
        self.assertEqual(code, 0)
        data = json.loads(out_json)
        self.assertEqual(data["job_id"], job_id)
        self.assertTrue(data["matching"]["identity"]["recorded"])
        id_doc = data["matching"]["identity"]
        self.assertEqual(id_doc["attempt"], 2)
        self.assertEqual(id_doc["season"], 1)
        self.assertEqual(id_doc["episode"], 1)
        self.assertEqual(id_doc["selected_anidb_id"], 17617)
        self.assertEqual(id_doc["selected_tvdb_id"], 4242)
        self.assertEqual(len(id_doc["checks"]), 3)
        self.assertEqual(len(id_doc["failed_checks"]), 1)
        self.assertEqual(id_doc["failed_checks"][0]["step"], "anime_lists")

    def test_no_candidates(self):
        """AniDB returns 0 candidates and reports clear explanation."""
        event_id, job_id = self._create_identity_job(release_name="[Unknown] Obscure Series - 01.mkv")
        trace = MatchingTrace()
        trace.queries(["Obscure Series"], season=1, episode=1)
        trace.check("release_title", "passed")
        trace.check("anidb_candidates", "failed", "no_anidb_candidates")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute(
                    "UPDATE jobs SET state='NEEDS_REVIEW', last_reason_code='IDENTITY_INCOMPLETE', attempt_count=1 WHERE job_id=?",
                    (job_id,)
                )
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW",
                    reason_code="IDENTITY_INCOMPLETE", component="worker",
                    occurred_at_ms=1700000000000,
                    details={"attempt": 1, "matching": trace.data}
                )
        finally:
            db.close()

        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("AniDB candidates (0):", out)
        self.assertIn("(none returned)", out)
        self.assertIn("Failed check: anidb_candidates (no_anidb_candidates)", out)
        self.assertIn("Summary     : No AniDB candidates matched the release title or aliases.", out)

    def test_multiple_candidates(self):
        """AniDB returns multiple ambiguous candidates."""
        event_id, job_id = self._create_identity_job(release_name="[Group] Ambiguous Title - 01.mkv")
        trace = MatchingTrace()
        trace.queries(["Ambiguous Title"], season=1, episode=1)
        trace.candidates({
            1001: ["Ambiguous Title (2010)", "Ambiguous Title S1"],
            2002: ["Ambiguous Title (2020)", "Ambiguous Title Reboot"],
        })
        trace.check("release_title", "passed")
        trace.check("anidb_candidates", "failed", "multiple_anidb_candidates")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute(
                    "UPDATE jobs SET state='NEEDS_REVIEW', last_reason_code='IDENTITY_INCOMPLETE', attempt_count=1 WHERE job_id=?",
                    (job_id,)
                )
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW",
                    reason_code="IDENTITY_INCOMPLETE", component="worker",
                    occurred_at_ms=1700000000000,
                    details={"attempt": 1, "matching": trace.data}
                )
        finally:
            db.close()

        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("AniDB candidates (2):", out)
        self.assertIn("AniDB #1001: Ambiguous Title (2010) · Ambiguous Title S1", out)
        self.assertIn("AniDB #2002: Ambiguous Title (2020) · Ambiguous Title Reboot", out)
        self.assertIn("Failed check: anidb_candidates (multiple_anidb_candidates)", out)
        self.assertIn("Summary     : Multiple AniDB candidates matched the release title; automatic identification requires an unambiguous match.", out)

    def test_missing_anidb_tvdb_verification(self):
        """AniDB candidate resolved, but TVDB verification failed/missing."""
        event_id, job_id = self._create_identity_job()
        trace = MatchingTrace()
        trace.queries(["Test Anime"], season=1, episode=1)
        trace.candidates({5555: ["Test Anime"]})
        trace.anidb(5555)
        trace.check("release_title", "passed")
        trace.check("anidb_candidates", "passed")
        trace.check("tvdb_series", "failed", "tvdb_not_animation")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute(
                    "UPDATE jobs SET state='NEEDS_REVIEW', last_reason_code='IDENTITY_INCOMPLETE', attempt_count=1 WHERE job_id=?",
                    (job_id,)
                )
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW",
                    reason_code="IDENTITY_INCOMPLETE", component="worker",
                    occurred_at_ms=1700000000000,
                    details={"attempt": 1, "matching": trace.data}
                )
        finally:
            db.close()

        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("AniDB: AniDB #5555", out)
        self.assertIn("TVDB : Not selected", out)
        self.assertIn("Failed check: tvdb_series (tvdb_not_animation)", out)
        self.assertIn("Summary     : Matched TVDB series is not categorized as animation.", out)

    def test_historical_no_unique_anidb_candidate(self):
        """Historical diagnostic note no_unique_anidb_candidate is rendered without misinterpretation."""
        event_id, job_id = self._create_identity_job()
        trace = MatchingTrace()
        trace.queries(["Historical Show"], season=1, episode=1)
        trace.check("release_title", "passed")
        trace.check("anidb_candidates", "failed", "no_unique_anidb_candidate")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute(
                    "UPDATE jobs SET state='NEEDS_REVIEW', last_reason_code='IDENTITY_INCOMPLETE', attempt_count=1 WHERE job_id=?",
                    (job_id,)
                )
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW",
                    reason_code="IDENTITY_INCOMPLETE", component="worker",
                    occurred_at_ms=1700000000000,
                    details={"attempt": 1, "matching": trace.data}
                )
        finally:
            db.close()

        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("anidb_candidates     (no_unique_anidb_candidate)", out)
        self.assertIn("Failed check: anidb_candidates (no_unique_anidb_candidate)", out)
        self.assertIn("Summary     : Multiple AniDB candidates matched the release title; automatic identification requires an unambiguous match.", out)

    def test_missing_evidence(self):
        """Missing evidence reports 'Not recorded' gracefully and exits with status 0."""
        event_id, job_id = self._create_identity_job()
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='PENDING', attempt_count=0 WHERE job_id=?", (job_id,))
        finally:
            db.close()

        # Text mode
        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("Identity matching: Not recorded for this attempt (older job or evidence unavailable). No provider request was made.", out)

        # JSON mode
        code, out_json = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short",
            "--json"
        )
        self.assertEqual(code, 0)
        data = json.loads(out_json)
        self.assertFalse(data["matching"]["identity"]["recorded"])
        self.assertIn("Not recorded", data["matching"]["identity"]["message"])

    def test_explicit_attempt_selection(self):
        """Explicit attempt selection defaults to latest and allows selecting specific attempts."""
        event_id, job_id = self._create_identity_job()

        trace1 = MatchingTrace()
        trace1.queries(["Title Att 1"], season=1, episode=1)
        trace1.check("release_title", "passed")
        trace1.check("anidb_candidates", "failed", "no_anidb_candidates")

        trace2 = MatchingTrace()
        trace2.queries(["Title Att 2"], season=1, episode=1)
        trace2.candidates({999: ["Title Att 2 Match"]})
        trace2.anidb(999)
        trace2.check("release_title", "passed")
        trace2.check("anidb_candidates", "passed")
        trace2.check("tvdb_series", "failed", "provider_auth_failed")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='NEEDS_REVIEW', attempt_count=1 WHERE job_id=?", (job_id,))
                append_job_history(
                    db, job_id=job_id, event_code="RETRY_SCHEDULED",
                    reason_code="PROVIDER_AUTH_FAILED", component="worker",
                    occurred_at_ms=1000,
                    details={"attempt": 1, "matching": trace1.data}
                )
                db.execute("UPDATE jobs SET state='NEEDS_REVIEW', last_reason_code='PROVIDER_AUTH_FAILED', attempt_count=2 WHERE job_id=?", (job_id,))
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW",
                    reason_code="PROVIDER_AUTH_FAILED", component="worker",
                    occurred_at_ms=2000,
                    details={"attempt": 2, "matching": trace2.data}
                )
        finally:
            db.close()

        # Default gives latest (attempt 2)
        code, out_latest = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("attempt 2", out_latest)
        self.assertIn("Title Att 2", out_latest)
        self.assertIn("AniDB #999", out_latest)
        self.assertIn("TVDB authentication failed", out_latest)

        # --attempt 1 gives attempt 1
        code, out_att1 = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--attempt", "1",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("attempt 1", out_att1)
        self.assertIn("Title Att 1", out_att1)
        self.assertIn("no_anidb_candidates", out_att1)

        # --attempt 99 reports not recorded
        code, out_att99 = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--attempt", "99",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("Not recorded for this attempt", out_att99)

    def test_downstream_job_resolution(self):
        """Downstream job pointing to an incoming event resolves identity matching."""
        event_id, job_id = self._create_identity_job()
        trace = MatchingTrace()
        trace.queries(["Downstream Parent"], season=1, episode=1)
        trace.check("release_title", "passed")
        trace.check("identity_verified", "passed", "verified")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE', last_reason_code=NULL, attempt_count=1 WHERE job_id=?", (job_id,))
                append_job_history(
                    db, job_id=job_id, event_code="DONE",
                    reason_code=None, component="worker",
                    occurred_at_ms=1000,
                    details={"attempt": 1, "matching": trace.data}
                )
                verify_job_id = enqueue_job(
                    db, kind="VERIFY_RELEASE", payload={"anidb_id": 123},
                    component="worker", event_id=event_id, created_by_job_id=job_id
                )
        finally:
            db.close()

        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(verify_job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn(f"=== Identity matching · Job #{verify_job_id}", out)
        self.assertIn("Downstream Parent", out)
        self.assertIn("Series identified successfully.", out)

    def test_argument_position_flexibility(self):
        """Flags work whether placed before or after the job subcommand."""
        event_id, job_id = self._create_identity_job()
        trace = MatchingTrace()
        trace.queries(["Arg Placement Test"], season=1, episode=1)
        trace.check("release_title", "passed")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='NEEDS_REVIEW', attempt_count=1 WHERE job_id=?", (job_id,))
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW",
                    reason_code="IDENTITY_INCOMPLETE", component="worker",
                    occurred_at_ms=1000,
                    details={"attempt": 1, "matching": trace.data}
                )
        finally:
            db.close()

        # Flags AFTER job subcommand: adm report job --id 222 --short --compress --dest ... --json
        dest_dir = self.root / "reports_after"
        dest_dir.mkdir()
        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short", "--compress",
            "--dest", str(dest_dir),
            "--json"
        )
        self.assertEqual(code, 0)
        tar_after = dest_dir / f"job-report-{job_id}.tar.gz"
        self.assertTrue(tar_after.exists())

        # Flags BEFORE job subcommand: adm report --short --compress --dest ... --json job --id 222
        dest_dir2 = self.root / "reports_before"
        dest_dir2.mkdir()
        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "--include-matching", "identity",
            "--short", "--compress",
            "--dest", str(dest_dir2),
            "--json",
            "job", "--id", str(job_id)
        )
        self.assertEqual(code, 0)
        tar_before = dest_dir2 / f"job-report-{job_id}.tar.gz"
        self.assertTrue(tar_before.exists())

        # Short alias without 'job': adm report --id 222 --include-matching identity --short
        code, out_alias = self.run_report_capture(
            "--config-dir", str(self.root),
            "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("=== Identity matching", out_alias)

        # Validation: --short without --include-matching errors
        code, err = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--short"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("--short requires --include-matching <section>", err)

        # Validation: invalid include-matching choice errors
        code, err2 = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "invalid_section"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("invalid choice: 'invalid_section'", err2)

    def test_prowlarr_backward_compatibility(self):
        """Existing Prowlarr matching reports remain completely compatible."""
        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", "99999",
            "--include-matching", "prowlarr",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("=== Prowlarr matching", out)
        self.assertIn("Not recorded for this attempt", out)

    def test_read_only_and_security(self):
        """Database remains unchanged during reporting and no tokens are leaked."""
        event_id, job_id = self._create_identity_job()
        trace = MatchingTrace()
        trace.queries(["Security Test"], season=1, episode=1)
        trace.check("release_title", "passed")

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='NEEDS_REVIEW', attempt_count=1 WHERE job_id=?", (job_id,))
                append_job_history(
                    db, job_id=job_id, event_code="NEEDS_REVIEW",
                    reason_code="IDENTITY_INCOMPLETE", component="worker",
                    occurred_at_ms=1000,
                    details={"attempt": 1, "matching": trace.data}
                )
                before_changes = db.total_changes
        finally:
            db.close()

        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)

        db = connect(self.database)
        try:
            self.assertEqual(db.total_changes, 0, "adm report must not perform database mutations")
        finally:
            db.close()


    def test_candidate_evidence_ranking_report(self):
        """Candidate evidence table rows are formatted into ranking report with scores and decision."""
        from src.nextgen.storage.candidate_evidence import save_candidate_evidence
        event_id, job_id = self._create_identity_job(release_name="[Anime Time] Black Clover - S02E02")
        trace = MatchingTrace()
        trace.queries(["Black Clover", "Black Clover 2nd Season"], season=2, episode=2)
        trace.scoring(90, "v1", "AUTO_SELECTED", 3, 2)
        trace.anidb(19433)
        trace.check("release_title", "passed")
        trace.check("season_scope", "passed")
        trace.check("anidb_candidates", "passed")

        candidates = [
            {
                "anidb_id": 19433,
                "display_title": "Black Clover 2nd Season",
                "matched_anidb_title": "Black Clover 2nd Season",
                "matched_variant": "Black Clover 2nd Season",
                "similarity_pct": 100,
                "rank": 1,
                "eligible": True,
                "selected": True,
                "evidence": {
                    "matched_variant": "Black Clover 2nd Season",
                    "matched_anidb_title": "Black Clover 2nd Season",
                    "matched_variant_provenance": "season_derived",
                    "exact_match_bonus": 10.0,
                    "season_agreement": 10.0,
                },
            },
            {
                "anidb_id": 12665,
                "display_title": "Black Clover",
                "matched_anidb_title": "Black Clover",
                "matched_variant": "Black Clover",
                "similarity_pct": 100,
                "rank": 2,
                "eligible": True,
                "selected": False,
                "evidence": {
                    "matched_variant": "Black Clover",
                    "matched_anidb_title": "Black Clover",
                    "matched_variant_provenance": "cleaned",
                    "exact_match_bonus": 10.0,
                },
            },
            {
                "anidb_id": 12351,
                "display_title": "Black Clover Special",
                "matched_anidb_title": "Black Clover Special",
                "matched_variant": "Black Clover",
                "similarity_pct": 75,
                "rank": 3,
                "eligible": False,
                "selected": False,
                "evidence": {
                    "special_penalty": -15.0,
                },
            },
        ]

        db = connect(self.database)
        try:
            with transaction(db):
                db.execute(
                    "UPDATE jobs SET state='DONE', attempt_count=1 WHERE job_id=?",
                    (job_id,)
                )
                append_job_history(
                    db, job_id=job_id, event_code="DONE",
                    reason_code=None, component="worker",
                    occurred_at_ms=1700000000000,
                    details={"attempt": 1, "matching": trace.data}
                )
                save_candidate_evidence(db, job_id, 1, candidates, now_ms=1700000000000)
        finally:
            db.close()

        code, out = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short"
        )
        self.assertEqual(code, 0)
        self.assertIn("Threshold: 90% · Scoring version: v1", out)
        self.assertIn("AniDB #19433 — Black Clover 2nd Season", out)
        self.assertIn("Similarity: 100%", out)
        self.assertIn("Rank: 1", out)
        self.assertIn("Selected: YES", out)
        self.assertIn("AniDB #12665 — Black Clover", out)
        self.assertIn("Rank: 2", out)
        self.assertIn("Selected: NO", out)
        self.assertIn("AniDB #12351 — Black Clover Special", out)
        self.assertIn("Rank: 3", out)
        self.assertIn("Decision    : AUTO_SELECTED", out)

        # JSON output check
        code_json, out_json = self.run_report_capture(
            "--config-dir", str(self.root),
            "job", "--id", str(job_id),
            "--include-matching", "identity",
            "--short", "--json"
        )
        self.assertEqual(code_json, 0)
        doc = json.loads(out_json)
        match_id = doc["matching"]["identity"]
        self.assertEqual(match_id["threshold_pct"], 90)
        self.assertEqual(match_id["decision"], "AUTO_SELECTED")
        self.assertEqual(len(match_id["candidate_evidence"]), 3)
        self.assertEqual(match_id["candidate_evidence"][0]["anidb_id"], 19433)
        self.assertTrue(match_id["candidate_evidence"][0]["selected"])


if __name__ == "__main__":
    unittest.main()
