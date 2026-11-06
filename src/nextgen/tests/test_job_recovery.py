"""Tests for historical job recovery, diagnostic classification, and CLI."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.nextgen.api.adm import main as adm_main
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationSnapshot
from src.nextgen.job_recovery import (
    build_summary,
    inspect_stopped_jobs,
    list_jobs,
    preview_recovery,
    apply_recovery,
    create_backup,
    main as recovery_main,
)
from src.nextgen.storage.sqlite import connect, initialize, enqueue_job_with_receipt, transaction


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.database_path = self.config_dir / 'anime.sqlite'
        
        # Initialize sqlite DB
        db = connect(self.database_path)
        initialize(db)
        db.close()

        # Initialize config
        snapshot = ConfigurationSnapshot(1, {
            'storage': {'database_path': str(self.database_path)},
        })
        ConfigurationStore(str(self.config_dir)).initialize(snapshot.to_document()['settings'])

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_stopped_job(self, kind, reason, state='NEEDS_REVIEW', attempt=1, payload=None, failed_check=None):
        payload = payload or {'plan_id': 1}
        db = connect(self.database_path)
        with transaction(db):
            job_id, audit = enqueue_job_with_receipt(db, kind=kind, payload=payload, component='api')
            db.execute(
                "UPDATE jobs SET state=?, last_reason_code=?, attempt_count=? WHERE job_id=?",
                (state, reason, attempt, job_id),
            )
            if failed_check:
                from src.nextgen.history import append_job_history
                append_job_history(
                    db,
                    job_id=job_id,
                    event_code='NEEDS_REVIEW',
                    reason_code=reason,
                    component='worker',
                    occurred_at_ms=1000,
                    details={'attempt': attempt, 'failed_check': failed_check},
                )
        db.close()
        return job_id

    def test_inspect_and_summary_aggregates_stopped_jobs(self):
        # 2 auto-retriable jobs (TORRENT_METADATA_TIMEOUT, ACQUISITION_SIZE_LIMIT)
        j1 = self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT', failed_check='src/nextgen/acquisition/metadata.py:100 check')
        j2 = self._create_stopped_job('EVALUATE_BATCH', 'ACQUISITION_SIZE_LIMIT')
        # 1 manual-action job (IDENTITY_INCOMPLETE)
        j3 = self._create_stopped_job('IDENTIFY_ANIME', 'IDENTITY_INCOMPLETE')

        db = connect(self.database_path)
        try:
            jobs = inspect_stopped_jobs(db)
            self.assertEqual(len(jobs), 3)
            
            job1 = next(j for j in jobs if j['job_id'] == j1)
            self.assertTrue(job1['eligible'])
            self.assertEqual(job1['category'], 'AUTONOMOUS_RETRY')
            self.assertEqual(job1['failed_check'], 'src/nextgen/acquisition/metadata.py:100 check')

            job3 = next(j for j in jobs if j['job_id'] == j3)
            self.assertFalse(job3['eligible'])
            self.assertEqual(job3['category'], 'MANUAL_ACTION')

            summary = build_summary(db)
            self.assertEqual(len(summary), 3)
            summary_dict = {s['reason_code']: s for s in summary}
            self.assertEqual(summary_dict['TORRENT_METADATA_TIMEOUT']['eligible'], 1)
            self.assertEqual(summary_dict['ACQUISITION_SIZE_LIMIT']['eligible'], 1)
            self.assertEqual(summary_dict['IDENTITY_INCOMPLETE']['eligible'], 0)
            self.assertEqual(summary_dict['IDENTITY_INCOMPLETE']['manual'], 1)
        finally:
            db.close()

    def test_list_jobs_filtering(self):
        self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT')
        self._create_stopped_job('EVALUATE_BATCH', 'ACQUISITION_SIZE_LIMIT')
        self._create_stopped_job('IDENTIFY_ANIME', 'IDENTITY_INCOMPLETE')

        db = connect(self.database_path)
        try:
            eligible_only = list_jobs(db, eligible_only=True)
            self.assertEqual(len(eligible_only), 2)
            self.assertTrue(all(j['eligible'] for j in eligible_only))

            manual_only = list_jobs(db, manual_only=True)
            self.assertEqual(len(manual_only), 1)
            self.assertEqual(manual_only[0]['kind'], 'IDENTIFY_ANIME')

            by_kind = list_jobs(db, kind='PREPARE_ACQUISITION')
            self.assertEqual(len(by_kind), 1)
            self.assertEqual(by_kind[0]['reason_code'], 'TORRENT_METADATA_TIMEOUT')
        finally:
            db.close()

    def test_preview_recovery_does_not_mutate(self):
        j1 = self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT')
        db = connect(self.database_path)
        try:
            previews = preview_recovery(db, all_eligible=True)
            self.assertEqual(len(previews), 1)
            self.assertEqual(previews[0]['job_id'], j1)
            self.assertEqual(previews[0]['would_transition'], 'PENDING')

            # Ensure DB was not mutated
            row = db.execute("SELECT state, attempt_count, last_reason_code FROM jobs WHERE job_id=?", (j1,)).fetchone()
            self.assertEqual(row[0], 'NEEDS_REVIEW')
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2], 'TORRENT_METADATA_TIMEOUT')
        finally:
            db.close()

    def test_create_backup_generates_valid_sqlite_copy(self):
        self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT')
        backup_file = create_backup(self.database_path, dest_dir=self.config_dir)
        self.assertTrue(os.path.exists(backup_file))
        
        # Verify content in backup
        backup_db = sqlite3.connect(backup_file)
        rows = backup_db.execute("SELECT count(*) FROM jobs").fetchone()
        self.assertEqual(rows[0], 1)
        backup_db.close()

    def test_apply_recovery_requeues_eligible_and_preserves_attempts(self):
        j1 = self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT', attempt=2)
        j2 = self._create_stopped_job('IDENTIFY_ANIME', 'IDENTITY_INCOMPLETE', attempt=1)

        result = apply_recovery(str(self.config_dir), all_eligible=True, backup=True)
        self.assertEqual(len(result['recovered']), 1)
        self.assertEqual(result['recovered'][0]['job_id'], j1)
        self.assertEqual(result['recovered'][0]['attempt'], 2)
        self.assertIsNotNone(result['backup_path'])
        self.assertTrue(os.path.exists(result['backup_path']))

        # Verify DB state
        db = connect(self.database_path)
        try:
            row1 = db.execute("SELECT state, attempt_count, last_reason_code FROM jobs WHERE job_id=?", (j1,)).fetchone()
            self.assertEqual(row1[0], 'PENDING')
            self.assertEqual(row1[1], 2)  # Attempt preserved!
            self.assertIsNone(row1[2])   # Reason cleared on PENDING

            # Ineligible job untouched
            row2 = db.execute("SELECT state, attempt_count, last_reason_code FROM jobs WHERE job_id=?", (j2,)).fetchone()
            self.assertEqual(row2[0], 'NEEDS_REVIEW')
            self.assertEqual(row2[1], 1)
            self.assertEqual(row2[2], 'IDENTITY_INCOMPLETE')

            # History audit event recorded
            events = db.execute("SELECT event_code FROM job_history WHERE job_id=? ORDER BY history_id DESC", (j1,)).fetchall()
            self.assertIn('RETRY_REQUESTED', [e[0] for e in events])
        finally:
            db.close()

    def test_cli_subcommands(self):
        j1 = self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT', attempt=1)

        # 1. Summary CLI
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            ret = recovery_main(['--config-dir', str(self.config_dir), 'summary'])
            self.assertEqual(ret, 0)
        self.assertIn('TORRENT_METADATA_TIMEOUT', stdout.getvalue())

        # 2. Summary JSON CLI
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            ret = recovery_main(['--config-dir', str(self.config_dir), 'summary', '--json'])
            self.assertEqual(ret, 0)
        parsed = json.loads(stdout.getvalue())
        self.assertIn('summary', parsed)

        # 3. List CLI
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            ret = recovery_main(['--config-dir', str(self.config_dir), 'list'])
            self.assertEqual(ret, 0)
        self.assertIn(str(j1), stdout.getvalue())

        # 4. Preview CLI
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            ret = recovery_main(['--config-dir', str(self.config_dir), 'preview', '--all-eligible'])
            self.assertEqual(ret, 0)
        self.assertIn('Dry-Run Preview', stdout.getvalue())

        # 5. The apply subcommand executes without redundant --apply.
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            ret = recovery_main(['--config-dir', str(self.config_dir), 'apply', '--all-eligible'])
            self.assertEqual(ret, 0)
        self.assertIn('Successfully Requeued Jobs', stdout.getvalue())

        # 6. The removed --apply flag is rejected before anything is changed.
        old_style = self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT')
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as rejected:
            recovery_main(['--config-dir', str(self.config_dir),
                           'apply', '--job-id', str(old_style), '--apply'])
        self.assertEqual(rejected.exception.code, 2)
        db = connect(self.database_path)
        try:
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE job_id=?',
                                        (old_style,)).fetchone()[0], 'NEEDS_REVIEW')
        finally:
            db.close()

    def test_adm_entrypoint_routes_to_recover(self):
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch.object(
            sys, 'argv', ['adm', 'recover', '--config-dir', str(self.config_dir), 'summary']
        ), self.assertRaises(SystemExit) as exit_err:
            adm_main()
        self.assertEqual(exit_err.exception.code, 0)
        self.assertIn('Stopped Jobs Recovery Summary', stdout.getvalue())

    def test_apply_recovery_single_job_id_and_by_reason(self):
        j1 = self._create_stopped_job('EVALUATE_BATCH', 'ACQUISITION_SIZE_LIMIT', attempt=1)
        j2 = self._create_stopped_job('TRANSFER_BATCH', 'TORRENT_PIECE_OVERLAP', attempt=1)

        # Recover by reason
        result = apply_recovery(str(self.config_dir), reason='ACQUISITION_SIZE_LIMIT', backup=False)
        self.assertEqual(len(result['recovered']), 1)
        self.assertEqual(result['recovered'][0]['job_id'], j1)

        # Recover single job id
        result2 = apply_recovery(str(self.config_dir), job_id=j2, backup=False)
        self.assertEqual(len(result2['recovered']), 1)
        self.assertEqual(result2['recovered'][0]['job_id'], j2)


    def test_explicit_reason_can_retry_manual_classification_with_safety_gate(self):
        reason = 'SERVICE_SETUP_INVALID'
        retryable = self._create_stopped_job('PREPARE_ACQUISITION', reason, attempt=2)
        held = self._create_stopped_job('PREPARE_ACQUISITION', reason, state='ON_HOLD')
        automatic = self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT')

        db = connect(self.database_path)
        try:
            jobs = {job['job_id']: job for job in inspect_stopped_jobs(db)}
            self.assertEqual(jobs[retryable]['category'], 'MANUAL_ACTION')
            self.assertFalse(jobs[retryable]['eligible'])
            self.assertTrue(jobs[retryable]['retryable'])

            # Untargeted bulk recovery keeps the conservative reason matrix.
            self.assertEqual([item['job_id'] for item in preview_recovery(db, all_eligible=True)],
                             [automatic])

            # Explicit --reason selects retryable manual-classified jobs, but does
            # not override the underlying state/lease/operation safety checks.
            preview = preview_recovery(db, reason=reason)
            self.assertEqual({item['job_id']: item['eligible'] for item in preview},
                             {retryable: True, held: False})
        finally:
            db.close()

        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            result = recovery_main([
                '--config-dir', str(self.config_dir),
                'apply', '--reason', reason, '--no-backup',
            ])
        self.assertEqual(result, 0)
        self.assertIn('Successfully Requeued Jobs', stdout.getvalue())

        db = connect(self.database_path)
        try:
            rows = dict(db.execute('SELECT job_id, state FROM jobs'))
            self.assertEqual(rows[retryable], 'PENDING')
            self.assertEqual(rows[held], 'ON_HOLD')
            self.assertEqual(rows[automatic], 'NEEDS_REVIEW')
            history = db.execute(
                "SELECT event_code FROM job_history WHERE job_id=? ORDER BY history_id DESC LIMIT 1",
                (retryable,),
            ).fetchone()
            self.assertEqual(history[0], 'RETRY_REQUESTED')
        finally:
            db.close()

    def test_apply_recovery_cas_mismatch_records_failure_cleanly(self):
        j1 = self._create_stopped_job('PREPARE_ACQUISITION', 'TORRENT_METADATA_TIMEOUT', attempt=1)
        
        # Concurrently change attempt in DB before apply executes
        db = connect(self.database_path)
        with transaction(db):
            db.execute("UPDATE jobs SET attempt_count=5 WHERE job_id=?", (j1,))
        db.close()

        # Try to apply recovery expecting attempt 1 (as observed during inspection)
        # We simulate the preview inspecting attempt 1 then concurrently modified:
        with patch('src.nextgen.job_recovery.preview_recovery') as mock_preview:
            mock_preview.return_value = [{
                'job_id': j1,
                'kind': 'PREPARE_ACQUISITION',
                'state': 'NEEDS_REVIEW',
                'attempt': 1,
                'reason': 'TORRENT_METADATA_TIMEOUT',
                'eligible': True,
                'recommendation': 'retry',
                'failed_check': None,
                'would_transition': 'PENDING',
                'disabled_reason': None,
            }]
            result = apply_recovery(str(self.config_dir), job_id=j1, backup=False)
            self.assertEqual(len(result['recovered']), 0)
            self.assertEqual(len(result['failed']), 1)
            self.assertEqual(result['failed'][0]['job_id'], j1)
            self.assertIn('job attempt changed', result['failed'][0]['error'])
