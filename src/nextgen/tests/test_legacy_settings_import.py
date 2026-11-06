import json
import tempfile
import unittest
from pathlib import Path
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.secrets import SecretStore
from src.nextgen.tests.test_installer import load, ROOT
converter=load('legacy_settings_converter',ROOT/'fixes/migrate_legacy_settings.py')

class LegacySettingsImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.old=self.root/'legacy';self.old.mkdir(mode=0o700);self.old.chmod(0o700)
        self.output=self.root/'native';self.database=self.root/'nextgen.sqlite'
        self.settings={'daemon':{'database_path':str(self.root/'legacy.db'),'concurrency':2},
            'web':{'host':'0.0.0.0','port':8877},
            'storage':{'download_root':'/media/downloads','user_evaluation_root':'/media/staging'},
            'sonarr':{'enabled':True,'base_url':'http://sonarr.example','root_folder':'/media/library','quality_profile_id':4},
            'jellyfin':{'enabled':False,'base_url':'http://jellyfin.example'},
            'shoko':{'enabled':True,'base_url':'http://shoko.example'},
            'anidb':{'client':'registeredclient','clientver':2},
            'qbittorrent':{'base_url':'http://qb.example','username':'user'},
            'user_evaluation':{'max_episode_mb':700,'max_six_episode_mb':3500},'ntfy':{'topic':'old-topic'}}
        self.secrets={'tvdb_api_key':'private-tvdb','sonarr_api_key':'private-sonarr','qbittorrent_password':'private-pass',
            'web_token':'w'*43,'admin_password':'old-admin','ntfy_token':'private-ntfy'}
        self.write()

    def write(self):
        for name,value in [('settings',self.settings),('secrets',self.secrets)]:
            (self.old/(name+'.json')).write_text(json.dumps(value))

    def test_preview_has_no_values_or_side_effects(self):
        before={p.name:p.read_bytes() for p in self.old.iterdir()}
        report=converter.migrate(self.old,self.output,self.database)
        self.assertFalse(report['changed']);self.assertFalse(self.output.exists());self.assertFalse(self.database.exists())
        raw=json.dumps(report)
        for value in ('private-tvdb','private-pass','old-admin','old-topic','w'*43):self.assertNotIn(value,raw)
        self.assertIn('ntfy.topic',report['retained_legacy_settings'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.old.iterdir()})

    def test_apply_native_settings_private_credentials_and_fresh_database_identity(self):
        original=(self.old/'settings.json').read_bytes()
        report=converter.migrate(self.old,self.output,self.database,apply=True)
        snapshot=ConfigurationStore(self.output).read();private=SecretStore(self.output)
        self.assertTrue(report['changed']);self.assertEqual(snapshot.value('storage.database_path'),str(self.database))
        self.assertEqual(snapshot.value('api.host'),'127.0.0.1');self.assertEqual(snapshot.value('api.port'),8877)
        self.assertEqual(snapshot.value('worker.concurrency'),2);self.assertEqual(snapshot.value('sonarr.quality_profile_id'),4)
        self.assertEqual(snapshot.value('storage.keep_root'),'/media/library')
        self.assertEqual(snapshot.value('jellyfin.base_url'),'');self.assertEqual(snapshot.value('shoko.base_url'),'http://shoko.example')
        self.assertEqual(private.tvdb_credentials().api_key,'private-tvdb')
        self.assertEqual(private.qbittorrent_credentials().password,'private-pass')
        self.assertEqual(private.access_credentials().webhook_token,'w'*43)
        self.assertFalse(private.status().management_configured)
        self.assertEqual((self.output/'secrets.json').stat().st_mode&0o777,0o600)
        self.assertFalse(self.database.exists());self.assertEqual((self.old/'settings.json').read_bytes(),original)

    def test_rerun_preserves_later_native_edits_and_credentials(self):
        converter.migrate(self.old,self.output,self.database,apply=True)
        store=ConfigurationStore(self.output);store.update({'worker':{'concurrency':7}},expected_revision=1)
        private=SecretStore(self.output);private.update_tvdb_api_key('new-private',expected_revision=1)
        report=converter.migrate(self.old,self.output,self.database,apply=True)
        self.assertTrue(report['already_imported']);self.assertFalse(report['changed'])
        self.assertEqual(store.read().value('worker.concurrency'),7);self.assertEqual(private.tvdb_credentials().api_key,'new-private')

    def test_existing_unrelated_output_and_changed_inputs_are_not_overwritten(self):
        self.output.mkdir(mode=0o700);self.output.chmod(0o700);(self.output/'settings.json').write_text('keep')
        with self.assertRaises(ValueError):converter.migrate(self.old,self.output,self.database,apply=True)
        self.assertEqual((self.output/'settings.json').read_text(),'keep')
        (self.output/'settings.json').unlink();self.output.rmdir()
        converter.migrate(self.old,self.output,self.database,apply=True)
        self.settings['daemon']['concurrency']=6;self.write()
        with self.assertRaises(ValueError):converter.migrate(self.old,self.output,self.database,apply=True)

    def test_incompatible_values_require_explicit_overrides(self):
        self.settings['qbittorrent']['base_url']='http://qb.example/subpath';self.write()
        with self.assertRaises(ValueError):converter.plan(self.old,self.database)
        report=converter.migrate(self.old,self.output,self.database,apply=True,
            overrides={'qbittorrent':{'base_url':'http://qb.example'}})
        self.assertTrue(report['changed'])

    def test_fingerprint_fence_symlink_and_in_place_import_refused(self):
        with self.assertRaises(ValueError):converter.migrate(self.old,self.old,self.database,apply=True)
        with self.assertRaises(ValueError):converter.migrate(self.old,self.output,self.database,apply=True,expected_fingerprint='a'*64)
        original=self.old/'secrets.json';original.rename(self.old/'real.json');original.symlink_to(self.old/'real.json')
        with self.assertRaises(OSError):converter.migrate(self.old,self.output,self.database,apply=True)



class InPlaceTests(LegacySettingsImportTests):
    def test_in_place_backup_and_repeat(self):
        original=(self.old/'settings.json').read_bytes()
        result=converter.fix_in_place(self.old)
        self.assertTrue(result['changed'])
        self.assertEqual((Path(result['backup'])/'settings.json').read_bytes(),original)
        self.assertEqual(ConfigurationStore(self.old).read().value('worker.concurrency'),2)
        self.assertEqual(SecretStore(self.old).tvdb_credentials().api_key,'private-tvdb')
        before=(self.old/'settings.json').read_bytes()
        self.assertFalse(converter.fix_in_place(self.old)['changed'])
        self.assertEqual((self.old/'settings.json').read_bytes(),before)

    def test_interrupted_conversion_recovers(self):
        from unittest.mock import patch
        real=converter.replace_private
        calls=[]
        def interrupted(path,*args):
            calls.append(path)
            if len(calls)==2:raise OSError('simulated interruption')
            return real(path,*args)
        with patch.object(converter,'replace_private',side_effect=interrupted):
            with self.assertRaises(OSError):converter.fix_in_place(self.old)
        self.assertTrue(converter.fix_in_place(self.old)['changed'])
        self.assertEqual(SecretStore(self.old).tvdb_credentials().api_key,'private-tvdb')
