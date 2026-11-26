"""Tests for fixes/cutover_series_job.py (SJ-9)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fixes.cutover_series_job import execute_cutover, migrate_qbittorrent
from src.anime.storage.sqlite import (
    accept_event_and_job,
    connect,
    initialize,
    save_identity,
    transaction,
)


class CutoverSeriesJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "queue.sqlite"
        db = connect(self.db_path)
        initialize(db)
        db.close()

    def _seed_legacy_work(self):
        db = connect(self.db_path)
        try:
            # Series 101: unfinished VERIFY_RELEASE + staged episode + episode reservation
            ev1, job1 = accept_event_and_job(
                db,
                "test",
                json.dumps({"release_name": "[Group] Show One - 02", "torrent_infohash": "a" * 40}).encode(),
                kind="VERIFY_RELEASE",
                payload={"anidb_id": 101},
            )
            id1 = save_identity(
                db,
                ev1,
                {"schema_version": 2, "anime": {"anidb_id": 101, "tvdb_series_id": 501, "title": "Show One"}},
            )
            ev_staged, job_staged = accept_event_and_job(
                db,
                "test",
                json.dumps({"release_name": "[Group] Show One - 01", "torrent_infohash": "b" * 40}).encode(),
                kind="STAGE_USER_EVALUATION_MEDIA",
                payload={"anidb_id": 101},
            )
            db.execute("PRAGMA foreign_keys = OFF")
            with transaction(db):
                db.execute("UPDATE jobs SET state='ON_HOLD', last_reason_code='USER_HOLD' WHERE job_id=?", (job1,))
                db.execute(
                    "INSERT INTO anime_title_metadata(anidb_id, english_title) VALUES(101, 'Show One')"
                )
                db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?", (job_staged,))
                db.execute("INSERT OR IGNORE INTO identified_episodes VALUES(1001, 101)")
                db.execute(
                    "INSERT INTO release_verifications(job_id, identity_result_id, anidb_id, release_sha256, "
                    "verification_scope, anidb_episode_number, anidb_episode_id, tvdb_series_id, tvdb_season, "
                    "tvdb_episode, tvdb_episode_id, verified_at_ms) "
                    "VALUES(?, ?, 101, ?, 'DECLARED_SINGLE_EPISODE', 1, 1001, 501, 1, 1, 2001, 1000)",
                    (job_staged, id1, "c" * 64),
                )
                db.execute(
                    "INSERT INTO episode_reservations(owner_job_id, evaluation_job_id, verification_id, plan_id, "
                    "anidb_id, anidb_episode_id, tvdb_series_id, tvdb_season, tvdb_episode, tvdb_episode_id, state, reserved_at_ms) "
                    "VALUES(?, ?, 1, 1, 101, 1001, 501, 1, 1, 2001, 'ACTIVE', 1000)",
                    (job_staged, job_staged),
                )
                db.execute(
                    "INSERT INTO staging_publications(plan_id, job_id, verification_job_id, manifest_json, state, receipts_json, created_at_ms, published_at_ms) "
                    "VALUES(1, ?, ?, ?, 'PUBLISHED', ?, 1000, 1000)",
                    (
                        job_staged,
                        job1,
                        json.dumps({
                            "anidb_id": 101,
                            "tvdb_season": 1,
                            "tvdb_episode": 1,
                            "anidb_episode_number": 1,
                            "staged_path": "/srv/anime-discovery/staging/Show One/S01E01.mkv",
                        }),
                        json.dumps({"staged_path": "/srv/anime-discovery/staging/Show One/S01E01.mkv"}),
                    ),
                )

                # Series 202: rejected series
                db.execute(
                    "INSERT INTO identified_anime(anidb_id, tvdb_series_id, title) VALUES(202, 502, 'Show Two')"
                )
                db.execute(
                    "INSERT INTO anime_review_decisions(anidb_id, operation_id, job_id, decision, cleanup_state, decided_at_ms) "
                    "VALUES(202, 0, 1, 'REJECT', 'COMPLETE', 1000)"
                )

                # Old job_history row to be wiped
                db.execute(
                    "INSERT INTO job_history(job_id, event_code, component, occurred_at_ms, details_json) "
                    "VALUES(?, 'CREATED', 'api', 1000, '{}')",
                    (job1,),
                )
            db.execute("PRAGMA foreign_keys = ON")
        finally:
            db.close()

    def test_preview_and_apply_cutover(self):
        self._seed_legacy_work()

        # Preview does not mutate DB
        preview = execute_cutover(self.db_path, apply=False)
        self.assertFalse(preview["already_cut_over"])
        self.assertEqual(preview["unfinished_jobs"], 1)
        self.assertEqual(preview["series_count"], 2)
        self.assertGreaterEqual(preview["old_history_rows"], 1)
        self.assertGreater(preview["old_tables_to_drop"], 0)
        self.assertIsNone(preview["backup"])

        # Apply converts work, updates paths, drops old tables, backs up DB
        applied = execute_cutover(self.db_path, apply=True)
        self.assertTrue(applied["applied"])
        self.assertIsNotNone(applied["backup"])
        self.assertTrue(Path(applied["backup"]).exists())
        self.assertEqual(applied["paths_updated"], 1)

        db = connect(self.db_path)
        try:
            # Verify series rows
            row101 = db.execute(
                "SELECT title, state, torrents_json, episodes_json, recent_json FROM series WHERE anidb_id=101"
            ).fetchone()
            self.assertEqual(row101[0], "Show One")
            self.assertEqual(row101[1], "WORKING")
            torrents = json.loads(row101[2])
            self.assertEqual(len(torrents), 1)
            self.assertEqual(torrents[0]["infohash"], "a" * 40)
            self.assertEqual(torrents[0]["step"], "check_release")
            self.assertEqual(torrents[0]["state"], "waiting")
            self.assertEqual(torrents[0]["why"], "USER_HOLD")

            episodes = json.loads(row101[3])
            self.assertEqual(episodes["S01E01"]["state"], "staged")
            self.assertEqual(episodes["S01E01"]["file"], "/srv/akon/staging/Show One/S01E01.mkv")

            recent = json.loads(row101[4])
            self.assertEqual(len(recent), 1)
            self.assertIn("converted at cutover", recent[0])

            row202 = db.execute("SELECT title, state FROM series WHERE anidb_id=202").fetchone()
            self.assertEqual(row202, ("Show Two", "REJECTED"))

            # Verify old tables dropped, kept tables preserved
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertNotIn("jobs", tables)
            self.assertNotIn("job_history", tables)
            self.assertNotIn("job_operations", tables)
            self.assertNotIn("episode_reservations", tables)
            self.assertIn("series", tables)
            self.assertIn("identified_anime", tables)
            self.assertIn("provider_public_cache", tables)
            self.assertIn("notifications", tables)
        finally:
            db.close()

        # Idempotent re-run
        second = execute_cutover(self.db_path, apply=True)
        self.assertTrue(second["already_cut_over"])
        self.assertFalse(second["applied"])

    def test_migrate_qbittorrent(self):
        responses = [
            MagicMock(__enter__=lambda s: MagicMock(read=lambda: b"Ok."), __exit__=lambda *a: False),
            MagicMock(
                __enter__=lambda s: MagicMock(
                    read=lambda: json.dumps([
                        {"hash": "h1", "save_path": "/srv/anime-discovery/downloads/t1"}
                    ]).encode()
                ),
                __exit__=lambda *a: False,
            ),
            MagicMock(close=lambda: None),
            MagicMock(close=lambda: None),
            MagicMock(close=lambda: None),
        ]
        opener = MagicMock()
        opener.open.side_effect = responses
        with patch("urllib.request.build_opener", return_value=opener):
            res = migrate_qbittorrent("http://qb.local:8080", "admin", "secret")
        self.assertEqual(res, {"moved_torrents": 1, "relocated_paths": 1})


if __name__ == "__main__":
    unittest.main()
