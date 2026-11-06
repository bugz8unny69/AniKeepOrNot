"""End-to-end integration tests for Batch Keep and Cleanup (W-01, W-02, W-03).

Proves that:
1. BatchKeepJournal.load accepts resolver keyword argument from Service._run without TypeError.
2. BatchKeepJournal accepts valid Stage publication receipts where media has hash_checked=False.
3. Service.run succeeds with BatchKeepJournal and updates batch_keep_imports to COMPLETE.
4. BatchPostKeepCleanup succeeds with copy_only=True when media receipt has hash_checked=False.
"""
import time
from functools import partial
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from src.nextgen.keep_selection import KeepSelection
from src.nextgen.keep_service import Service
from src.nextgen.batch_post_keep_cleanup import BatchPostKeepCleanup
from src.nextgen.storage.batch_keep_imports import BatchKeepJournal
from src.nextgen.storage.job_queue import JobQueue, Claim
from src.nextgen.storage.migrations import LATEST_VERSION
from src.nextgen.storage.sqlite import connect, initialize, transaction
from src.nextgen.sonarr import Client, Options


class BatchKeepServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'test.sqlite'
        self.db = connect(self.database)
        initialize(self.db)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], LATEST_VERSION)

        # Setup test paths
        self.staging_root = self.root / 'staging'
        self.staging_root.mkdir()
        self.keep_root = self.root / 'keep'
        self.keep_root.mkdir()
        self.library_root = self.root / 'library'
        self.library_root.mkdir()

        # Create staged media file
        self.staged_media = self.staging_root / 'Show S01E01.mkv'
        self.staged_media.write_bytes(b'video content' * 100)
        stat = self.staged_media.stat()
        self.file_sig = [int(stat.st_dev), int(stat.st_ino), int(stat.st_size),
                         int(stat.st_mtime_ns), int(stat.st_ctime_ns)]
        root_stat = self.staging_root.stat()
        self.root_sig = [int(root_stat.st_dev), int(root_stat.st_ino)]

        # Configuration and secrets double
        self.config_data = {
            'storage.database_path': str(self.database),
            'storage.keep_root': str(self.keep_root),
            'storage.library_root': str(self.library_root),
            'sonarr.base_url': 'http://sonarr.invalid',
            'sonarr.quality_profile_id': 1,
            'sonarr.timeout_seconds': 30,
            'sonarr.staging_root': None,
            'sonarr.library_root': None,
        }
        self.configuration = MagicMock()
        self.configuration.value.side_effect = lambda key: self.config_data[key]
        self.secrets = MagicMock()
        self.secrets.sonarr_api_key.return_value = 'sonarr-key'

    def tearDown(self):
        self.db.close()

    def _seed_batch_staging(self, *, stage_job_id=61, operation_id=7, plan_id=15, file_index=0):
        manifest = {
            'root': str(self.staging_root),
            'root_identity': self.root_sig,
            'transfer': 'COPY',
            'entries': [
                {'role': 'media', 'path': 'Show S01E01.mkv', 'size': len(b'video content' * 100),
                 'sha256': 'a' * 64},
                {'role': 'episode', 'path': 'Show S01E01.nfo', 'size': 10, 'sha256': 'b' * 64},
                {'role': 'series', 'path': 'tvshow.nfo', 'size': 10, 'sha256': 'c' * 64},
            ]
        }
        # Notice hash_checked is False for media, exactly as staging.py Publisher emits
        receipts = [
            {'role': 'media', 'path': 'Show S01E01.mkv', 'root': self.root_sig,
             'file': self.file_sig, 'sha256': 'a' * 64, 'hash_checked': False,
             'artifact': 1},
            {'role': 'episode', 'path': 'Show S01E01.nfo', 'root': self.root_sig,
             'file': self.file_sig, 'sha256': 'b' * 64, 'hash_checked': True,
             'artifact': 2},
            {'role': 'series', 'path': 'tvshow.nfo', 'root': self.root_sig,
             'file': self.file_sig, 'sha256': 'c' * 64, 'hash_checked': True,
             'artifact': None},
        ]
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) "
                "VALUES(1,'test',X'61',0)")
            self.db.execute(
                "INSERT INTO identified_anime(anidb_id,tvdb_series_id,title) VALUES(10,20,'Show')")
            self.db.execute(
                "INSERT INTO identity_results(result_id,event_id,anidb_id,schema_version,series_json,validated_at_ms) "
                "VALUES(1,1,10,2,'{}',0)")
            self.db.execute(
                "INSERT INTO jobs(job_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms) "
                "VALUES(50, 'VERIFY_RELEASE', '{}', 'DONE', 0, 0, 0)")
            sha64 = '0' * 64
            hash40 = 'a' * 40
            sha_info = 'b' * 64
            marker32 = 'c' * 32
            fp_d = 'd' * 64
            fp_e = 'e' * 64
            fp_f = 'f' * 64
            fp_1 = '1' * 64
            fp_2 = '2' * 64
            self.db.execute(
                "INSERT INTO batch_verifications(batch_id,job_id,identity_result_id,anidb_id,tvdb_series_id,"
                "tvdb_season,release_sha256,info_hash,info_sha256,verified_at_ms) "
                "VALUES(1,50,1,10,20,1,?,?,?,0)", (sha64, hash40, sha_info))
            self.db.execute(
                "INSERT INTO jobs(job_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms) "
                "VALUES(51, 'EVALUATE_BATCH', '{}', 'DONE', 0, 0, 0)")
            self.db.execute(
                "INSERT INTO batch_user_evaluation_plans(plan_id,batch_id,evaluation_job_id,anchor_episode,"
                "backfill_count,following_count,configuration_revision,max_episode_bytes,max_plan_bytes,"
                "planned_size_bytes,created_at_ms) "
                "VALUES(?,1,51,1,1,1,1,10000000,20000000,1000,0)", (plan_id,))
            self.db.execute(
                "INSERT INTO batch_user_evaluation_files(plan_id,file_index,tvdb_episode,selected_size_bytes) "
                "VALUES(?,?,1,1000)", (plan_id, file_index))
            self.db.execute(
                "INSERT INTO batch_torrent_submissions(plan_id,reservation_job_id,first_job_id,first_attempt,"
                "info_hash,info_sha256,ownership_marker,base_url,save_path,category,timeout_seconds,"
                "api_major,fingerprint,configuration_revision,state,reason_code,created_at_ms,updated_at_ms) "
                "VALUES(?,51,51,1,?,?,?,'http://qb','/downloads','anime',30,"
                "5,?,1,'OWNED_STOPPED','TORRENT_STOPPED_CONFIRMED',0,0)",
                (plan_id, hash40, sha_info, marker32, fp_d))
            self.db.execute(
                "INSERT INTO batch_torrent_transfers(plan_id,transfer_job_id,transfer_attempt,selected_index,"
                "piece_size,fingerprint,phase,reason_code,start_job_id,start_attempt,selected_progress,"
                "created_at_ms,updated_at_ms) "
                "VALUES(?,51,1,0,16384,?,'COMPLETE','TORRENT_DOWNLOAD_COMPLETE',51,1,1.0,0,0)",
                (plan_id, fp_e))
            self.db.execute(
                "INSERT INTO batch_probe_media(plan_id,verification_job_id,transfer_job_id,"
                "submission_fingerprint,transfer_fingerprint,evidence_json,verified_at_ms) "
                "VALUES(?,51,51,?,?,'{}',0)", (plan_id, fp_f, fp_f))
            self.db.execute(
                "INSERT INTO batch_torrent_unlocks(plan_id,unlock_job_id,unlock_attempt,verification_job_id,"
                "fingerprint,priorities_json,piece_size,phase,reason_code,start_job_id,start_attempt,"
                "created_at_ms,updated_at_ms) "
                "VALUES(?,51,1,51,?,'{}',16384,'COMPLETE','TORRENT_DOWNLOAD_COMPLETE',51,1,0,0)",
                (plan_id, fp_1))
            self.db.execute(
                "INSERT INTO batch_episode_media(plan_id,file_index,verification_job_id,unlock_job_id,"
                "submission_fingerprint,transfer_fingerprint,evidence_json,verified_at_ms) "
                "VALUES(?,?,51,51,?,?,'{}',0)",
                (plan_id, file_index, fp_2, fp_2))
            self.db.execute(
                "INSERT INTO jobs(job_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms) "
                "VALUES(?, 'STAGE_BATCH_EPISODE', '{}', 'DONE', 0, 0, 0)", (stage_job_id,))
            self.db.execute(
                "INSERT INTO job_operations(operation_id,state,created_at_ms,updated_at_ms) "
                "VALUES(?, 'STAGED', 0, 0)", (operation_id,))
            self.db.execute(
                "INSERT INTO job_operation_members(operation_id,job_id) VALUES(?,?)",
                (operation_id, stage_job_id))
            self.db.execute(
                "INSERT INTO batch_episode_reservations(reservation_id,plan_id,owner_job_id,file_index,"
                "anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,state,reserved_at_ms) "
                "VALUES(1,?,51,?,10,1001,20,1,1,2001,'ACTIVE',0)", (plan_id, file_index))
            self.db.execute(
                "INSERT INTO batch_staging_publications(plan_id,file_index,job_id,verification_job_id,manifest_json,receipts_json,state,created_at_ms,published_at_ms) "
                "VALUES(?,?,?,50,?,?,'PUBLISHED',0,0)",
                (plan_id, file_index, stage_job_id, json.dumps(manifest), json.dumps(receipts)))

    def test_batch_keep_journal_load_signature_and_hash_checked_contract(self):
        """W-01 and W-02: journal accepts resolver keyword and hash_checked=False media."""
        stage_job = 61
        op_id = 7
        self._seed_batch_staging(stage_job_id=stage_job, operation_id=op_id, plan_id=15, file_index=0)

        # Create KEEP_BATCH job
        keep_job = 100
        now_ms = int(time.time() * 1000)
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO jobs(job_id,kind,payload_json,state,lease_token,lease_expires_at_ms,attempt_count,available_at_ms,created_at_ms,updated_at_ms) "
                "VALUES(?, 'KEEP_BATCH', '{}', 'RUNNING', 'token123', ?, 1, 0, 0, ?)",
                (keep_job, now_ms + 60000, now_ms))
            self.db.execute(
                "INSERT INTO management_actions(job_id,target_job_id,operation_id,action,created_at_ms) "
                "VALUES(?, ?, ?, 'KEEP', ?)", (keep_job, stage_job, op_id, now_ms))

        claim = Claim(job_id=keep_job, event_id=None, kind='KEEP_BATCH', payload='{}', attempt=1, token='token123')
        journal = BatchKeepJournal(str(self.database), file_index=0)

        # Call load with resolver keyword argument (as Service._run does)
        resolver_mock = MagicMock()
        phase, data = journal.load(claim, stage_job, self.configuration, resolver=resolver_mock)

        self.assertEqual(phase, 'PLANNED')
        self.assertIn('selection', data)
        self.assertEqual(data['selection']['tvdb_episode'], 1)
        self.assertEqual(data['selection']['staging_root'], str(self.staging_root))

    def test_service_run_with_batch_keep_journal(self):
        """W-01 & W-02 end-to-end: Service._run succeeds with BatchKeepJournal and fake Sonarr."""
        stage_job = 61
        op_id = 7
        self._seed_batch_staging(stage_job_id=stage_job, operation_id=op_id, plan_id=15, file_index=0)

        keep_job = 100
        now_ms = int(time.time() * 1000)
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO jobs(job_id,kind,payload_json,state,lease_token,lease_expires_at_ms,attempt_count,available_at_ms,created_at_ms,updated_at_ms) "
                "VALUES(?, 'KEEP_BATCH', '{}', 'RUNNING', 'token123', ?, 1, 0, 0, ?)",
                (keep_job, now_ms + 60000, now_ms))
            self.db.execute(
                "INSERT INTO management_actions(job_id,target_job_id,operation_id,action,created_at_ms) "
                "VALUES(?, ?, ?, 'KEEP', ?)", (keep_job, stage_job, op_id, now_ms))

        claim = Claim(job_id=keep_job, event_id=None, kind='KEEP_BATCH', payload='{}', attempt=1, token='token123')

        # Fake Sonarr client
        imported_destination = self.keep_root / 'Show' / 'Show S01E01.mkv'
        imported_destination.parent.mkdir(parents=True, exist_ok=True)
        imported_destination.write_bytes(self.staged_media.read_bytes())
        db_path = str(self.database)

        class FakeClient:
            def __init__(self, options, key):
                self.options = options
            def request(self, route, body=None):
                if route == 'system/status':
                    return {'version': '4.0.16.1'}
                if route == 'command':
                    return {'id': 99}
                if route == 'command/99':
                    return {'id': 99, 'status': 'completed'}
                if route == 'episodefile/500':
                    return {'id': 500, 'seriesId': 31, 'size': imported_destination.stat().st_size,
                            'path': str(imported_destination)}
                raise AssertionError(f"Unexpected route: {route}")
            def series(self, tvdb_series_id):
                return {'id': 31, 'path': str(Path(self.options.library_root) / 'Show')}
            def episode(self, selection, series):
                # When checked before import, hasFile is False; after, True
                row = connect(db_path).execute(
                    "SELECT phase FROM batch_keep_imports WHERE job_id=?", (keep_job,)).fetchone()
                imported = row and row[0] == 'IMPORT_UNCERTAIN'
                return {'id': 41, 'hasFile': imported, 'episodeFileId': 500 if imported else 0}
            def import_payload(self, selection, series, episode):
                return {'name': 'ManualImport', 'importMode': 'copy',
                        'files': [{'path': selection.media_path, 'seriesId': 31, 'episodeIds': [41]}]}

        journal_factory = lambda db_path: BatchKeepJournal(db_path, file_index=0)
        service = Service(
            self.configuration,
            self.secrets,
            client_factory=FakeClient,
            journal_factory=journal_factory
        )

        cancelled = threading.Event()
        result = service.run(claim, stage_job, cancelled)
        self.assertTrue(result)

        # Verify batch_keep_imports is COMPLETE
        row = self.db.execute("SELECT phase FROM batch_keep_imports WHERE job_id=?", (keep_job,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'COMPLETE')

    def test_batch_post_keep_cleanup_copy_only(self):
        """W-03: BatchPostKeepCleanup succeeds with copy_only=True and media hash_checked=False."""
        stage_job = 61
        op_id = 7
        self._seed_batch_staging(stage_job_id=stage_job, operation_id=op_id, plan_id=15, file_index=0)

        keep_job = 100
        now_ms = int(time.time() * 1000)
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO jobs(job_id,kind,payload_json,state,lease_token,lease_expires_at_ms,attempt_count,available_at_ms,created_at_ms,updated_at_ms) "
                "VALUES(?, 'KEEP_BATCH', '{}', 'RUNNING', 'token123', ?, 1, 0, 0, ?)",
                (keep_job, now_ms + 60000, now_ms))
            self.db.execute(
                "INSERT INTO management_actions(job_id,target_job_id,operation_id,action,created_at_ms) "
                "VALUES(?, ?, ?, 'KEEP', ?)", (keep_job, stage_job, op_id, now_ms))

        claim = Claim(job_id=keep_job, event_id=None, kind='KEEP_BATCH', payload='{}', attempt=1, token='token123')
        journal = BatchKeepJournal(str(self.database), file_index=0)
        journal.load(claim, stage_job, self.configuration)

        # Complete the keep import
        imported_destination = self.keep_root / 'Show' / 'Show S01E01.mkv'
        imported_destination.parent.mkdir(parents=True, exist_ok=True)
        imported_destination.write_bytes(self.staged_media.read_bytes())

        data = json.loads(self.db.execute(
            "SELECT data_json FROM batch_keep_imports WHERE job_id=?", (keep_job,)).fetchone()[0])
        data['series_id'] = 31
        data['series_path'] = str(self.keep_root / 'Show')
        data['episode_id'] = 41
        data['result'] = {'episode_file_id': 500, 'relative_path': 'Show/Show S01E01.mkv',
                          'size': self.staged_media.stat().st_size}

        with transaction(self.db):
            self.db.execute(
                "UPDATE batch_keep_imports SET phase='COMPLETE', data_json=? WHERE job_id=?",
                (json.dumps(data), keep_job))
            self.db.execute(
                "UPDATE jobs SET state='DONE' WHERE job_id=?", (keep_job,))

        # Now test BatchPostKeepCleanup
        cleanup_job = 200
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO jobs(job_id,kind,payload_json,state,lease_token,lease_expires_at_ms,attempt_count,created_by_job_id,available_at_ms,created_at_ms,updated_at_ms) "
                "VALUES(?, 'CLEANUP_BATCH_KEPT_MEDIA', '{}', 'RUNNING', 'token456', ?, 1, ?, 0, 0, ?)",
                (cleanup_job, now_ms + 60000, keep_job, now_ms))

        cleanup_claim = Claim(job_id=cleanup_job, event_id=None, kind='CLEANUP_BATCH_KEPT_MEDIA', payload='{}', attempt=1, token='token456')

        class FakeSonarrCleanupClient:
            def series(self, tvdb_series_id):
                return {'id': 31, 'path': str(imported_destination.parent)}
            def episode(self, selection, series):
                return {'id': 41, 'hasFile': True, 'episodeFileId': 500}
            def request(self, route):
                if route == 'episodefile/500':
                    return {'id': 500, 'seriesId': 31, 'size': imported_destination.stat().st_size,
                            'path': str(imported_destination)}
                raise AssertionError(route)

        unlinked = []
        def fake_remove(manifest, receipts, guard, keep_root):
            for r in json.loads(receipts):
                unlinked.append(r['path'])

        cleanup_service = BatchPostKeepCleanup(
            self.configuration,
            self.secrets,
            client_factory=lambda opt, key: FakeSonarrCleanupClient(),
            remove=fake_remove
        )

        cleanup_service.run(cleanup_claim, keep_job, threading.Event())
        self.assertIn('Show S01E01.mkv', unlinked)


if __name__ == '__main__':
    unittest.main()
