"""Offline installer/import cases; no packages, real units or live services."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src.anime.configuration import ConfigurationStore
from src.anime.install_support import prepare, units
from src.anime.secrets import SecretStore
from src.anime.storage.sqlite import connect

ROOT=Path(__file__).resolve().parents[3]


def load(name,path):
    loader=importlib.machinery.SourceFileLoader(name,str(path));spec=importlib.util.spec_from_loader(name,loader)
    module=importlib.util.module_from_spec(spec);loader.exec_module(module);return module


installer=load('nextgen_test_installer',ROOT/'bin/anime-discovery-install')
class InstallerSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.config=self.root/'config';self.config.mkdir(mode=0o700);self.config.chmod(0o700)
        self.database=self.root/'anime.sqlite'

    def test_fresh_setup_and_repeated_upgrade_preserve_values_and_keys(self):
        result=prepare(self.config,self.database,{'worker':{'concurrency':2}}, {'webhook_token':'w'*43})
        self.assertTrue(result['local_ready']);store=ConfigurationStore(self.config)
        before=(self.config/'secrets.json').read_bytes();settings=(self.config/'settings.json').read_bytes()
        prepare(self.config,self.root/'ignored.sqlite',{'worker':{'concurrency':9}},{'webhook_token':'r'*43})
        self.assertEqual((self.config/'secrets.json').read_bytes(),before)
        self.assertEqual((self.config/'settings.json').read_bytes(),settings)
        self.assertEqual(store.read().value('worker.concurrency'),2);self.assertFalse((self.root/'ignored.sqlite').exists())

    def test_prepared_setup_keeps_webhook_without_management_token(self):
        ConfigurationStore(self.config).initialize({'storage':{'database_path':str(self.database)}})
        SecretStore(self.config).initialize(webhook_token='w'*43)
        prepare(self.config,self.database,{}, {})
        keys=SecretStore(self.config).access_credentials()
        self.assertEqual(keys.webhook_token,'w'*43);self.assertFalse(keys.management_token)
        self.assertFalse((self.config/'management-password.json').exists())

    def test_upgrade_preserves_existing_administrator_password(self):
        from src.anime.api.password_store import PasswordStore
        prepare(self.config,self.database,{}, {})
        passwords=PasswordStore(self.config)
        passwords.replace('keep this administrator password',expected_revision=0)
        before=(self.config/'management-password.json').read_bytes()
        prepare(self.config,self.database,{}, {})
        self.assertEqual((self.config/'management-password.json').read_bytes(),before)
        self.assertTrue(passwords.verify('keep this administrator password')[1])

    def test_existing_legacy_database_is_not_upgraded(self):
        import sqlite3
        db=sqlite3.connect(self.database);db.execute('CREATE TABLE old_data(value TEXT)');db.execute("INSERT INTO old_data VALUES('preserve')");db.commit();db.close()
        with self.assertRaises(Exception):prepare(self.config,self.database,{}, {})
        db=sqlite3.connect(self.database)
        try:self.assertEqual(db.execute('SELECT * FROM old_data').fetchall(),[('preserve',)])
        finally:db.close()

    def test_units_use_real_paths_loopback_port_and_media_mounts(self):
        from src.anime.configuration_schema import ConfigurationSnapshot
        snapshot=ConfigurationSnapshot(1,{'api':{'port':8877},'storage':{'download_root':'/mnt/downloads','user_evaluation_root':'/mnt/staging','keep_root':'/mnt/Anime Library'}})
        rendered=units(snapshot,'/opt/anime-discovery',str(self.config),str(self.root),'anime-discovery',None)
        self.assertIn('http://127.0.0.1:8877',rendered['anime-discovery-api.service'])
        self.assertIn('/opt/anime-discovery/current/.venv/bin/python',rendered['anime-discovery-api.service'])
        self.assertIn('RequiresMountsFor=',rendered['anime-discovery-worker.service'])
        # The worker enables its normal Sonarr pipeline without a startup flag.
        self.assertNotIn('--sonarr-acquire',rendered['anime-discovery-worker.service'])
        self.assertIn('"/mnt/Anime Library"',rendered['anime-discovery-worker.service'])
        self.assertNotIn('/mnt/Anime Library',rendered['anime-discovery-api.service'])
        for name in ('anime-discovery-api.service','anime-discovery-worker.service'):
            self.assertIn('Type=notify',rendered[name])
            self.assertIn('WatchdogSec=60s',rendered[name])
        self.assertIn('BindsTo=anime-discovery-api.service',rendered['anime-discovery-worker.service'])
        # A misconfigured required service exits 78; systemd must not loop.
        self.assertIn('RestartPreventExitStatus=78\n',rendered['anime-discovery-worker.service'])
        self.assertNotIn('RestartPreventExitStatus',rendered['anime-discovery-api.service'])
        self.assertIn('RuntimeDirectoryMode=0700',rendered['anime-discovery-api.service'])
        self.assertIn('ANIME_DISCOVERY_ADMIN_SOCKET=',rendered['anime-discovery-api.service'])

    def test_rendered_units_pass_systemd_parser(self):
        import shutil
        import sys
        from src.anime.configuration_schema import ConfigurationSnapshot
        if shutil.which('systemd-analyze') is None:self.skipTest('systemd-analyze unavailable')
        executable=self.root/'current/.venv/bin/python'
        executable.parent.mkdir(parents=True)
        executable.symlink_to(sys.executable)
        rendered=units(ConfigurationSnapshot(1,{}),self.root,self.config,self.root,'anime-discovery',None)
        paths=[]
        for name,body in rendered.items():
            path=self.root/name
            path.write_text(body)
            paths.append(str(path))
        result=subprocess.run(['systemd-analyze','verify',*paths],capture_output=True,text=True)
        if result.returncode != 0 and "Failed to create directory '/run/systemd/'" in result.stderr:
            self.skipTest('systemd runtime directory unavailable')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_installer_has_no_plan_option(self):
        with self.assertRaises(SystemExit):
            installer.parser().parse_args(['--plan'])

    def test_default_completion_is_readable_and_json_is_opt_in(self):
        result={'installed':True,'started_services':[]}
        defaults={'config_dir':str(self.config),'root':str(self.root/'app')}
        for flags in (['--yes'],['--yes','--json']):
            output=io.StringIO()
            with patch.object(installer,'saved_defaults',return_value=defaults), \
                 patch.object(installer,'apply',return_value=result),contextlib.redirect_stdout(output):
                self.assertEqual(installer.main(flags),0)
            if '--json' in flags:self.assertEqual(json.loads(output.getvalue()),result)
            else:
                self.assertIn('installation completed successfully',output.getvalue())
                self.assertIn('No services were started.',output.getvalue())
                self.assertIn('systemctl start anime-discovery-api.service',output.getvalue())
                self.assertNotIn('"installed"',output.getvalue())

    def test_yes_never_prompts_and_interactive_mode_asks_before_changes(self):
        defaults={'root':str(self.root/'app'),'config_dir':str(self.root/'native'),
            'state_dir':str(self.root/'state'),'unit_dir':str(self.root/'units')}
        with patch.object(installer,'saved_defaults',return_value=defaults), \
             patch.object(installer,'apply',return_value={'installed':True}) as apply, \
             patch('builtins.input',side_effect=AssertionError('unattended prompt')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(['--yes']),0);apply.assert_called_once()
        with patch.object(installer,'saved_defaults',return_value=defaults), \
             patch.object(installer,'apply',return_value={'installed':True}) as apply, \
             patch('builtins.input',side_effect=['','','','','no']) as ask, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main([]),0);apply.assert_not_called();self.assertEqual(ask.call_count,5)

    def test_upgrade_uses_saved_installation_without_interactive_prompts(self):
        self.config.joinpath('settings.json').write_text(json.dumps({
            'schema_version':10, 'settings':{'storage':{'database_path':str(self.database)}}}))
        saved={'root':str(self.root/'app'), 'config_dir':str(self.config),
               'state_dir':str(self.root/'state'), 'unit_dir':str(self.root/'units'),
               'ref':'NextGen', 'origin':'https://anime.example.test'}
        with patch.object(installer,'saved_defaults',return_value=saved), \
             patch.object(installer,'apply',return_value={'installed':True,'revision':'a'*40,'started_services':['anime-discovery-api.service'],'remaining_stopped_services':['anime-discovery-worker.service']}) as apply, \
             patch('builtins.input',side_effect=AssertionError('upgrade must not prompt')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(['--upgrade']),0)
        self.assertEqual(apply.call_count,1)
        options, args = apply.call_args.args
        self.assertEqual(options['config_dir'],str(self.config))
        self.assertEqual(options['origin'],'https://anime.example.test')
        self.assertEqual(options['ref'],'NextGen')
        self.assertEqual(options['database'],str(self.database))
        self.assertTrue(args.upgrade)

    def test_completion_messages_are_specific_to_install_and_upgrade(self):
        self.config.joinpath('settings.json').write_text(json.dumps({
            'schema_version':10,'settings':{'storage':{'database_path':str(self.database)}}}))
        defaults={'root':str(self.root/'app'),'config_dir':str(self.config)}
        report={'revision':'b'*40,'started_services':['anime-discovery-api.service'],
                'remaining_stopped_services':['anime-discovery-worker.service']}
        for flags in (['--upgrade'],['--yes']):
            output=io.StringIO()
            with patch.object(installer,'saved_defaults',return_value=defaults), \
                 patch.object(installer,'apply',return_value=report), \
                 patch('builtins.input',side_effect=AssertionError('unexpected prompt')), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(installer.main(flags),0)
            self.assertNotIn('Configure providers',output.getvalue())
            if '--upgrade' in flags:
                self.assertIn('upgraded successfully',output.getvalue())
                self.assertIn('Revision:',output.getvalue())
                self.assertIn('Restarted services: anime-discovery-api.service',output.getvalue())
                self.assertIn('Remained stopped: anime-discovery-worker.service',output.getvalue())
                self.assertNotIn('set-password',output.getvalue())
                self.assertNotIn('Future upgrades:',output.getvalue())
            else:
                self.assertIn('installation completed successfully',output.getvalue())
                self.assertIn('Started services: anime-discovery-api.service',output.getvalue())

    def test_upgrade_refuses_changes_to_saved_installation(self):
        self.config.joinpath('settings.json').write_text(json.dumps({
            'schema_version':10,'settings':{'storage':{'database_path':str(self.database)}}}))
        saved={'root':str(self.root/'app'),'config_dir':str(self.config),'origin':'https://anime.example.test'}
        for flags in (['--public-origin','https://other.example.test'],
                      ['--config-dir',str(self.root/'other')],
                      ['--admin-password-file',str(self.root/'password')]):
            with self.subTest(flags=flags),patch.object(installer,'saved_defaults',return_value=saved):
                with self.assertRaises(ValueError):
                    installer.resolve(installer.parser().parse_args(['--upgrade',*flags]))

    def test_upgrade_without_installation_metadata_fails_closed(self):
        with patch.object(installer,'saved_defaults',return_value={}):
            with self.assertRaisesRegex(ValueError,'existing installation'):
                installer.resolve(installer.parser().parse_args(['--upgrade']))

    def test_upgrade_failure_reports_reason_instead_of_generic_message(self):
        saved={'root':str(self.root/'app'),'config_dir':str(self.config)}
        self.config.joinpath('settings.json').write_text(json.dumps({'schema_version':10,'settings':{}}))
        output=io.StringIO()
        with patch.object(installer,'saved_defaults',return_value=saved), \
             patch.object(installer,'apply',side_effect=RuntimeError('git exited with status 128')), \
             contextlib.redirect_stderr(output):
            self.assertEqual(installer.main(['--upgrade']),1)
        self.assertIn('git exited with status 128',output.getvalue())
        self.assertNotIn('Review prerequisites',output.getvalue())

    def test_upgrade_metadata_lookup_uses_global_installer_even_from_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'app'
            root.mkdir()
            meta=root/'installer.json'
            meta.write_text(json.dumps({'root':str(root),'ref':'NextGen'}))
            with patch.object(installer,'_saved_installation_file',side_effect=lambda path: json.loads(path.read_text())), \
                 patch.object(installer,'DEFAULTS',{**installer.DEFAULTS,'root':str(root),'destination':str(Path(temp)/'bin/install')}):
                self.assertEqual(installer.saved_defaults()['root'],str(root))

    def test_upgrade_without_existing_native_config_fails_closed(self):
        with patch.object(installer,'saved_defaults',return_value={'config_dir':str(self.config)}), \
             patch.object(installer,'apply',side_effect=AssertionError('must not install')), \
             patch('builtins.input',side_effect=AssertionError('must not prompt')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(['--upgrade']),1)

    def test_new_metadata_wins_over_legacy_and_custom_state_is_discovered(self):
        state=self.root/'custom-state'
        root=self.root/'app';root.mkdir()
        metadata=state/'installer';metadata.mkdir(parents=True)
        old={'root':str(root),'state_dir':str(state),'ref':'old'}
        new={**old,'ref':'NextGen'}
        (root/'installer.json').write_text(json.dumps(old))
        (metadata/'installer.json').write_text(json.dumps(new))
        with patch.object(installer,'_saved_installation_file',side_effect=lambda path: json.loads(path.read_text())), \
             patch.object(installer,'DEFAULTS',{**installer.DEFAULTS,'root':str(root),'state_dir':str(self.root/'unused'),'destination':str(self.root/'bin/install')}):
            self.assertEqual(installer.saved_defaults(),new)

    def test_shell_metacharacters_and_invalid_revision_are_refused(self):
        for argv in [['--root','/opt/$(touch bad)'],['--revision','HEAD'],['--source','https://token@example.com/repo.git']]:
            with patch.object(installer,'saved_defaults',return_value={}):
                with self.assertRaises(ValueError):installer.resolve(installer.parser().parse_args(['--yes',*argv]))


class InstallerOrchestrationTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.namespace=SimpleNamespace
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.root=self.folder/'app';self.state=self.folder/'state'
        self.config=self.state/'config';self.unitdir=self.folder/'units';self.command=self.folder/'bin'/'install'
        self.events=[]
        with patch.object(installer,'saved_defaults',return_value={}):
            self.args=installer.parser().parse_args(['--yes','--source-dir',str(self.folder/'source'),'--root',str(self.root),
                '--state-dir',str(self.state),'--config-dir',str(self.config),'--unit-dir',str(self.unitdir),
                '--destination',str(self.command),'--start','none'])
            self.options=installer.resolve(self.args)

    def candidate(self,source,release,revision,execute):
        for name in ['src/anime/__main__.py','src/anime/install_support.py','bin/anime-discovery-install','bin/adm']:
            path=release/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# test candidate')
        return 'a'*40

    def execute(self,args,**kwargs):
        args=[str(a) for a in args];self.events.append(args)
        if args[:2]==['systemctl','show']:return subprocess.CompletedProcess(args,0,'not-found\n','')
        if args[:2]==['systemctl','is-active']:return subprocess.CompletedProcess(args,3,'','')
        return subprocess.CompletedProcess(args,0,'','')

    def service(self,release,options,command,payload,execute):
        self.events.append(['native',command])
        if command=='render':return {'anime-discovery-api.service':'[Unit]\nDescription=Test\n'}
        return {'local_ready':True}

    @contextlib.contextmanager
    def patches(self,service=None):
        account=self.namespace(pw_uid=1001,pw_gid=1001)
        real_stat=Path.stat
        real_lstat=Path.lstat
        def root_stat(path,*args,**kwargs):
            result=real_stat(path,*args,**kwargs)
            if path.is_relative_to(self.folder):
                values=list(result);values[4]=0
                return os.stat_result(values)
            return result
        def root_lstat(path,*args,**kwargs):
            result=real_lstat(path,*args,**kwargs)
            if path.is_relative_to(self.folder):
                values=list(result);values[4]=0
                return os.stat_result(values)
            return result
        with patch.object(installer.Path,'stat',autospec=True,side_effect=root_stat), \
             patch.object(installer.Path,'lstat',autospec=True,side_effect=root_lstat), \
             patch.object(installer.os,'geteuid',return_value=0),patch.object(installer.os,'chown'), \
             patch.object(installer.pwd,'getpwnam',return_value=account), \
             patch.object(installer.grp,'getgrnam',return_value=self.namespace(gr_gid=1001)), \
             patch.object(installer.shutil,'which',return_value='/tool'), \
             patch.object(installer,'extract_checkout',side_effect=self.candidate), \
             patch.object(installer,'service_call',side_effect=service or self.service), \
             patch.object(installer,'should_reexec',return_value=False):yield

    def test_fresh_install_handles_absent_units_and_installs_stable_command(self):
        with self.patches():result=installer.apply(self.options,self.args,self.execute)
        self.assertTrue(result['installed']);self.assertEqual(result['started_services'],[])
        self.assertTrue((self.root/'current').is_symlink());self.assertTrue(self.command.is_file())
        self.assertEqual(self.command.stat().st_mode&0o777,0o755)
        self.assertTrue((self.state/'installer/installer.json').is_file())
        self.assertTrue((self.state/'installer/installation.json').is_file())
        self.assertFalse((self.root/'installer.json').exists())
        self.assertFalse((self.root/'installation.json').exists())
        self.assertEqual(json.loads(Path(str(self.command)+'.json').read_text()),{'root':str(self.root),'state_dir':str(self.state)})
        adm=self.command.parent/'adm'
        self.assertEqual(adm.stat().st_mode&0o777,0o755)
        self.assertIn(str(self.root/'current/bin/adm'),adm.read_text())
        self.assertIn('"$@"',adm.read_text())
        self.assertIn('ANIME_DISCOVERY_CONFIG_DIR='+str(self.config)+'\n',adm.read_text())
        self.assertFalse(any(event[:2]==['systemctl','stop'] for event in self.events))
        self.assertIn(['native','prepare'],self.events);self.assertIn(['native','render'],self.events)
        self.assertFalse(any('apt-get' in event for event in self.events))

    def test_upgrade_restarts_only_services_active_before_upgrade(self):
        self.args.upgrade=True
        self.args.start='preserve'
        for initial in (['anime-discovery-api.service','anime-discovery-worker.service'],
                        ['anime-discovery-api.service'],[]):
            with self.subTest(initial=initial):
                self.events.clear()
                def execute(args,**kwargs):
                    args=[str(x) for x in args]
                    if args[:3]==['systemctl','is-active','--quiet']:
                        # Initial sampling occurs before any stop/start commands.
                        started=any(e[:2]==['systemctl','start'] for e in self.events)
                        active=args[3] in (initial if not started else initial)
                        self.events.append(args)
                        return subprocess.CompletedProcess(args,0 if active else 3,'','')
                    return self.execute(args,**kwargs)
                with self.patches():result=installer.apply(self.options,self.args,execute)
                self.assertEqual(result['started_services'],initial)
                self.assertEqual(result['remaining_stopped_services'],
                    [s for s in installer.SERVICES if s not in initial])
                start=[event for event in self.events if event[:2]==['systemctl','start']]
                self.assertEqual(start,[['systemctl','start',*initial]] if initial else [])

    def test_success_moves_metadata_out_of_checkout_and_rerun_reads_it(self):
        self.root.mkdir()
        saved={k:self.options[k] for k in (*installer.DEFAULTS,'origin')}
        for name in ('installer.json','installation.json'):
            (self.root/name).write_text(json.dumps(saved));(self.root/name).chmod(0o600)
        with self.patches():installer.apply(self.options,self.args,self.execute)
        self.assertFalse((self.root/'installer.json').exists())
        self.assertFalse((self.root/'installation.json').exists())
        self.assertTrue((self.state/'installer/installation.json').exists())
        self.assertEqual((self.state/'installer').stat().st_mode&0o777,0o700)
        with self.patches(),patch.object(installer,'DEFAULTS',{**installer.DEFAULTS,'destination':str(self.command)}):
            self.assertEqual(installer.saved_defaults(),saved)

    def test_upgrade_reports_failed_service_restart(self):
        self.args.upgrade=True
        self.args.start='preserve'
        def execute(args,**kwargs):
            args=[str(x) for x in args]
            if args[:3]==['systemctl','is-active','--quiet']:
                started=any(e[:2]==['systemctl','start'] for e in self.events)
                self.events.append(args)
                return subprocess.CompletedProcess(args,3 if started else 0,'','')
            return self.execute(args,**kwargs)
        with self.patches(),self.assertRaisesRegex(RuntimeError,
                'Service did not return to active state after upgrade: anime-discovery-api.service'):
            installer.apply(self.options,self.args,execute)

    def test_automated_password_is_sent_through_adm_stdin_only(self):
        password=self.folder/'password'
        password.write_text('automation password for testing\n')
        self.args.admin_password_file=password
        self.args.start='api'
        def execute(args,**kwargs):
            if '--password-stdin' in args:
                self.assertEqual(kwargs['input'],'automation password for testing\n')
                self.assertIn('--if-unset',args)
                self.assertNotIn('automation password for testing',args)
            result=self.execute(args,**kwargs)
            if args[:3]==['systemctl','is-active','--quiet']:
                return subprocess.CompletedProcess(args,0,'','')
            return result
        with self.patches():installer.apply(self.options,self.args,execute)
        self.assertTrue(any('--password-stdin' in event for event in self.events))

    def test_failed_native_setup_keeps_previous_release_and_units(self):
        old=self.root/'releases'/'old';old.mkdir(parents=True);(self.root/'current').symlink_to(old)
        legacy=self.root/'installer.json';legacy.write_text('{"ref":"NextGen"}')
        self.unitdir.mkdir();unit=self.unitdir/'anime-discovery-api.service';unit.write_text('old unit')
        def failing(*args):raise RuntimeError('simulated setup failure')
        with self.patches(failing),self.assertRaises(RuntimeError):installer.apply(self.options,self.args,self.execute)
        self.assertEqual((self.root/'current').resolve(),old);self.assertEqual(unit.read_text(),'old unit')
        self.assertFalse(self.command.exists())
        self.assertEqual(legacy.read_text(),'{"ref":"NextGen"}')
        self.assertTrue((self.state/'installer/installation.pending.json').is_file())
        self.assertFalse(any(event[:2]==['systemctl','start'] for event in self.events))

    def test_malicious_archive_is_refused_without_extracting_outside_release(self):
        import tarfile
        release=self.folder/'release';release.mkdir()
        def execute(args,**kwargs):
            if 'archive' in args:
                archive=Path(next(a.split('=',1)[1] for a in args if str(a).startswith('--output=')))
                with tarfile.open(archive,'w') as stream:
                    info=tarfile.TarInfo('../outside');info.size=1;stream.addfile(info,io.BytesIO(b'x'))
            return subprocess.CompletedProcess(args,0,'a'*40,'')
        with self.assertRaises(ValueError):installer.extract_checkout(self.folder/'source',release,None,execute)
        self.assertFalse((self.folder/'outside').exists())
