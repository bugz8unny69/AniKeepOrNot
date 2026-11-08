"""Offline provider reservation/cache/migration cases; intentionally unrun."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.anime.providers.errors import ProviderDeferred
from src.anime.storage.provider_cache import ProviderStore
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import initialize


class ProviderStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/"queue.sqlite"
        db = sqlite3.connect(self.path)
        initialize(db)
        db.close()
        self.now = 1000
        self.store = ProviderStore(self.path,clock=lambda:self.now)

    def test_concurrent_process_style_connections_share_reservation(self):
        def reserve(_):
            try: return self.store.reserve("anidb",10000)[1]
            except ProviderDeferred: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve,range(2)))
        self.assertEqual(sum(result is not None for result in results),1)
        # A fresh object simulates a worker restart against the same state.
        other = ProviderStore(self.path,clock=lambda:self.now)
        with self.assertRaises(ProviderDeferred) as deferred: other.reserve("anidb",10000)
        self.assertEqual(deferred.exception.delay_ms,10000)
        self.now += 10000
        self.assertIsNotNone(other.reserve("anidb",10000)[1])

    def test_public_cache_hit_precedes_rate_reservation_and_expires_at_equality(self):
        _,token = self.store.reserve("anidb",10000,cache_key="anime:1")
        self.store.public_put("anidb","anime:1",b'<anime/>',1000,token)
        self.assertEqual(self.store.reserve("anidb",10000,cache_key="anime:1"),(b'<anime/>',None))
        self.now = 2000
        self.assertIsNone(self.store.public_get("anidb","anime:1"))
        with self.assertRaises(ProviderDeferred): self.store.reserve("anidb",10000,cache_key="anime:1")

    def test_error_cooldown_survives_restart_and_success_cannot_undo_it(self):
        _,token = self.store.reserve("anidb",10000)
        self.store.failure("anidb",minimum_ms=600000)
        self.store.success("anidb",token)
        with self.assertRaises(ProviderDeferred) as deferred:
            ProviderStore(self.path,clock=lambda:self.now).reserve("anidb",10000)
        self.assertEqual(deferred.exception.delay_ms,600000)

    def test_stale_reservation_cannot_publish_over_newer_cache(self):
        _,old = self.store.reserve("anidb",10000)
        self.now += 10000
        _,new = self.store.reserve("anidb",10000)
        self.store.public_put("anidb","anime:1",b'new',1000,new)
        self.store.public_put("anidb","anime:1",b'old',1000,old)
        self.assertEqual(self.store.public_get("anidb","anime:1"),b'new')
        for provider in ("tvdb", "token"):
            with self.assertRaises(ValueError): self.store.public_put(provider,"key",b'secret',1000,new)

    def test_v8_upgrade_preserves_rows_and_matches_fresh_schema(self):
        db = sqlite3.connect(":memory:")
        fresh = sqlite3.connect(":memory:")
        try:
            for script in SCRIPTS[:8]: db.executescript((SQL_DIR/script).read_text())
            db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('test',X'61',1)")
            db.commit()
            before = db.execute("SELECT * FROM incoming_events").fetchall()
            db.executescript((SQL_DIR/"009_provider_cache.sql").read_text())
            db.executescript((SQL_DIR/"010_release_verification.sql").read_text())
            db.executescript((SQL_DIR/"011_trial_windows.sql").read_text())
            db.executescript((SQL_DIR/"012_episode_reservations.sql").read_text())
            db.executescript((SQL_DIR/"013_acquisition_plans.sql").read_text())
            db.executescript((SQL_DIR/"014_acquisition_budgets.sql").read_text())
            db.executescript((SQL_DIR/"015_torrent_submissions.sql").read_text())
            db.executescript((SQL_DIR/"016_torrent_transfers.sql").read_text())
            db.executescript((SQL_DIR/"017_cancellation.sql").read_text())
            db.executescript((SQL_DIR/"018_management_actions.sql").read_text())
            db.executescript((SQL_DIR/"019_media_verifications.sql").read_text())
            db.executescript((SQL_DIR/"020_staging_publications.sql").read_text())
            db.executescript((SQL_DIR/"021_reject_decisions.sql").read_text())
            db.executescript((SQL_DIR / "022_keep_imports.sql").read_text())
            db.executescript((SQL_DIR / "023_reservation_release.sql").read_text())
            from src.anime.storage.migrations import SCRIPTS as current_scripts
            for name in current_scripts[23:]:db.executescript((SQL_DIR/name).read_text())
            initialize(fresh)
            self.assertEqual(_structure(db),_structure(fresh))
            self.assertEqual(db.execute("SELECT * FROM incoming_events").fetchall(),before)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],len(current_scripts))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(),[])
        finally:
            db.close()
            fresh.close()

    def test_wrong_version_and_failed_migration_are_rolled_back(self):
        for version in (7,8,9):
            db = sqlite3.connect(":memory:")
            try:
                db.execute("PRAGMA user_version="+str(version))
                sql = (SQL_DIR/"009_provider_cache.sql").read_text()
                if version==8: sql=sql.replace("PRAGMA user_version=9;","SELECT * FROM missing;\nPRAGMA user_version=9;")
                with self.assertRaises(sqlite3.Error): db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='provider_limits'").fetchone())
            finally: db.close()


    def test_cache_size_bound_and_future_cache_entries_are_not_reused(self):
        _,token = self.store.reserve("anidb",10000)
        with patch("src.anime.storage.provider_cache.MAX_CACHE_BYTES",4):
            self.store.public_put("anidb","anime:1",b'abc',10000,token)
            self.now += 1
            self.store.public_put("anidb","anime:2",b'def',10000,token)
        self.assertIsNone(self.store.public_get("anidb","anime:1"))
        self.assertEqual(self.store.public_get("anidb","anime:2"),b'def')
        self.now -= 2
        self.assertIsNone(self.store.public_get("anidb","anime:2"))


    def test_missing_database_is_not_initialized_by_provider_library(self):
        from src.anime.storage.job_queue import QueueUnavailable
        path = self.path.with_name("missing.sqlite")
        with self.assertRaises(QueueUnavailable): ProviderStore(path).reserve("anidb",10000)
        self.assertFalse(path.exists())
