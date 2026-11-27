"""Tests for fixes/cutover_series_job.py (CO-1 to CO-5, CO-7)."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from fixes import cutover_series_job as cutover
from src.anime.acquisition.media import signature
from src.anime.series_job.actions import keep_series_sync
from src.anime.series_job.context import SeriesContext
from src.anime.series_job.reject import run_reject
from src.anime.series_job.store import get_series
from src.anime.series_job.worker import run_series_attempt
from src.anime.storage import migrations
from src.anime.storage.migrations import _structure
from src.anime.storage.review_decisions import decision
from src.anime.storage.sqlite import initialize
from src.anime.tests.test_series_keep import FakeSonarr
from src.anime.tests.test_series_steps import Configuration, FakeQb


def _create_v78(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    for script in migrations.SCRIPTS[:78]:
        db.executescript((migrations.SQL_DIR / script).read_text())
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _seed_staged_single(db: sqlite3.Connection, staging_root: Path, *, aid: int = 101, tvdb: int = 501,
                        title: str = "Show One", infohash: str = "a" * 40, marker: str = "1" * 32,
                        rel_path: str = "Show One/Season 01/Show One - S01E01.mkv"):
    staged = staging_root / rel_path
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"v" * 100)
    sig = list(signature(staged.stat()))
    nfo_rel = rel_path.replace(".mkv", ".nfo")
    (staging_root / nfo_rel).write_text("<episodedetails/>")

    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("INSERT OR IGNORE INTO identified_anime VALUES(?, ?, ?)", (aid, tvdb, title))
    db.execute("INSERT OR IGNORE INTO identified_episodes VALUES(?, ?)", (aid * 100 + 1, aid))
    db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(1, 'test', ?, 1000, 'IDENTIFIED')",
               (json.dumps({"release_name": f"[G] {title} - 01", "torrent_infohash": infohash}).encode(),))
    db.execute("INSERT INTO identity_results(result_id, event_id, anidb_id, schema_version, series_json, validated_at_ms) "
               "VALUES(1, 1, ?, 2, ?, 1000)",
               (aid, json.dumps({"schema_version": 2, "anime": {"anidb_id": aid, "tvdb_series_id": tvdb, "title": title}})))
    db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms) "
               "VALUES(1, 1, 'STAGE_USER_EVALUATION_MEDIA', '{}', 'DONE', 0, 1000, 1000)")
    db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms) "
               "VALUES(2, 1, 'VERIFY_MEDIA', '{}', 'DONE', 0, 1000, 1000)")
    db.execute("INSERT INTO release_verifications(verification_id, job_id, identity_result_id, anidb_id, release_sha256, "
               "verification_scope, anidb_episode_number, anidb_episode_id, tvdb_series_id, tvdb_season, tvdb_episode, tvdb_episode_id, verified_at_ms) "
               "VALUES(1, 1, 1, ?, ?, 'DECLARED_SINGLE_EPISODE', 1, ?, ?, 1, 1, ?, 1000)",
               (aid, "b" * 64, aid * 100 + 1, tvdb, tvdb * 100 + 1))
    db.execute("INSERT INTO user_evaluation_plans(plan_id, anidb_id, tvdb_series_id, tvdb_season, anchor_verification_id, "
               "anchor_episode, backfill_count, following_count, configuration_revision, created_at_ms) "
               "VALUES(1, ?, ?, 1, 1, 1, 1, 1, 1, 1000)", (aid, tvdb))
    db.execute("INSERT INTO episode_reservations(reservation_id, owner_job_id, evaluation_job_id, verification_id, plan_id, "
               "anidb_id, anidb_episode_id, tvdb_series_id, tvdb_season, tvdb_episode, tvdb_episode_id, state, reserved_at_ms) "
               "VALUES(1, 1, 1, 1, 1, ?, ?, ?, 1, 1, ?, 'ACTIVE', 1000)",
               (aid, aid * 100 + 1, tvdb, tvdb * 100 + 1))
    ap_manifest = {"name": f"[G] {title} - 01", "selected": {"index": 0, "path": "ep01.mkv", "size": 100}}
    db.execute("INSERT INTO acquisition_plans(plan_id, job_id, reservation_id, info_hash, manifest_json, submission_marker, "
               "state, configuration_revision, prepared_at_ms) VALUES(1, 1, 1, ?, ?, ?, 'PREPARED', 1, 1000)",
               (infohash, json.dumps(ap_manifest), marker))
    db.execute("INSERT INTO torrent_submissions(plan_id, first_job_id, first_attempt, base_url, save_path, category, "
               "timeout_seconds, api_major, fingerprint, configuration_revision, state, reason_code, created_at_ms, updated_at_ms) "
               "VALUES(1, 1, 1, 'http://qb.test', '/d', 'anime-discovery', 5, 5, ?, 1, 'OWNED_STOPPED', 'TORRENT_STOPPED_CONFIRMED', 1000, 1000)",
               ("c" * 64,))
    db.execute("INSERT INTO torrent_transfers(plan_id, selection_job_id, selection_attempt, start_job_id, start_attempt, "
               "selected_index, piece_size, fingerprint, phase, reason_code, selected_progress, created_at_ms, updated_at_ms) "
               "VALUES(1, 1, 1, 1, 1, 0, 1024, ?, 'COMPLETE', 'TORRENT_DOWNLOAD_COMPLETE', 1.0, 1000, 1000)",
               ("d" * 64,))
    ev_json = json.dumps({"path": "ep01.mkv", "sha256": "e" * 64, "file": sig})
    db.execute("INSERT INTO media_verifications(plan_id, job_id, download_job_id, submission_fingerprint, transfer_fingerprint, "
               "evidence_json, verified_at_ms) VALUES(1, 2, 1, ?, ?, ?, 1000)",
               ("c" * 64, "d" * 64, ev_json))
    sp_manifest = {
        "entries": [
            {"role": "media", "path": rel_path, "size": 100, "sha256": "e" * 64},
            {"role": "episode", "path": nfo_rel, "size": len("<episodedetails/>")},
        ]
    }
    sp_receipts = [{"role": "media", "path": rel_path, "file": sig, "sha256": "e" * 64}]
    db.execute("INSERT INTO staging_publications(plan_id, job_id, verification_job_id, manifest_json, state, receipts_json, "
               "created_at_ms, published_at_ms) VALUES(1, 1, 2, ?, 'PUBLISHED', ?, 1000, 1000)",
               (json.dumps(sp_manifest), json.dumps(sp_receipts)))
    db.execute("PRAGMA foreign_keys = ON")


def _seed_staged_batch(db: sqlite3.Connection, staging_root: Path, *, aid: int = 101, tvdb: int = 501,
                       title: str = "Show One", infohash: str = "f" * 40, marker: str = "2" * 32,
                       rel_path: str = "Show One/Season 01/Show One - S01E02.mkv"):
    staged = staging_root / rel_path
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"v" * 200)
    sig = list(signature(staged.stat()))

    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("INSERT OR IGNORE INTO identified_anime VALUES(?, ?, ?)", (aid, tvdb, title))
    db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(10, 'test', ?, 1000, 'IDENTIFIED')",
               (json.dumps({"release_name": f"[G] {title} Batch", "torrent_infohash": infohash}).encode(),))
    db.execute("INSERT INTO identity_results(result_id, event_id, anidb_id, schema_version, series_json, validated_at_ms) "
               "VALUES(10, 10, ?, 2, ?, 1000)",
               (aid, json.dumps({"schema_version": 2, "anime": {"anidb_id": aid, "tvdb_series_id": tvdb, "title": title}})))
    db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms) "
               "VALUES(10, 10, 'VERIFY_BATCH_EPISODE', '{}', 'DONE', 0, 1000, 1000)")
    db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms) "
               "VALUES(11, 10, 'STAGE_BATCH_EPISODE', '{}', 'DONE', 0, 1000, 1000)")
    db.execute("INSERT INTO batch_verifications(batch_id, job_id, identity_result_id, anidb_id, tvdb_series_id, tvdb_season, "
               "release_sha256, info_hash, verified_at_ms) VALUES(10, 10, 10, ?, ?, 1, ?, ?, 1000)",
               (aid, tvdb, "9" * 64, infohash))
    db.execute("INSERT INTO batch_verified_files(batch_id, file_index, file_path, file_size_bytes, release_episode, "
               "anidb_episode_number, anidb_episode_id, tvdb_season, tvdb_episode, tvdb_episode_id) "
               "VALUES(10, 0, 'Show - 02.mkv', 200, 2, 2, ?, 1, 2, ?)",
               (aid * 100 + 2, tvdb * 100 + 2))
    db.execute("INSERT INTO batch_user_evaluation_plans(plan_id, batch_id, evaluation_job_id, anchor_episode, backfill_count, "
               "following_count, configuration_revision, max_episode_bytes, max_plan_bytes, planned_size_bytes, created_at_ms) "
               "VALUES(10, 10, 10, 2, 1, 1, 1, 1000, 5000, 200, 1000)")
    db.execute("INSERT INTO batch_user_evaluation_files(plan_id, file_index, tvdb_episode, selected_size_bytes) "
               "VALUES(10, 0, 2, 200)")
    db.execute("INSERT INTO batch_torrent_submissions(plan_id, reservation_job_id, first_job_id, first_attempt, info_hash, "
               "ownership_marker, base_url, save_path, category, timeout_seconds, api_major, fingerprint, configuration_revision, "
               "state, reason_code, created_at_ms, updated_at_ms) "
               "VALUES(10, 10, 10, 1, ?, ?, 'http://qb.test', '/d', 'anime-discovery', 5, 5, ?, 1, 'OWNED_STOPPED', 'TORRENT_STOPPED_CONFIRMED', 1000, 1000)",
               (infohash, marker, "8" * 64))
    ev_json = json.dumps({"path": "Show - 02.mkv", "sha256": "7" * 64, "file": sig})
    db.execute("INSERT INTO batch_episode_media(plan_id, file_index, verification_job_id, unlock_job_id, submission_fingerprint, "
               "transfer_fingerprint, evidence_json, verified_at_ms) VALUES(10, 0, 10, 10, ?, ?, ?, 1000)",
               ("8" * 64, "6" * 64, ev_json))
    bsp_manifest = {"entries": [{"role": "media", "path": rel_path, "size": 200, "sha256": "7" * 64}]}
    bsp_receipts = [{"role": "media", "path": rel_path, "file": sig, "sha256": "7" * 64}]
    db.execute("INSERT INTO batch_staging_publications(plan_id, file_index, job_id, verification_job_id, manifest_json, state, "
               "receipts_json, created_at_ms, published_at_ms) VALUES(10, 0, 11, 10, ?, 'PUBLISHED', ?, 1000, 1000)",
               (json.dumps(bsp_manifest), json.dumps(bsp_receipts)))
    db.execute("PRAGMA foreign_keys = ON")


class CutoverSeriesJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db_path = self.base / "akon.sqlite"
        self.staging = self.base / "staging"
        self.keep = self.base / "keep"
        self.downloads = self.base / "downloads"
        for d in (self.staging, self.keep, self.downloads):
            d.mkdir()

    def test_preview_apply_080_and_structure(self):
        db = _create_v78(self.db_path)
        _seed_staged_single(db, self.staging)
        db.execute("PRAGMA foreign_keys = OFF")
        # Unfinished VERIFY_RELEASE in NEEDS_REVIEW -> get_metadata (stopped)
        db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) VALUES(20, 'test', ?, 1000, 'IDENTIFIED')",
                   (json.dumps({"release_name": "[G] Show One - 03", "torrent_infohash": "2" * 40}).encode(),))
        db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, last_reason_code, available_at_ms, created_at_ms, updated_at_ms) "
                   "VALUES(20, 20, 'VERIFY_RELEASE', '{\"anidb_id\":101}', 'NEEDS_REVIEW', 'RELEASE_UNSUPPORTED', 0, 1000, 1000)")
        # Unfinished DISCOVER_EPISODE_RELEASES -> work.search
        db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms) "
                   "VALUES(21, NULL, 'DISCOVER_EPISODE_RELEASES', '{\"anidb_id\":101,\"anidb_episode_number\":4}', 'ON_HOLD', 0, 1000, 1000)")
        # Rejected series decision
        db.execute("INSERT INTO identified_anime VALUES(202, 502, 'Show Two')")
        db.execute("INSERT INTO anime_review_decisions(anidb_id, operation_id, job_id, decision, cleanup_state, decided_at_ms) "
                   "VALUES(202, 1, 99, 'REJECT', 'COMPLETE', 1000)")
        # In-progress KEEP_MEDIA with IMPORT_UNCERTAIN
        db.execute("INSERT INTO identified_anime VALUES(303, 503, 'Show Three')")
        db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms) "
                   "VALUES(30, NULL, 'KEEP_MEDIA', '{}', 'RETRY_WAIT', 0, 1000, 1000)")
        keep_data = {
            "selection": {"anidb_id": 303, "tvdb_series_id": 503, "tvdb_season": 1, "tvdb_episode": 1},
            "series": {"id": 77, "path": "/keep/Show Three"},
            "episode_id": 88,
            "command_id": 999,
        }
        db.execute("INSERT INTO keep_imports(job_id, phase, data_json, updated_at_ms) VALUES(30, 'IMPORT_UNCERTAIN', ?, 1000)",
                   (json.dumps(keep_data),))
        db.commit()
        db.close()

        # Preview changes nothing
        preview = cutover.execute_cutover(self.db_path, apply=False)
        self.assertFalse(preview["already_cut_over"])
        self.assertEqual(preview["refused_jobs"], [])
        self.assertEqual(preview["series_count"], 3)
        self.assertEqual(preview["converted_jobs"], 3)

        db_check = sqlite3.connect(self.db_path)
        self.assertEqual(db_check.execute("PRAGMA user_version").fetchone()[0], 78)
        db_check.close()

        # Refuses if services active
        with mock.patch.object(cutover, "services_stopped", return_value=["anime-discovery-api.service"]):
            with self.assertRaisesRegex(RuntimeError, "Services must be stopped"):
                cutover.execute_cutover(self.db_path, apply=True)

        # Apply with services stopped
        with mock.patch.object(cutover, "services_stopped", return_value=[]):
            applied = cutover.execute_cutover(self.db_path, apply=True)
        self.assertTrue(applied["applied"])
        self.assertTrue(applied["migrated_to_v79"])
        self.assertEqual(applied["refused_jobs"], [])
        self.assertTrue(Path(applied["backup"]).exists())

        db2 = sqlite3.connect(self.db_path)
        self.assertEqual(db2.execute("PRAGMA user_version").fetchone()[0], 79)
        s101 = get_series(db2, 101)
        self.assertEqual(s101.state, "WORKING")
        self.assertEqual(s101.episodes["S01E01"]["state"], "staged")
        self.assertEqual(s101.episodes["S01E01"]["torrent"], "a" * 40)
        self.assertIn(4, s101.work["search"]["wanted"])
        stopped = next(t for t in s101.torrents if t["infohash"] == "2" * 40)
        self.assertEqual(stopped["step"], "get_metadata")
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["why"], "RELEASE_UNSUPPORTED")

        s303 = get_series(db2, 303)
        self.assertEqual(s303.state, "WORKING")
        self.assertEqual(s303.work["keep"]["step"], "sonarr_import")
        self.assertEqual(s303.work["keep"]["imports"]["S01E01"]["command_id"], 999)

        self.assertEqual(decision(db2, 202), "REJECT")
        self.assertEqual(decision(db2, 303), "ACCEPT")

        # Migration 080 runs cleanly and matches fresh DB structure
        db2.executescript((migrations.SQL_DIR / "080_series_only.sql").read_text())
        self.assertEqual(db2.execute("PRAGMA user_version").fetchone()[0], 80)
        self.assertEqual(db2.execute("PRAGMA foreign_key_check").fetchall(), [])
        fresh = sqlite3.connect(":memory:")
        initialize(fresh)
        self.assertEqual(_structure(db2), _structure(fresh))
        fresh.close()
        db2.close()

    def test_converted_staged_series_can_be_kept_and_rejected(self):
        # 1. Cutover a series with both a staged single episode and a staged season-pack episode, then Keep it
        db = _create_v78(self.db_path)
        _seed_staged_single(db, self.staging, aid=101, tvdb=501, title="Show One")
        _seed_staged_batch(db, self.staging, aid=101, tvdb=501, title="Show One")
        db.commit()
        db.close()

        with mock.patch.object(cutover, "services_stopped", return_value=[]):
            cutover.execute_cutover(self.db_path, apply=True)

        db_keep = sqlite3.connect(self.db_path)
        keep_series_sync(db_keep, 101, now=10)
        db_keep.commit()

        cfg = Configuration(str(self.db_path))
        cfg.values.update({
            "storage.user_evaluation_root": str(self.staging),
            "storage.keep_root": str(self.keep),
            "storage.download_root": str(self.downloads),
            "sonarr.base_url": "http://sonarr.test",
            "sonarr.staging_root": "",
            "sonarr.library_root": "",
            "sonarr.quality_profile_id": 4,
            "sonarr.timeout_seconds": 5,
            "sonarr.long_running_threshold": 0,
            "sonarr.backfill_long_running": False,
            "shoko.base_url": "",
            "jellyfin.base_url": "",
            "acquisition.audio_check": "off",
            "qbittorrent.base_url": "http://qb.test",
            "qbittorrent.category": "anime-discovery",
            "qbittorrent.timeout_seconds": 5,
        })
        fake_sonarr = FakeSonarr(self)
        fake_qb = FakeQb()
        fake_qb.torrent = {"state": "uploading", "files": (), "priorities": {}, "progress": {}}
        torrents_mgr = mock.Mock()
        torrents_mgr.client.return_value = fake_qb
        secrets = mock.Mock()
        secrets.sonarr_api_key.return_value = "sonarr-key"
        ctx = SeriesContext(db=db_keep, configuration=cfg, verify=None, secrets=secrets, torrents=torrents_mgr,
                            cancelled=threading.Event())
        with mock.patch("src.anime.sonarr.Client", fake_sonarr), \
                mock.patch("src.anime.sonarr.existing_file", return_value={"relative_path": "Show One/ep.mkv"}):
            asyncio.run(run_series_attempt(db_keep, ctx, now=100))
            fake_sonarr.command = "completed"
            asyncio.run(run_series_attempt(db_keep, ctx, now=10**13))
        row = get_series(db_keep, 101)
        self.assertEqual(row.state, "KEPT")
        self.assertEqual(row.episodes["S01E01"]["state"], "kept")
        self.assertEqual(row.episodes["S01E02"]["state"], "kept")
        self.assertEqual(fake_qb.calls.count(("remove",)), 2)
        db_keep.close()

        # 2. Cutover another staged series and Reject it with real run_reject + FakeQb
        db_path2 = self.base / "reject.sqlite"
        db_r = _create_v78(db_path2)
        _seed_staged_single(db_r, self.staging, aid=404, tvdb=504, title="Show Four",
                            infohash="e" * 40, marker="3" * 32,
                            rel_path="Show Four/Season 01/Show Four - S01E01.mkv")
        db_r.commit()
        db_r.close()

        with mock.patch.object(cutover, "services_stopped", return_value=[]):
            cutover.execute_cutover(db_path2, apply=True)

        db_r2 = sqlite3.connect(db_path2)
        row_r = get_series(db_r2, 404)
        fake_qb2 = FakeQb()
        torrents_mgr2 = mock.Mock()
        torrents_mgr2.client.return_value = fake_qb2
        ctx2 = SeriesContext(db=db_r2, configuration=cfg, verify=None, torrents=torrents_mgr2,
                             cancelled=threading.Event(), now=2000)
        asyncio.run(run_reject(row_r, ctx2))
        self.assertIn(("remove",), fake_qb2.calls)
        self.assertFalse((self.staging / "Show Four/Season 01/Show Four - S01E01.mkv").exists())
        self.assertEqual(row_r.episodes["S01E01"]["state"], "dropped")
        db_r2.close()

    def test_unconvertible_job_is_reported_not_skipped(self):
        db = _create_v78(self.db_path)
        db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, available_at_ms, created_at_ms, updated_at_ms) "
                   "VALUES(77, NULL, 'VERIFY_RELEASE', '{}', 'ON_HOLD', 0, 1000, 1000)")
        db.commit()
        db.close()

        preview = cutover.execute_cutover(self.db_path, apply=False)
        self.assertEqual(len(preview["refused_jobs"]), 1)
        self.assertEqual(preview["refused_jobs"][0]["job_id"], 77)
        self.assertIn("anidb_id", preview["refused_jobs"][0]["reason"])

    def _job(self, db, job_id, kind, state, reason, payload, event_hash=None):
        if event_hash is not None:
            db.execute("INSERT INTO incoming_events(event_id, source, raw_body, received_at_ms, state) "
                       "VALUES(?, 'test', ?, 1000, 'IDENTIFIED')",
                       (job_id, json.dumps({"release_name": f"[G] Show - {job_id}",
                                            "torrent_infohash": event_hash}).encode()))
        db.execute("INSERT INTO jobs(job_id, event_id, kind, payload_json, state, last_reason_code, available_at_ms, "
                   "created_at_ms, updated_at_ms) VALUES(?, ?, ?, ?, ?, ?, 0, 1000, 1000)",
                   (job_id, job_id if event_hash else None, kind, json.dumps(payload), state, reason))

    def test_held_failed_stopped_and_decided_work_converts_by_what_it_was_waiting_for(self):
        """Owner, 2026-11-27: the old states mean different things; each converts to what resumes it."""
        db = _create_v78(self.db_path)
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("INSERT INTO identified_anime VALUES(404, 504, 'Show Four')")
        # ON_HOLD: the series job has no hold; the series knows what it works on, so this is waiting work.
        self._job(db, 40, "RESERVE_EPISODE", "ON_HOLD", "EPISODE_RESERVED", {"anidb_id": 404}, "4" * 40)
        # FAILED by a program error: not finished, so it converts, stopped.
        self._job(db, 41, "DOWNLOAD_TORRENT", "FAILED", "JOB_HANDLER_FAILED", {"anidb_id": 404}, "5" * 40)
        # FAILED for any other reason: finished (and 080 accepts it); not converted.
        self._job(db, 42, "PREPARE_ACQUISITION", "FAILED", "TORRENT_METADATA_TIMEOUT", {"anidb_id": 404}, "6" * 40)
        # A rejected series: its held job closes with no torrent.
        db.execute("INSERT INTO identified_anime VALUES(505, 505, 'Show Five')")
        db.execute("INSERT INTO anime_review_decisions(anidb_id, operation_id, job_id, decision, cleanup_state, "
                   "decided_at_ms) VALUES(505, 1, 99, 'REJECT', 'COMPLETE', 1000)")
        self._job(db, 50, "SELECT_USER_EVALUATION", "ON_HOLD", "ANIME_REJECTED", {"anidb_id": 505})
        # A Keep Sonarr refused (Needs review): stays stopped until Retry, never runs at the cutover.
        db.execute("INSERT INTO identified_anime VALUES(606, 506, 'Show Six')")
        self._job(db, 60, "KEEP_MEDIA", "NEEDS_REVIEW", "KEEP_IMPORT_REVIEW", {})
        db.execute("INSERT INTO keep_imports(job_id, phase, data_json, updated_at_ms) VALUES(60, 'IMPORT_UNCERTAIN', ?, 1000)",
                   (json.dumps({"selection": {"anidb_id": 606, "tvdb_series_id": 506, "tvdb_season": 1,
                                              "tvdb_episode": 1}, "series": {"id": 7, "path": "/keep/Six"}}),))
        db.commit()
        db.close()

        with mock.patch.object(cutover, "services_stopped", return_value=[]):
            applied = cutover.execute_cutover(self.db_path, apply=True)
        self.assertEqual(applied["refused_jobs"], [])
        outcomes = {o["job_id"]: o["outcome"] for o in applied["job_outcomes"]}
        self.assertEqual(sorted(outcomes), [40, 41, 50, 60])
        self.assertEqual(outcomes[50], "closed: series rejected")

        db = sqlite3.connect(self.db_path)
        torrents = {t["infohash"]: t for t in get_series(db, 404).torrents}
        self.assertEqual(sorted(torrents), ["4" * 40, "5" * 40])
        self.assertEqual(torrents["4" * 40]["state"], "waiting")
        self.assertEqual((torrents["5" * 40]["state"], torrents["5" * 40]["why"]), ("stopped", "JOB_HANDLER_FAILED"))
        self.assertEqual(get_series(db, 505).torrents, [])
        six = get_series(db, 606)
        self.assertEqual(six.work["keep"]["stopped"], "KEEP_IMPORT_REVIEW")
        self.assertEqual((six.state, six.wake_at_ms), ("ASLEEP", None))      # Not taken until Retry.
        self.assertEqual(db.execute("SELECT state FROM jobs WHERE job_id=42").fetchone(), ("FAILED",))
        # 080 accepts what is left.
        db.executescript((migrations.SQL_DIR / "080_series_only.sql").read_text())
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 80)
        db.close()

    def test_a_canceled_acquisitions_publications_are_not_staged(self):
        """Production, 2026-11-27: Cleanup on a stuck acquisition removed its staged files, but the
        old rows still say PUBLISHED. They must not come back as staged episodes."""
        db = _create_v78(self.db_path)
        _seed_staged_batch(db, self.staging)
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("INSERT INTO job_operations(operation_id, state, cleanup_state, reason_code, created_at_ms, updated_at_ms) "
                   "VALUES(9, 'CANCELED', 'COMPLETE', 'CANCELLATION_COMPLETE', 1000, 2000)")
        db.execute("INSERT INTO job_operation_members(operation_id, job_id) VALUES(9, 11)")   # The stage job.
        db.commit()
        db.close()
        with mock.patch.object(cutover, "services_stopped", return_value=[]):
            cutover.execute_cutover(self.db_path, apply=True)
        db = sqlite3.connect(self.db_path)
        series = get_series(db, 101)
        self.assertTrue(series is None or "S01E02" not in series.episodes)
        db.close()


if __name__ == "__main__":
    unittest.main()
