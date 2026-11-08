"""Offline versioned runner tests. Never use production or legacy databases."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.anime.maintenance import MaintenanceGate, MaintenanceError
from src.anime.storage import migrations
from src.anime.storage.sqlite import admitted_connection, initialize


def historical(path, version):
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        for name in migrations.SCRIPTS[:version]:
            db.executescript((migrations.SQL_DIR / name).read_text())
    finally:
        db.close()


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "anime.sqlite"
        self.gate = MaintenanceGate(self.path)

    def run_upgrade(self, plan):
        ticket = self.gate.request()
        with self.gate.exclusive(ticket) as lease:
            return migrations.apply(self.path, plan, lease=lease)

    def test_all_supported_versions_reach_fresh_schema_and_current_is_noop(self):
        for version in range(1, migrations.LATEST_VERSION+1):
            with self.subTest(version=version):
                path = self.path.with_name(str(version) + ".sqlite")
                historical(path, version)
                gate = MaintenanceGate(path)
                with gate.operation():
                    plan = migrations.preview(path)
                ticket = gate.request()
                with gate.exclusive(ticket) as lease:
                    result = migrations.apply(path, plan, lease=lease)
                self.assertEqual(result.applied_versions, tuple(range(version+1, migrations.LATEST_VERSION+1)))
                self.assertEqual(gate.status(), ticket)  # No implicit resume.
                upgraded = sqlite3.connect(path)
                fresh = sqlite3.connect(":memory:")
                try:
                    initialize(fresh)
                    self.assertEqual(migrations._structure(upgraded), migrations._structure(fresh))
                    self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                    if version < migrations.LATEST_VERSION:
                        backup = sqlite3.connect(path.with_name(result.backup_name))
                        try:
                            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], version)
                        finally:
                            backup.close()
                    else:
                        self.assertIsNone(result.backup_name)
                finally:
                    upgraded.close()
                    fresh.close()

    def test_existing_rows_ids_history_and_provenance_preserved_with_wal_backup(self):
        historical(self.path, 3)
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
              INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(7,'test',X'6162',1);
              INSERT INTO identified_anime VALUES(1,2,'Title');
              INSERT INTO identified_episodes VALUES(3,1);
              INSERT INTO identity_results VALUES(4,7,1,2,'{}',1);
              INSERT INTO result_episode_links VALUES(4,1,3);
              INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms)
                VALUES(8,7,'identify','{}',1,1,1);
              INSERT INTO jobs(job_id,event_id,created_by_job_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms)
                VALUES(9,7,8,'follow','{}',1,1,1);
              INSERT INTO event_history VALUES(10,7,1,'HISTORICAL','OLD_CODE','old detail');
              INSERT INTO job_history VALUES(11,8,1,'HISTORICAL','OLD_CODE','{}');
            """)
            result = self.run_upgrade(migrations.preview(self.path))
            self.assertEqual(db.execute("SELECT created_by_job_id FROM jobs WHERE job_id=9").fetchone(), (8,))
            self.assertEqual(db.execute("SELECT raw_body FROM incoming_events WHERE event_id=7").fetchone(), (b"ab",))
            self.assertEqual(db.execute("SELECT * FROM result_episode_links").fetchall(), [(4,1,3)])
            self.assertEqual(db.execute("SELECT reason_code,component FROM event_history WHERE history_id=10").fetchone(), ("OLD_CODE",None))
            self.assertEqual(db.execute("SELECT reason_code,component,activity_code FROM job_history WHERE history_id=11").fetchone(), ("OLD_CODE",None,None))
            backup = sqlite3.connect(self.path.with_name(result.backup_name))
            try:
                self.assertEqual(backup.execute("SELECT raw_body FROM incoming_events").fetchone(), (b"ab",))
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 3)
            finally:
                backup.close()
            self.assertEqual(self.path.with_name(result.backup_name).stat().st_mode & 0o777, 0o600)
        finally:
            db.close()

    def test_preview_missing_unknown_unversioned_or_drifted_database_refused(self):
        with self.assertRaises(migrations.MigrationError):
            migrations.preview(self.path)
        self.assertFalse(self.path.exists())
        for version in (0, migrations.LATEST_VERSION+1, 99):
            db = sqlite3.connect(self.path)
            db.execute("PRAGMA user_version=" + str(version))
            db.close()
            with self.assertRaises(migrations.MigrationUnsupported):
                migrations.preview(self.path)
            self.path.unlink()
        historical(self.path, 3)
        db = sqlite3.connect(self.path)
        db.execute("CREATE INDEX unexpected ON incoming_events(source)")
        db.close()
        with self.assertRaises(migrations.MigrationSchemaInvalid):
            migrations.preview(self.path)

    def test_plan_conflict_wrong_or_expired_lease_prevents_backup_and_changes(self):
        historical(self.path, 3)
        plan = migrations.preview(self.path)
        with self.assertRaises(MaintenanceError):
            migrations.apply(self.path, plan, lease=None)
        ticket = self.gate.request()
        with self.gate.exclusive(ticket) as lease:
            with self.assertRaises(migrations.MigrationConflict):
                migrations.apply(self.path, replace(plan, schema_fingerprint="changed"), lease=lease)
            with self.assertRaises(MaintenanceError):
                migrations.apply(self.path.with_name("other"), plan, lease=lease)
        with self.assertRaises(MaintenanceError):
            migrations.apply(self.path, plan, lease=lease)
        self.assertEqual(list(self.path.parent.glob("*.before-*.sqlite")), [])

    def test_precommit_validation_failure_rolls_back_step_but_keeps_earlier_commit(self):
        historical(self.path, 3)
        plan = migrations.preview(self.path)
        validate = migrations._validate
        def fail_five(db, version, scripts):
            if version == 5:
                raise migrations.MigrationSchemaInvalid()
            return validate(db, version, scripts)
        with patch.object(migrations, "_validate", side_effect=fail_five):
            with self.assertRaises(migrations.MigrationSchemaInvalid):
                self.run_upgrade(plan)
        self.assertIsNotNone(self.gate.status())
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertNotIn("activity_code", [row[1] for row in db.execute("PRAGMA table_info(jobs)")])
        finally:
            db.close()
        # Explicit re-preview and recovery can continue from the committed version.
        result = self.run_upgrade(migrations.preview(self.path))
        self.assertEqual(result.applied_versions, tuple(range(5,migrations.LATEST_VERSION+1)))

    def test_backup_failure_never_applies_a_migration(self):
        historical(self.path, 3)
        plan = migrations.preview(self.path)
        with patch.object(migrations, "_backup", side_effect=OSError("secret path")):
            with self.assertRaises(migrations.MigrationError) as raised:
                self.run_upgrade(plan)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(migrations.preview(self.path).from_version, 3)
        self.assertIsNotNone(self.gate.status())

    def test_admitted_runtime_connection_requires_current_version_and_open_gate(self):
        historical(self.path, 3)
        with self.assertRaises(migrations.MigrationUnsupported):
            with admitted_connection(self.gate):
                self.fail("old schema")
        self.run_upgrade(migrations.preview(self.path))
        ticket = self.gate.status()
        with self.gate.exclusive(ticket) as lease:
            lease.complete()  # Simulates management compatibility/readiness checks.
        with admitted_connection(self.gate) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], migrations.LATEST_VERSION)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_bad_foreign_keys_are_refused(self):
        historical(self.path, 3)
        db = sqlite3.connect(self.path)
        db.execute("INSERT INTO event_history VALUES(1,999,1,'OLD',NULL,NULL)")
        db.commit()
        db.close()
        with self.assertRaises(migrations.MigrationSchemaInvalid):
            migrations.preview(self.path)

    def test_step_error_restores_foreign_key_enforcement_and_transaction_state(self):
        historical(self.path, 4)
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            scripts = migrations._scripts()
            broken = scripts[4][1].replace("PRAGMA user_version=5;", "SELECT * FROM missing_table;")
            with self.assertRaises(sqlite3.Error):
                migrations._apply_step(db, broken, 5, scripts)
            self.assertFalse(db.in_transaction)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
        finally:
            db.close()

    def test_manifest_change_after_preview_is_a_conflict(self):
        historical(self.path, 3)
        plan = migrations.preview(self.path)
        scripts = migrations._scripts()
        modified = scripts[:3] + ((scripts[3][0], scripts[3][1] + "\n-- updated package\n"),) + scripts[4:]
        with patch.object(migrations, "_scripts", return_value=modified):
            with self.assertRaises(migrations.MigrationConflict):
                self.run_upgrade(plan)
        self.assertEqual(list(self.path.parent.glob("*.before-*.sqlite")), [])

    def test_database_symlink_is_not_followed(self):
        target = self.path.with_name("target.sqlite")
        historical(target, 3)
        self.path.symlink_to(target)
        with self.assertRaises(migrations.MigrationSchemaInvalid):
            migrations.preview(self.path)

    def test_modified_check_constraint_is_detected_even_when_columns_match(self):
        historical(self.path, 3)
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA writable_schema=ON")
            db.execute("UPDATE sqlite_master SET sql=replace(sql, 'attempt_count >= 0', 'attempt_count >= -1') WHERE name='jobs'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(migrations.MigrationSchemaInvalid):
            migrations.preview(self.path)
