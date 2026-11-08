"""Each installed branch keeps its own settings folder and database; a first switch may copy the data."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.anime.configuration import ConfigurationStore
from src.anime.install_support import (
    BranchSetupRefused, branch_setup, copy_database, migration_lineage)
from src.anime.storage.migrations import LATEST_VERSION, SQL_DIR
from src.anime.storage.sqlite import connect, initialize
from src.anime.tests import test_installer as base

installer = base.installer


def make_branch(folder, database):
    """A configured branch: current settings and an initialized database with one row."""
    folder.mkdir(parents=True, mode=0o700)
    ConfigurationStore(folder).initialize({'storage': {'database_path': str(database)},
                                           'acquisition': {'max_episode_mb': 777}})
    (folder / 'secrets.json').write_text('{"secret": 1}')
    (folder / 'management-password.json').write_text('{"password": 1}')
    db = connect(database)
    try:
        initialize(db)
        db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('manual-release',X'7b7d',1)")
        db.commit()
    finally:
        db.close()


class MigrationLineageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.sql = Path(temp.name) / 'sql'
        shutil.copytree(SQL_DIR, self.sql)

    def test_identical_migrations_up_to_the_database_version_are_accepted(self):
        self.assertEqual(migration_lineage(self.sql, LATEST_VERSION), LATEST_VERSION)
        self.assertEqual(migration_lineage(self.sql, 10), 10)

    def test_a_different_migration_at_or_below_the_version_is_refused_and_named(self):
        changed = sorted(self.sql.glob('*.sql'))[9]
        changed.write_text(changed.read_text() + '\n-- a different branch\n')
        self.assertEqual(migration_lineage(self.sql, 9), 9)          # Below the change: same history.
        with self.assertRaisesRegex(BranchSetupRefused, 'differ at migration 10'):
            migration_lineage(self.sql, 10)

    def test_a_same_numbered_but_different_migration_is_refused(self):
        # "Both branches reached v65" proves nothing: the file itself must match.
        last = sorted(self.sql.glob('*.sql'))[-1]
        last.rename(last.with_name(last.name[:4] + 'something_else.sql'))
        with self.assertRaisesRegex(BranchSetupRefused, 'differ'):
            migration_lineage(self.sql, LATEST_VERSION)

    def test_a_newer_database_than_this_branch_knows_is_refused(self):
        with self.assertRaisesRegex(BranchSetupRefused, 'newer than this branch'):
            migration_lineage(self.sql, LATEST_VERSION + 1)


class BranchSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.source = self.folder / 'config'
        self.source_db = self.folder / 'state' / 'anime-discovery.sqlite'
        self.source_db.parent.mkdir()
        make_branch(self.source, self.source_db)
        self.target = self.folder / 'config' / 'branches' / 'refactor'
        self.target.mkdir(parents=True, mode=0o700)
        self.target_db = self.folder / 'state' / 'anime-discovery-refactor.sqlite'

    def test_a_new_branch_gets_converted_settings_credentials_and_an_empty_database(self):
        result = branch_setup(self.target, self.source, self.target_db, SQL_DIR, False)
        self.assertEqual(result, {'database': str(self.target_db), 'copied_from_version': None})
        snapshot = ConfigurationStore(self.target).read()
        self.assertEqual(snapshot.value('storage.database_path'), str(self.target_db))
        self.assertEqual(snapshot.value('acquisition.max_episode_mb'), 777)       # Settings carried over.
        for name in ('secrets.json', 'management-password.json'):
            self.assertEqual((self.target / name).read_text(), (self.source / name).read_text())
            self.assertEqual((self.target / name).stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.target_db.exists())                                  # prepare creates it.
        self.assertEqual(ConfigurationStore(self.source).read().value('storage.database_path'),
                         str(self.source_db))                                     # Source untouched.

    def test_copy_db_data_copies_the_rows_when_the_histories_match(self):
        result = branch_setup(self.target, self.source, self.target_db, SQL_DIR, True)
        self.assertEqual(result['copied_from_version'], LATEST_VERSION)
        db = sqlite3.connect(self.target_db)
        try:
            self.assertEqual(db.execute('SELECT source FROM incoming_events').fetchall(), [('manual-release',)])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], LATEST_VERSION)
        finally:
            db.close()
        self.assertEqual(self.target_db.stat().st_mode & 0o777, 0o600)

    def test_different_histories_copy_nothing(self):
        sql = self.folder / 'other-sql'
        shutil.copytree(SQL_DIR, sql)
        first = sorted(sql.glob('*.sql'))[0]
        first.write_text(first.read_text() + '\n-- other branch\n')
        with self.assertRaisesRegex(BranchSetupRefused, 'differ at migration 1 '):
            branch_setup(self.target, self.source, self.target_db, sql, True)
        self.assertFalse(self.target_db.exists())
        self.assertFalse((self.target / 'settings.json').exists())

    def test_settings_this_branch_cannot_read_are_refused(self):
        document = json.loads((self.source / 'settings.json').read_text())
        document['schema_version'] += 1                                           # A newer branch's settings.
        (self.source / 'settings.json').write_text(json.dumps(document))
        with self.assertRaisesRegex(BranchSetupRefused, 'cannot read the current settings'):
            branch_setup(self.target, self.source, self.target_db, SQL_DIR, False)

    def test_an_existing_target_database_is_never_overwritten(self):
        self.target_db.write_bytes(b'keep me')
        with self.assertRaises(FileExistsError):
            copy_database(self.source_db, self.target_db, SQL_DIR)
        self.assertEqual(self.target_db.read_bytes(), b'keep me')


class BranchResolveTests(base.InstallerSupportTests):
    for _name in [n for n in dir(base.InstallerSupportTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def saved(self, **extra):
        self.config.joinpath('settings.json').write_text(json.dumps({
            'schema_version': 10, 'settings': {'storage': {'database_path': str(self.database)}}}))
        return {'root': str(self.root / 'app'), 'config_dir': str(self.config), 'state_dir': str(self.root / 'state'),
                'unit_dir': str(self.root / 'units'), 'ref': 'NextGen', 'origin': 'https://anime.example.test', **extra}

    def resolve(self, *flags, saved=None):
        with patch.object(installer, 'saved_defaults', return_value=saved or self.saved()):
            return installer.resolve(installer.parser().parse_args(list(flags)))

    def test_a_first_switch_gets_its_own_folder_and_database(self):
        options = self.resolve('--upgrade', '--branch', 'refactor')
        self.assertEqual(options['switch'], 'new')
        self.assertEqual(options['config_dir'], str(self.config / 'branches' / 'refactor'))
        self.assertEqual(options['database'], str(self.root / 'state' / 'anime-discovery-refactor.sqlite'))
        self.assertEqual(options['switch_from'], {'ref': 'NextGen', 'config_dir': str(self.config)})
        self.assertEqual(options['branches']['NextGen'], {'config_dir': str(self.config)})   # Old branch kept.

    def test_branch_names_become_safe_folder_names(self):
        options = self.resolve('--upgrade', '--branch', 'feature/new-thing')
        self.assertTrue(options['config_dir'].endswith('/branches/feature-new-thing'))
        self.assertTrue(options['database'].endswith('/anime-discovery-feature-new-thing.sqlite'))

    def test_switching_back_reuses_that_branchs_own_state(self):
        saved = self.saved(branches={'NextGen': {'config_dir': str(self.config)}}, config_base=str(self.config))
        saved.update({'ref': 'refactor', 'config_dir': str(self.config)})
        options = self.resolve('--upgrade', '--branch', 'NextGen', saved=saved)
        self.assertEqual((options['switch'], options['config_dir']), ('existing', str(self.config)))

    def test_copy_db_data_needs_a_first_switch(self):
        with self.assertRaisesRegex(ValueError, 'only applies when'):
            self.resolve('--upgrade', '--copy-db-data')
        saved = self.saved(branches={'refactor': {'config_dir': str(self.config)}})
        with self.assertRaisesRegex(ValueError, 'already set up'):
            self.resolve('--upgrade', '--branch', 'refactor', '--copy-db-data', saved=saved)

    def test_leftovers_of_an_interrupted_switch_are_never_reused(self):
        (self.config / 'branches' / 'refactor').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.resolve('--upgrade', '--branch', 'refactor')

    def test_an_upgrade_without_branch_change_keeps_everything(self):
        options = self.resolve('--upgrade')
        self.assertIsNone(options['switch'])
        self.assertEqual(options['config_dir'], str(self.config))


class BranchSwitchApplyTests(base.InstallerOrchestrationTests):
    for _name in [n for n in dir(base.InstallerOrchestrationTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def switch_options(self):
        self.config.mkdir(parents=True)
        (self.config / 'settings.json').write_text('{}')
        previous = self.root / 'releases' / 'old'
        (previous / 'src/anime/storage/sql').mkdir(parents=True)
        self.root.joinpath('current').symlink_to(previous)
        target = self.state / 'config' / 'branches' / 'refactor'
        self.options.update({'ref': 'refactor', 'switch': 'new', 'config_dir': str(target),
                             'switch_from': {'ref': 'NextGen', 'config_dir': str(self.config)},
                             'branches': {'NextGen': {'config_dir': str(self.config)}},
                             'config_base': str(self.config),
                             'database': str(self.state / 'anime-discovery-refactor.sqlite')})
        self.args.copy_db_data = True
        return target, previous

    def test_a_first_switch_sets_up_the_branch_and_remembers_both(self):
        target, previous = self.switch_options()
        calls = {}
        def service(release, options, command, payload, execute):
            calls[command] = (options['config_dir'], payload)
            return {'database': payload.get('database'), 'copied_from_version': 65} if command == 'branch-setup' \
                else self.service(release, options, command, payload, execute)
        with self.patches(service):
            result = installer.apply(self.options, self.args, self.execute)
        folder, payload = calls['branch-setup']
        self.assertEqual(folder, str(target))
        self.assertEqual(payload, {'source_config_dir': str(self.config), 'copy_data': True,
                                   'database': str(self.state / 'anime-discovery-refactor.sqlite'),
                                   'source_sql_dir': str(previous / 'src/anime/storage/sql')})
        self.assertEqual(calls['render'][0], str(target))              # Units use the branch's own folder.
        saved = json.loads((self.state / 'installer' / 'installer.json').read_text())
        self.assertEqual((saved['ref'], saved['config_dir']), ('refactor', str(target)))
        self.assertEqual(saved['branches'], {'NextGen': {'config_dir': str(self.config)},
                                             'refactor': {'config_dir': str(target)}})
        self.assertEqual((result['branch_switch'], result['database_copied_from_version']), ('new', 65))

    def test_a_failed_setup_removes_the_new_branch_state_and_keeps_the_old(self):
        target, _ = self.switch_options()
        database = self.state / 'anime-discovery-refactor.sqlite'
        def service(release, options, command, payload, execute):
            if command == 'branch-setup':
                (Path(options['config_dir']) / 'settings.json').write_text('{}')
                database.write_bytes(b'partial')
                return {'database': str(database), 'copied_from_version': 65}
            if command == 'prepare':
                raise RuntimeError('runuser exited with status 1; detail: INSTALL_SETUP_FAILED')
            return self.service(release, options, command, payload, execute)
        with self.patches(service), self.assertRaises(RuntimeError):
            installer.apply(self.options, self.args, self.execute)
        self.assertFalse(target.exists())
        self.assertFalse(database.exists())
        self.assertTrue((self.config / 'settings.json').exists())      # The current branch is untouched.
        self.assertFalse((self.state / 'installer' / 'installer.json').exists())


if __name__ == '__main__':
    unittest.main()
