"""Offline local setup, explicit maintenance and worker dispatch; unrun."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from src.anime.__main__ import initialize_local, check_local, database_preview, upgrade_database, plan_token, main
from src.anime.configuration import ConfigurationStore
from src.anime.maintenance import MaintenanceGate, MaintenancePending
from src.anime.secrets import SecretStore
from src.anime.storage.migrations import MigrationPlan
from src.anime.storage.migrations import LATEST_VERSION
from src.anime.storage.sqlite import connect


class ServiceSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.database=self.root/'queue.sqlite'

    def initialize(self):
        return initialize_local(self.root,self.database,{'anidb':{'client_name':'registeredclient'}})

    def access(self):
        SecretStore(self.root).update_webhook_token('w'*43,expected_revision=1)

    def test_explicit_init_creates_current_local_files_without_external_work(self):
        result=self.initialize();self.assertTrue(result['initialized']);self.assertEqual(result['configuration_version'],17)
        self.assertEqual(ConfigurationStore(self.root).read().value('storage.database_path'),str(self.database))
        self.assertEqual(self.database.stat().st_mode&0o777,0o600)
        db=connect(self.database)
        try:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],LATEST_VERSION)
        finally:db.close()
        # API readiness must not require either a webhook token or a password.
        self.assertFalse(check_local(self.root)['external_services_checked'])
        self.access();self.assertFalse(check_local(self.root)['external_services_checked'])

    def test_existing_database_or_config_is_not_overwritten(self):
        self.database.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):self.initialize()
        self.assertEqual(self.database.read_bytes(),b'existing')
        self.database.unlink();self.initialize();before=self.database.read_bytes()
        with self.assertRaises(ValueError):self.initialize()
        self.assertEqual(self.database.read_bytes(),before)

    def test_invalid_settings_create_no_database(self):
        with self.assertRaises(ValueError):initialize_local(self.root,self.database,{'unknown':True})
        self.assertFalse(self.database.exists())

    def test_pending_maintenance_is_not_reported_ready_and_preview_does_not_resume(self):
        self.initialize();self.access();gate=MaintenanceGate(self.database);ticket=gate.request()
        with self.assertRaises(MaintenancePending):check_local(self.root)
        with database_preview(self.root) as (_,plan):self.assertEqual(plan.from_version,LATEST_VERSION)
        self.assertEqual(gate.status(),ticket)

    def test_plan_token_pins_scripts_and_source_version(self):
        plan=MigrationPlan(22,23,'a'*64,((23,'b'*64),))
        self.assertNotEqual(plan_token(plan),plan_token(replace(plan,from_version=23)))
        self.assertNotEqual(plan_token(plan),plan_token(replace(plan,scripts=((23,'c'*64),))))

    def test_wrong_upgrade_token_does_not_request_maintenance(self):
        self.initialize()
        with self.assertRaises(ValueError):upgrade_database(self.root,'wrong')
        self.assertIsNone(MaintenanceGate(self.database).status())

    def test_upgrade_failure_retains_pending_marker(self):
        self.initialize()
        with database_preview(self.root) as (_,plan):token=plan_token(plan)
        with patch('src.anime.__main__.migrations.apply',side_effect=OSError('offline')):
            with self.assertRaises(OSError):upgrade_database(self.root,token)
        self.assertIsNotNone(MaintenanceGate(self.database).status())

    def test_worker_dispatches_composed_worker_without_starting_api(self):
        run=AsyncMock()
        with patch('src.anime.__main__.check_local') as check,patch('src.anime.providers.configuration.run_identity_worker',run):
            self.assertEqual(main(['worker','--config-dir',str(self.root)]),0)
        check.assert_called_once_with(str(self.root),worker=True);run.assert_awaited_once_with(str(self.root),backfill=True,backfill_select=True,backfill_acquire=True)

    def test_check_command_does_not_start_worker(self):
        with patch('src.anime.__main__.check_local',return_value={'local_ready':True}) as check,redirect_stdout(io.StringIO()):
            self.assertEqual(main(['check','--config-dir',str(self.root)]),0)
        check.assert_called_once_with(str(self.root),worker=False)
