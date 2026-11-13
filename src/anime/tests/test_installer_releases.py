"""Installed versions: readable names, one previous kept, rollback, self re-run, database backups."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from src.anime.storage.migrations import prune_backups
from src.anime.tests import test_installer as base

installer = base.installer

UNIT = 'anime-discovery-api.service'


class ReleaseLayoutTests(base.InstallerOrchestrationTests):
    # Only the tests below; the inherited ones already run in test_installer.
    for _name in [n for n in dir(base.InstallerOrchestrationTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def releases(self):
        return sorted(p.name for p in (self.root / 'releases').iterdir())

    def test_release_folders_have_readable_unique_names(self):
        with self.patches():
            first = installer.apply(self.options, self.args, self.execute)
            second = installer.apply(self.options, self.args, self.execute)
        today = time.strftime('%Y-%m-%d')
        self.assertEqual(Path(first['release']).name, f'{today}-local-aaaaaaaa')     # --source-dir install.
        self.assertEqual(Path(second['release']).name, f'{today}-local-aaaaaaaa-2')  # Same commit again.
        info = json.loads((Path(second['release']) / installer.RELEASE_INFO).read_text())
        self.assertEqual(info['revision'], 'a' * 40)
        self.assertEqual(info['config_dir'], self.options['config_dir'])   # bin/adm run from the release finds it.
        self.assertEqual(os.readlink(self.root / 'current'), second['release'])
        # A re-run of a fetched installer is named after the configured branch.
        with self.patches(), patch.dict(os.environ, {installer.REEXEC_ENV: '1'}):
            third = installer.apply(self.options, self.args, self.execute)
        self.assertEqual(Path(third['release']).name, f'{today}-NextGen-aaaaaaaa')

    def test_upgrade_keeps_the_running_release_and_one_previous(self):
        releases = self.root / 'releases'
        for name in ('a' * 32, 'b' * 32, '.incoming-' + 'c' * 32):     # Old random names and a leftover.
            (releases / name).mkdir(parents=True)
        (self.root / 'current').symlink_to(releases / ('b' * 32))
        backups = self.state / 'installer' / 'unit-backups'
        for name in ('a' * 32, 'b' * 32):
            (backups / name).mkdir(parents=True)
        os.chmod(self.state / 'installer', 0o700)
        with self.patches():
            result = installer.apply(self.options, self.args, self.execute)
        new = Path(result['release']).name
        self.assertEqual(self.releases(), sorted([new, 'b' * 32]))
        self.assertEqual(sorted(result['removed_releases']), sorted(['.incoming-' + 'c' * 32, 'a' * 32]))
        self.assertEqual(sorted(p.name for p in backups.iterdir()), sorted([new, 'b' * 32]))

    def test_a_failed_upgrade_deletes_nothing(self):
        releases = self.root / 'releases'
        for name in ('a' * 32, 'b' * 32):
            (releases / name).mkdir(parents=True)
        (self.root / 'current').symlink_to(releases / ('b' * 32))

        def failing(*args):
            raise RuntimeError('simulated setup failure')
        with self.patches(failing), self.assertRaises(RuntimeError):
            installer.apply(self.options, self.args, self.execute)
        self.assertEqual(len(self.releases()), 3)                          # Both old ones and the candidate.
        self.assertEqual(os.readlink(self.root / 'current'), str(releases / ('b' * 32)))

    # --- rollback -------------------------------------------------------------
    def installed_pair(self):
        releases = self.root / 'releases'
        old, new = releases / '2026-11-01-NextGen-11111111', releases / '2026-11-06-NextGen-22222222'
        for release in (old, new):
            release.mkdir(parents=True)
        (old / installer.RELEASE_INFO).write_text(json.dumps({'revision': '1' * 40}))
        (self.root / 'current').symlink_to(new)
        metadata = self.state / 'installer'
        (metadata / 'unit-backups' / new.name).mkdir(parents=True)
        os.chmod(metadata, 0o700)
        (metadata / 'unit-backups' / new.name / UNIT).write_text('old release unit')
        (metadata / 'installation.json').write_text(json.dumps({'release': str(new), 'previous_release': str(old)}))
        self.unitdir.mkdir()
        (self.unitdir / UNIT).write_text('new release unit')
        return old, new

    def rollback_execute(self, check_status=0, stderr=''):
        def execute(args, **kwargs):
            args = [str(a) for a in args]
            self.events.append(args)
            if args[-4:-2] == ['src.anime', 'check']:
                return subprocess.CompletedProcess(args, check_status, '', stderr)
            if args[:3] == ['systemctl', 'is-active', '--quiet']:
                return subprocess.CompletedProcess(args, 0 if args[3] == UNIT else 3, '', '')
            return subprocess.CompletedProcess(args, 0, '', '')
        return execute

    def test_rollback_switches_to_the_previous_release_and_its_units(self):
        old, new = self.installed_pair()
        with self.patches():
            result = installer.rollback(self.options, self.args, self.rollback_execute())
        self.assertEqual(os.readlink(self.root / 'current'), str(old))
        self.assertEqual((self.unitdir / UNIT).read_text(), 'old release unit')
        self.assertEqual(result['revision'], '1' * 40)
        self.assertEqual(result['started_services'], [UNIT])                # Only what was running.
        self.assertIn(['systemctl', 'stop', UNIT], self.events)
        self.assertIn(['systemctl', 'start', UNIT], self.events)
        self.assertTrue(new.is_dir())                                        # Nothing deleted.
        recorded = json.loads((self.state / 'installer' / 'installation.json').read_text())
        self.assertEqual((recorded['release'], recorded['previous_release']), (str(old), str(new)))
        check = next(e for e in self.events if 'check' in e)
        self.assertEqual(check[:4], ['runuser', '-u', self.options['user'], '--'])   # As the service account.

    def test_rollback_restores_adm_to_previous_branch(self):
        old, new = self.installed_pair()
        (old / installer.RELEASE_INFO).write_text(
            json.dumps({'revision': '1' * 40, 'ref': 'NextGen'}))
        current_config = str(self.config / 'branches' / 'refactor')
        previous_config = str(self.config)
        self.options['config_dir'] = current_config
        self.options['branches'] = {
            'NextGen': {'config_dir': previous_config},
            'refactor': {'config_dir': current_config},
        }
        self.command.parent.mkdir(parents=True, exist_ok=True)
        (self.command.parent / 'adm').write_text(
            installer.adm_launcher(self.root, current_config))
        with self.patches():
            installer.rollback(self.options, self.args, self.rollback_execute())
        launcher = (self.command.parent / 'adm').read_text()
        self.assertIn('ANIME_DISCOVERY_CONFIG_DIR='+previous_config+'\n', launcher)
        self.assertEqual(os.readlink(self.root / 'current'), str(old))

    def test_rollback_refuses_when_the_database_is_too_new_for_the_previous_release(self):
        old, new = self.installed_pair()
        execute = self.rollback_execute(1, 'DATABASE_VERSION_UNSUPPORTED: This database version cannot be upgraded')
        with self.patches(), self.assertRaises(ValueError) as refused:
            installer.rollback(self.options, self.args, execute)
        self.assertIn('Cannot roll back to 2026-11-01-NextGen-11111111', str(refused.exception))
        self.assertIn('DATABASE_VERSION_UNSUPPORTED', str(refused.exception))
        self.assertEqual(os.readlink(self.root / 'current'), str(new))      # Unchanged...
        self.assertEqual((self.unitdir / UNIT).read_text(), 'new release unit')
        self.assertFalse(any(e[:2] == ['systemctl', 'stop'] for e in self.events))   # ...and nothing stopped.

    def test_rollback_needs_a_recorded_previous_release(self):
        old, new = self.installed_pair()
        (self.state / 'installer' / 'installation.json').write_text(json.dumps({'release': str(new)}))
        with self.patches(), self.assertRaises(ValueError):
            installer.rollback(self.options, self.args, self.rollback_execute())

    def test_rollback_is_a_mode_of_its_own(self):
        with self.assertRaises(SystemExit):
            installer.parser().parse_args(['--upgrade', '--rollback'])


class BranchTests(base.InstallerSupportTests):
    for _name in [n for n in dir(base.InstallerSupportTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def saved(self):
        self.config.joinpath('settings.json').write_text(json.dumps({
            'schema_version': 10, 'settings': {'storage': {'database_path': str(self.database)}}}))
        return {'root': str(self.root / 'app'), 'config_dir': str(self.config), 'state_dir': str(self.root / 'state'),
                'unit_dir': str(self.root / 'units'), 'ref': 'NextGen', 'origin': 'https://anime.example.test'}

    def resolve(self, *flags):
        with patch.object(installer, 'saved_defaults', return_value=self.saved()):
            return installer.resolve(installer.parser().parse_args(list(flags)))

    def test_saved_installation_metadata_retains_branch_index(self):
        import stat
        from types import SimpleNamespace
        record = {
            'ref': 'refactor',
            'config_dir': '/etc/anime-discovery/branches/refactor',
            'config_base': '/etc/anime-discovery',
            'branches': {'NextGen': {'config_dir': '/etc/anime-discovery'},
                         'refactor': {'config_dir': '/etc/anime-discovery/branches/refactor'}},
        }
        with patch.object(installer, 'private_json', return_value=record), \
             patch.object(installer.Path, 'lstat',
                          return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)), \
             patch.object(installer.Path, 'stat',
                          return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)):
            loaded = installer._saved_installation_file(Path('/not-used/installer.json'))
        self.assertEqual(loaded['branches'], record['branches'])
        self.assertEqual(loaded['config_base'], record['config_base'])

    def test_upgrade_can_switch_branches(self):
        self.assertEqual(self.resolve('--upgrade')['ref'], 'NextGen')
        self.assertEqual(self.resolve('--upgrade', '--branch', 'refactor')['ref'], 'refactor')

    def test_other_settings_still_cannot_change_on_upgrade(self):
        with self.assertRaisesRegex(ValueError, 'use --branch'):
            self.resolve('--upgrade', '--ref', 'refactor')
        with self.assertRaisesRegex(ValueError, 'origin'):
            self.resolve('--upgrade', '--branch', 'refactor', '--public-origin', 'https://other.example.test')

    def test_invalid_or_conflicting_branches_are_refused(self):
        for flags in (['--upgrade', '--branch', 'bad branch'], ['--upgrade', '--branch=-x'],
                      ['--rollback', '--branch', 'refactor']):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                self.resolve(*flags)

    def test_install_flag_is_gone(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            installer.parser().parse_args(['--install'])


class BranchUpgradeTests(base.InstallerOrchestrationTests):
    for _name in [n for n in dir(base.InstallerOrchestrationTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def test_a_branch_switch_names_the_release_saves_the_branch_and_replaces_units(self):
        self.unitdir.mkdir()
        (self.unitdir / UNIT).write_text('[Unit]\nDescription=Old\n')
        self.args.source_dir = None                 # Fetch from git, like a normal upgrade.
        self.options['ref'] = 'refactor'
        self.options['config_dir'] = str(self.config / 'branches' / 'refactor')

        def execute(args, **kwargs):
            args = [str(a) for a in args]
            if args[:2] == ['git', 'clone']:
                Path(args[-1]).mkdir(parents=True)
                for name in ('src/anime/__main__.py', 'src/anime/install_support.py',
                             'bin/akon-install', 'bin/adm'):
                    path = Path(args[-1]) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('# test candidate')
            if args[:2] == ['git', '-C'] and 'rev-parse' in args:
                self.events.append(args)
                return subprocess.CompletedProcess(args, 0, 'b' * 40 + '\n', '')
            return self.execute(args, **kwargs)
        with self.patches():
            result = installer.apply(self.options, self.args, execute)
        self.assertTrue(Path(result['release']).name.endswith('-refactor-bbbbbbbb'))
        fetch = next(e for e in self.events if 'fetch' in e)
        self.assertEqual(fetch[-1], 'refactor')                                  # The new branch was fetched.
        saved = json.loads((self.state / 'installer' / 'installer.json').read_text())
        self.assertEqual(saved['ref'], 'refactor')                               # Later upgrades keep it.
        self.assertIn('ANIME_DISCOVERY_CONFIG_DIR='+self.options['config_dir']+'\n',
                      (self.command.parent / 'adm').read_text())
        # Service files are rewritten from the new release, the old ones backed up.
        self.assertEqual((self.unitdir / UNIT).read_text(), '[Unit]\nDescription=Test\n')
        backup = self.state / 'installer' / 'unit-backups' / Path(result['release']).name / UNIT
        self.assertEqual(backup.read_text(), '[Unit]\nDescription=Old\n')
        self.assertIn(['systemctl', 'daemon-reload'], self.events)


class ReexecTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.running = self.folder / 'running'
        self.running.write_text('old installer')
        self.checkout = self.folder / 'checkout'
        (self.checkout / 'bin').mkdir(parents=True)

    def args(self, upgrade=True):
        return type('Args', (), {'upgrade': upgrade})()

    def test_only_an_upgrade_with_a_different_installer_re_runs_it(self):
        (self.checkout / 'bin/akon-install').write_text('new installer')
        self.assertTrue(installer.should_reexec(self.args(), self.checkout, self.running))
        self.assertFalse(installer.should_reexec(self.args(upgrade=False), self.checkout, self.running))
        with patch.dict(os.environ, {installer.REEXEC_ENV: '1'}):          # Already the re-run.
            self.assertFalse(installer.should_reexec(self.args(), self.checkout, self.running))
        (self.checkout / 'bin/akon-install').write_text('old installer')
        self.assertFalse(installer.should_reexec(self.args(), self.checkout, self.running))

    def test_main_runs_the_new_installer_on_the_fetched_checkout_once(self):
        seen = {}

        def run(command, env, cwd):
            seen['command'], seen['env'], seen['cwd'] = command, env, cwd
            return subprocess.CompletedProcess(command, 0)
        with patch.object(installer, 'resolve', return_value={}), \
                patch.object(installer, 'apply', side_effect=installer.Reexec(self.checkout, 'f' * 40)), \
                patch.object(installer.subprocess, 'run', side_effect=run):
            code = installer.main(['--upgrade', '--source-dir', '/old', '--revision', 'e' * 40])
        self.assertEqual(code, 0)
        command = seen['command']
        self.assertEqual(command[1], str(self.checkout / 'bin/akon-install'))
        self.assertEqual(command[2:], ['--upgrade', '--source-dir', str(self.checkout), '--revision', 'f' * 40])
        self.assertEqual(seen['env'][installer.REEXEC_ENV], '1')
        self.assertEqual(seen['cwd'], '/')                                 # Never the caller's folder.
        self.assertFalse(self.checkout.exists())                           # The fetched copy is removed.


class DurableUpgradeTests(base.InstallerOrchestrationTests):
    """Outages, hang-ups and early failures must not leave the install half-done or down."""
    for _name in [n for n in dir(base.InstallerOrchestrationTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    API = 'anime-discovery-api.service'

    def running_api(self, fail=None):
        """systemctl reports the API active; `fail(args)` may raise for one step."""
        def execute(args, **kwargs):
            args = [str(x) for x in args]
            if fail:
                fail(args)
            if args[:3] == ['systemctl', 'is-active', '--quiet']:
                self.events.append(args)
                return subprocess.CompletedProcess(args, 0 if args[3] == self.API else 3, '', '')
            return self.execute(args, **kwargs)
        return execute

    def test_a_brief_network_outage_is_retried(self):
        attempts = []

        def fail(args):
            if 'pip' in args:
                attempts.append(args)
                if len(attempts) == 1:
                    raise RuntimeError('pip exited with status 1; detail: temporary failure in name resolution')
        with self.patches(), patch.object(installer.time, 'sleep') as sleep, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = installer.apply(self.options, self.args, self.running_api(fail))
        self.assertEqual(len(attempts), 2)
        sleep.assert_called_once_with(installer.RETRY_DELAYS[0])
        self.assertIn('retrying', out.getvalue())
        self.assertTrue(result['installed'])

    def test_a_lasting_outage_fails_and_removes_the_half_built_release(self):
        releases = self.root / 'releases'
        (releases / ('b' * 32)).mkdir(parents=True)
        (self.root / 'current').symlink_to(releases / ('b' * 32))

        attempts = []

        def fail(args):
            if 'pip' in args:
                attempts.append(args)
                raise RuntimeError('pip exited with status 1')
        with self.patches(), patch.object(installer.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, 'pip'):
            installer.apply(self.options, self.args, self.running_api(fail))
        self.assertEqual(len(attempts), len(installer.RETRY_DELAYS) + 1)
        self.assertEqual(self.releases(), ['b' * 32])                     # Only the running release is left.
        self.assertEqual(os.readlink(self.root / 'current'), str(releases / ('b' * 32)))
        self.assertFalse(any(e[:2] == ['systemctl', 'stop'] for e in self.events))

    def test_failure_before_the_database_changes_restarts_what_was_running(self):
        self.args.upgrade, self.args.start = True, 'preserve'
        self.args.settings_file = self.folder / 'missing-settings.json'   # Fails after services stop.
        with self.patches(), contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(OSError):
            installer.apply(self.options, self.args, self.running_api())
        self.assertIn(['systemctl', 'start', self.API], self.events)
        self.assertNotIn('prepare', [e[1] for e in self.events if e[:1] == ['native']])
        self.assertIn('restarting', out.getvalue())
        self.assertFalse((self.state / 'installer' / 'installation.pending.json').exists())
        self.assertEqual(self.releases(), [])                              # Candidate removed.

    def test_failure_after_the_database_changes_keeps_services_stopped_and_the_release(self):
        self.args.upgrade, self.args.start = True, 'preserve'

        def service(release, options, command, payload, execute):
            self.events.append(['native', command])
            if command == 'render':
                raise RuntimeError('simulated failure after migration')
            return {'local_ready': True}
        with self.patches(service), self.assertRaises(RuntimeError):
            installer.apply(self.options, self.args, self.running_api())
        self.assertNotIn(['systemctl', 'start', self.API], self.events)
        self.assertEqual(len(self.releases()), 1)                          # Only code that runs the new schema.

    def releases(self):
        return sorted(p.name for p in (self.root / 'releases').iterdir())


class DurableOutputTests(unittest.TestCase):
    def test_a_closed_terminal_does_not_stop_the_run_and_the_log_keeps_everything(self):
        class Gone(io.StringIO):
            def write(self, text):
                raise OSError(5, 'Input/output error')
        log = io.StringIO()
        stream = installer.DurableStream(Gone(), log)
        stream.write('Stopping services...\n')
        stream.flush()
        stream.write('Upgraded.\n')
        self.assertEqual(log.getvalue(), 'Stopping services...\nUpgraded.\n')
        self.assertFalse(stream.isatty())

    def test_hang_ups_are_ignored_during_a_run_and_restored_after(self):
        before = installer.signal.getsignal(installer.signal.SIGHUP)
        with patch.object(installer.os, 'geteuid', return_value=1000):
            with installer.durable_run({'state_dir': '/nonexistent'}):
                self.assertIs(installer.signal.getsignal(installer.signal.SIGHUP), installer.signal.SIG_IGN)
                self.assertIsInstance(installer.sys.stdout, installer.DurableStream)
        self.assertEqual(installer.signal.getsignal(installer.signal.SIGHUP), before)
        self.assertNotIsInstance(installer.sys.stdout, installer.DurableStream)

    def test_root_runs_are_logged_privately(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = Path(temp) / 'installer'
            metadata.mkdir()
            with patch.object(installer.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()):
                with installer.durable_run({'state_dir': temp}):
                    print('Preparing candidate source...')
            log = metadata / 'last-run.log'
            self.assertIn('Preparing candidate source...', log.read_text())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)


class DatabaseBackupTests(unittest.TestCase):
    def test_only_the_two_newest_pre_upgrade_copies_are_kept(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            database = folder / 'anime-discovery.sqlite'
            names = [f'anime-discovery.sqlite.before-v{v}-{n * 32}.sqlite' for v, n in ((57, 'a'), (58, 'b'), (59, 'c'))]
            for age, name in enumerate(reversed(names)):          # c newest, a oldest.
                path = folder / name
                path.write_bytes(b'x')
                os.utime(path, ns=(10**18 - age * 10**9, 10**18 - age * 10**9))
            (folder / 'anime-discovery.sqlite').write_bytes(b'live')
            (folder / ('other.sqlite.before-v1-' + 'd' * 32 + '.sqlite')).write_bytes(b'x')
            self.assertEqual(prune_backups(database), [names[0]])
            left = sorted(p.name for p in folder.iterdir())
            self.assertEqual(left, sorted([names[1], names[2], 'anime-discovery.sqlite',
                                           'other.sqlite.before-v1-' + 'd' * 32 + '.sqlite']))
            with self.assertRaises(ValueError):
                prune_backups(database, keep=0)



class ReleasePackageTests(unittest.TestCase):
    """Rollback and branch switches run an older release's code: use its own package."""
    def test_each_release_runs_its_own_package(self):
        import importlib.machinery, importlib.util, tempfile
        loader = importlib.machinery.SourceFileLoader(
            'installer_under_test', str(Path(__file__).resolve().parents[3] / 'bin/akon-install'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        installer = importlib.util.module_from_spec(spec)
        loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            for name, expected in (('anime', 'src.anime'), ('next' + 'gen', 'src.' + 'next' + 'gen')):
                release = Path(tmp) / name
                (release / 'src' / name).mkdir(parents=True)
                (release / 'src' / name / '__main__.py').write_text('')
                self.assertEqual(installer.package(release), expected)


if __name__ == '__main__':
    unittest.main()
