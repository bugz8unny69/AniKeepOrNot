"""Offline migration parity and rollback for the complete batch lifecycle."""
import sqlite3
import unittest

from src.anime.storage import migrations
from src.anime.storage.sqlite import initialize


class BatchRecoveryMigrationsTests(unittest.TestCase):
    def prefix(self,version):
        db=sqlite3.connect(':memory:')
        db.execute('PRAGMA foreign_keys=ON')
        for path in migrations.SCRIPTS[:version]:
            db.executescript((migrations.SQL_DIR/path).read_text())
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
        return db

    def test_all_batch_upgrades_have_fresh_schema_parity(self):
        self.assertGreaterEqual(migrations.LATEST_VERSION,41)
        upgraded=self.prefix(30)
        self.addCleanup(upgraded.close)
        for version in range(31,migrations.LATEST_VERSION+1):
            upgraded.executescript(
                (migrations.SQL_DIR/migrations.SCRIPTS[version-1]).read_text())
            self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0],version)
            self.assertEqual(migrations._structure(upgraded),
                             migrations._reference(version,migrations._scripts()))
            self.assertEqual(upgraded.execute('PRAGMA foreign_key_check').fetchall(),[])
        fresh=sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        fresh.execute('PRAGMA foreign_keys=ON')
        initialize(fresh)
        self.assertEqual(fresh.execute('PRAGMA user_version').fetchone(),
                         (migrations.LATEST_VERSION,))
        self.assertEqual(migrations._structure(upgraded),migrations._structure(fresh))

    def test_each_new_migration_rolls_back_all_new_tables_on_error(self):
        new_tables=(
            'batch_torrent_transfers','batch_probe_media','batch_torrent_unlocks',
            'batch_probe_stops','batch_episode_media','batch_staging_publications',
            'batch_keep_imports','batch_download_cleanup','batch_keep_cleanup',
            'batch_submission_inventories','batch_cancellation_cleanup',
            'sonarr_backfill_results','sonarr_episode_selections',
            'sonarr_episode_candidates','sonarr_magnet_intents','acquisition_plans_next',
            'sonarr_magnet_intents_next','sonarr_backfill_results_v48',
            'manual_identity_matches', None, 'batch_metadata_admissions',
            'anime_title_metadata',  # v52 adds optional display names
            'sonarr_backfill_results_v53',  # v53 allows WAITING_FOR_DUB results
            'prowlarr_search_snapshots',  # v54 adds durable Prowlarr matching snapshots
            'manual_release_submissions',  # v55 adds manual release submissions
            'trial_plans_v56',  # v56 keys trial selection by AniDB episode
            'release_verifications_v57',  # v57 allows acquiring and staging without TVDB placement
            None,  # v58 adds FAILED job state
            'identity_candidate_evidence',  # v59 adds durable candidate evidence
            'batch_candidate_scopes',  # v60 adds batch candidate operations
            'batch_candidate_stops',  # v61 records why each candidate stopped
            'manual_release_archive',  # v62 (NextGen) archives replaced manual release submissions
            'service_status',  # v63 adds the last known status of each external service
            'user_evaluation_plans',  # v64 renames tables; a failed v64 leaves no new name behind
            'user_evaluation_checks_v64',  # v65 rebuilds tables whose checks name codes
            None,  # v66 rewrites retired service codes
            'resources',  # v67 records every file, folder and torrent a job creates
            None)  # v68 rewrites stored src/nextgen paths to src/anime; rename_package: keep
        self.assertEqual(len(new_tables),migrations.LATEST_VERSION-30)
        for version,table in zip(range(31,migrations.LATEST_VERSION+1),new_tables):
            with self.subTest(version=version):
                db=self.prefix(version-1)
                try:
                    before=migrations._structure(db)
                    script=(migrations.SQL_DIR/migrations.SCRIPTS[version-1]).read_text()
                    script=script.replace(
                        f'PRAGMA user_version={version};',
                        f'SELECT * FROM no_such_table;\nPRAGMA user_version={version};')
                    with self.assertRaises(sqlite3.Error):
                        db.executescript(script)
                    if db.in_transaction:db.rollback()
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone(),
                                     (version-1,))
                    self.assertEqual(migrations._structure(db),before)
                    if table is not None:
                        self.assertIsNone(db.execute(
                            "SELECT name FROM sqlite_master WHERE name=?",
                            (table,)).fetchone())
                finally:
                    db.close()


if __name__=='__main__':
    unittest.main()
