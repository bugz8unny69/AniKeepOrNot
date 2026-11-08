"""Offline Sonarr transport, staged Keep and uncertain-response recovery; unrun."""
from functools import partial
import io
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
import urllib.error
from urllib.parse import urlsplit

from src.anime.jobs.keep_media_job import registration as keep
from src.anime.keep_service import Service
from src.anime.keep_selection import load_selection
from src.anime.secrets import SecretStore
from src.anime.sonarr import Client
from src.anime.storage.management_actions import ManagementActions
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.anime.storage.sqlite import connect, initialize, transaction
from src.anime.tests.test_stage_media_job import StageMediaJobTests
from src.anime.tests.test_verify_media_job import VerifyMediaJobTests
from src.anime.tests.test_torrent_transfer import TorrentTransferTests
from src.anime.tests.test_acquisition_preparation import AcquisitionPreparationTests
from src.anime.configuration_schema import CONVERSIONS, ConfigurationError, convert_document
from src.anime.tests.test_api_v1 import ApiV1Tests, ADMIN, WEB


class Response(io.BytesIO):
    def __init__(self,value):super().__init__(json.dumps(value).encode())
    def getcode(self):return 200


class FakeSonarr:
    def __init__(self):self.selection=None;self.root=None;self.present=True;self.imported=False;self.posts=[];self.payload=None;self.lose_import=False;self.lose_add=False;self.failed=False;self.wrong_destination=False;self.imported_size=None;self.monitored=None;self.puts=[];self.series_path=None
    def __call__(self,request,timeout):
        assert request.get_header('X-api-key')=='sonarr-private-key'
        path=urlsplit(request.full_url).path.removeprefix('/api/v3/')
        body=json.loads(request.data) if request.data is not None else None
        series={'id':31,'tvdbId':self.selection.tvdb_series_id,'path':self.series_path or str(self.root/'Series')}
        if self.monitored is not None:series.update(monitored=self.monitored,qualityProfileId=7,tags=[3])
        episode={'id':41,'seriesId':31,'tvdbId':self.selection.tvdb_episode_id,'seasonNumber':self.selection.tvdb_season,
                 'episodeNumber':self.selection.tvdb_episode,'hasFile':self.imported,'episodeFileId':51 if self.imported else 0}
        if body is not None:self.posts.append(path)
        if path=='system/status':return Response({'version':'4.0.16.1'})
        if path=='qualityprofile':return Response([{'id':1}])
        if path=='rootfolder':return Response([{'path':str(self.root)}])
        if path=='series/lookup':return Response([{'tvdbId':self.selection.tvdb_series_id,'title':'Test Anime'}])
        if path=='series':
            if body is not None:
                assert body['tvdbId']==self.selection.tvdb_series_id
                assert body['addOptions']['searchForMissingEpisodes'] is False
                assert body['addOptions']['monitor']=='all'
                self.present=True
                if self.lose_add:self.lose_add=False;raise urllib.error.URLError('lost reply')
                return Response(series)
            return Response([series] if self.present else [])
        if path=='episode':return Response([episode])
        if path=='series/31' and request.get_method()=='PUT':
            self.puts.append(body);self.monitored=body['monitored'];return Response(body)
        quality={'quality':{'id':1},'revision':{'version':1,'real':0,'isRepack':False}}
        if path=='manualimport':
            if body is None:return Response([{'path':self.selection.media_path,'quality':quality,'languages':[{'id':1,'name':'English'}]}])
            assert body[0]['episodeIds']==[41]
            return Response([{**body[0],'episodes':[episode],'rejections':[]}])
        if path=='command' and body is not None:
            assert body['importMode']=='copy' and len(body['files'])==1
            assert body['files'][0]['seriesId']==31 and body['files'][0]['episodeIds']==[41]
            self.payload=body
            if not self.failed:
                destination=self.root/'Series'/'episode.mkv';destination.parent.mkdir(exist_ok=True)
                shutil.copyfile(self.selection.media_path,destination);self.imported_size=destination.stat().st_size;self.imported=True
            if self.lose_import:self.lose_import=False;raise urllib.error.URLError('lost reply')
            return Response({'id':61})
        if path=='command':return Response([{'id':61,'body':self.payload}] if self.payload else [])
        if path=='command/61':return Response({'id':61,'status':'failed' if self.failed else 'completed'})
        if path=='episodefile/51':return Response({'id':51,'seriesId':31,'size':self.imported_size,
            'path':'/elsewhere/episode.mkv' if self.wrong_destination else str(self.root/'Series'/'episode.mkv')})
        raise AssertionError(path)


class KeepSonarrTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    queued=TorrentTransferTests.queued
    due=TorrentTransferTests.due
    ready=VerifyMediaJobTests.ready
    staged_ready=StageMediaJobTests.staged_ready

    async def prepared(self,**flags):
        fake=FakeSonarr()
        for name,value in flags.items():setattr(fake,name,value)
        secrets=SecretStore(self.root);secrets.initialize(sonarr_api_key='sonarr-private-key')
        factory=partial(Service,client_factory=lambda options,key:Client(options,key,transport=fake))
        worker=StageMediaJobTests.worker(self,additional=(keep(secrets,service_factory=factory),))
        staging,_=await self.staged_ready(worker);await worker.attempt()
        target=self.rows("SELECT job_id FROM jobs WHERE kind='STAGE_USER_EVALUATION_MEDIA'")[0][0]
        db=connect(self.database)
        try:fake.selection=load_selection(db,target)
        finally:db.close()
        fake.root=self.root/'permanent';fake.root.mkdir()
        current=self.store.read()
        self.store.update({'storage':{'keep_root':str(fake.root)},'sonarr':{'base_url':'http://sonarr.invalid','quality_profile_id':1}},expected_revision=current.revision)
        actions=ManagementActions(self.database);_,receipt,_=actions.submit(target,'keep')
        return worker,fake,actions,target,receipt['job_id']

    async def make_due(self,job):
        db=connect(self.database)
        try:
            with transaction(db):db.execute('UPDATE jobs SET available_at_ms=0 WHERE job_id=?',(job,))
        finally:db.close()

    async def test_keep_imports_one_tvdb_episode_and_reports_kept_without_removing_staging(self):
        worker,fake,actions,target,job=await self.prepared()
        await worker.attempt()
        self.assertEqual(self.rows('SELECT phase FROM keep_imports'),[('COMPLETE',)])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(fake.posts.count('command'),1)
        self.assertTrue(Path(fake.selection.media_path).is_file())
        self.assertTrue((fake.root/'Series'/'episode.mkv').is_file())
        _,again,_=actions.submit(target,'keep')
        self.assertEqual(again['job_id'],job);self.assertTrue(again['reused'])
        self.assertEqual(again['completion']['message'],'This episode is kept.')
        self.assertNotIn('sonarr-private-key',str(self.rows('SELECT * FROM keep_imports')))

    async def test_completed_keep_queues_refresh_in_an_independent_operation(self):
        worker,fake,actions,target,job=await self.prepared()
        current=self.store.read()
        self.store.update({'jellyfin':{'base_url':'http://jellyfin.invalid'}},expected_revision=current.revision)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(self.rows("SELECT state,created_by_job_id FROM jobs WHERE kind='REFRESH_JELLYFIN'"),[('PENDING',job)])
        self.assertEqual(self.rows("SELECT count(DISTINCT operation_id) FROM job_operation_members WHERE job_id=? OR job_id IN (SELECT job_id FROM jobs WHERE kind='REFRESH_JELLYFIN')",(job,)),[(2,)])
        _,again,_=actions.submit(target,'keep')
        self.assertTrue(again['reused'])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='REFRESH_JELLYFIN'"),[(1,)])

    async def test_lost_import_reply_reconciles_same_command_without_second_post(self):
        worker,fake,actions,target,job=await self.prepared(lose_import=True)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT phase FROM keep_imports'),[('IMPORT_UNCERTAIN',)])
        await self.make_due(job);await worker.attempt()
        self.assertEqual(self.rows('SELECT phase FROM keep_imports'),[('COMPLETE',)])
        self.assertEqual(fake.posts.count('command'),1)

    async def test_missing_sonarr_series_stops_without_creating_it(self):
        worker,fake,actions,target,job=await self.prepared(present=False)
        await worker.attempt()
        self.assertEqual(fake.posts,[])
        self.assertEqual(fake.puts,[])
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(job,)),
                         [('NEEDS_REVIEW','KEEP_IMPORT_REVIEW')])
        self.assertTrue(Path(fake.selection.media_path).is_file())

    async def test_keep_preserves_unmonitored_series_settings(self):
        worker,fake,actions,target,job=await self.prepared(monitored=False)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT phase FROM keep_imports'),[('COMPLETE',)])
        self.assertEqual(fake.puts,[])
        self.assertNotIn('series',fake.posts)

    async def test_keep_never_monitors_a_series_outside_the_library(self):
        worker,fake,actions,target,job=await self.prepared(monitored=False,series_path='/elsewhere/Series')
        await worker.attempt()
        self.assertEqual(fake.puts,[])
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(job,)),
                         [('NEEDS_REVIEW','KEEP_IMPORT_REVIEW')])

    async def test_keep_leaves_already_monitored_series_alone(self):
        worker,fake,actions,target,job=await self.prepared(monitored=True)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT phase FROM keep_imports'),[('COMPLETE',)])
        self.assertEqual(fake.puts,[])

    async def test_failed_command_preserves_source_and_does_not_get_reimported_on_resume(self):
        worker,fake,actions,target,job=await self.prepared(failed=True)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW','KEEP_IMPORT_REVIEW')])
        _,resumed,_=actions.submit(target,'keep');self.assertTrue(resumed['resumed'])
        await worker.attempt()
        self.assertEqual(fake.posts.count('command'),1)
        self.assertTrue(Path(fake.selection.media_path).is_file())

    async def test_completed_command_with_wrong_destination_cannot_report_kept(self):
        worker,fake,actions,target,job=await self.prepared(wrong_destination=True)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT phase FROM keep_imports'),[('IMPORT_UNCERTAIN',)])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW',)])

    async def test_modified_staged_file_stops_before_any_import(self):
        worker,fake,actions,target,job=await self.prepared()
        Path(fake.selection.media_path).write_bytes(b'changed')
        await worker.attempt()
        self.assertNotIn('command',fake.posts)
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('NEEDS_REVIEW',)])

    async def test_later_settings_do_not_retarget_an_uncertain_import(self):
        worker,fake,actions,target,job=await self.prepared(lose_import=True)
        await worker.attempt()
        snapshot=self.store.read()
        self.store.update({'sonarr':{'base_url':'http://different.invalid'},'storage':{'keep_root':str(self.root/'different')}},expected_revision=snapshot.revision)
        await self.make_due(job);await worker.attempt()
        saved=json.loads(self.rows('SELECT data_json FROM keep_imports')[0][0])
        self.assertEqual(saved['options']['base_url'],'http://sonarr.invalid')
        self.assertEqual(saved['options']['keep_root'],str(fake.root))
        self.assertEqual(fake.posts.count('command'),1)


class SonarrSettingsTests(unittest.TestCase):
    def test_v7_conversion_preserves_values_and_rejects_new_fields_in_old_formats(self):
        doc={}
        for version in range(7):
            doc=CONVERSIONS[version](doc)
            self.assertNotIn('sonarr',doc['settings'])
            with self.assertRaises(ConfigurationError):convert_document({**doc,'settings':{**doc['settings'],'sonarr':{'base_url':'http://sonarr.invalid'}}})
        upgraded=convert_document(doc)
        self.assertEqual(upgraded.schema_version,15)
        self.assertEqual(upgraded.revision,doc['revision']+8)
        self.assertEqual(upgraded.value('sonarr.base_url'),'')
        # The HTTP torrent metadata source was explicitly retired in v12, and v15
        # renamed "trial" to "user evaluation". Every other value is unchanged.
        current=upgraded.to_document()['settings']
        self.assertNotIn('torrent_metadata',current)
        renamed={'trial':'user_evaluation'}
        for section,value in doc['settings'].items():
            if section=='torrent_metadata':continue
            expected=dict(value)
            if section=='storage':expected['user_evaluation_root']=expected.pop('trial_root')
            self.assertEqual(current[renamed.get(section,section)],expected)


class SonarrCredentialApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ApiV1Tests.asyncSetUp
    request=ApiV1Tests.request

    async def test_separate_management_auth_and_secret_v4_presence_only(self):
        status,_,_=await self.request('PATCH','/api/v1/credentials/sonarr',{'expected_revision':1,'api_key':'private-sonarr-key'},token=WEB)
        self.assertEqual(status,401)
        status,data,_=await self.request('PATCH','/api/v1/credentials/sonarr',{'expected_revision':1,'api_key':'private-sonarr-key'},token=ADMIN)
        self.assertEqual(status,200);self.assertTrue(data['data']['status']['sonarr_configured'])
        self.assertNotIn('private-sonarr-key',json.dumps(data))
        self.assertEqual(self.secrets.sonarr_api_key(),'private-sonarr-key')
        self.assertEqual(json.loads((self.root/'secrets.json').read_text())['schema_version'],8)
        self.assertEqual(self.secrets.access_credentials().webhook_token,WEB)
        self.secrets.update_tvdb_api_key('tvdb-key',expected_revision=2)
        self.assertEqual(self.secrets.sonarr_api_key(),'private-sonarr-key')

    async def test_prowlarr_api_key_is_managed_like_other_integrations(self):
        status,data,_=await self.request('PATCH','/api/v1/credentials/prowlarr',{'expected_revision':1,'api_key':'private-prowlarr-key'},token=ADMIN)
        self.assertEqual(status,200);self.assertTrue(data['data']['status']['prowlarr_configured'])
        self.assertNotIn('private-prowlarr-key',json.dumps(data))
        self.assertEqual(self.secrets.prowlarr_api_key(),'private-prowlarr-key')
        self.assertEqual(self.secrets.reveal_integration('prowlarr'),{'api_key':'private-prowlarr-key'})


class KeepMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_existing_schema_and_matches_fresh(self):
        db=sqlite3.connect(':memory:');fresh=sqlite3.connect(':memory:')
        try:
            for script in SCRIPTS[:21]:db.executescript((SQL_DIR/script).read_text())
            before=db.execute('SELECT * FROM management_actions').fetchall()
            db.executescript((SQL_DIR/'022_keep_imports.sql').read_text())
            db.executescript((SQL_DIR / "023_reservation_release.sql").read_text())
            from src.anime.storage.migrations import SCRIPTS as current_scripts
            for name in current_scripts[23:]:db.executescript((SQL_DIR/name).read_text())
            initialize(fresh)
            self.assertEqual(_structure(db),_structure(fresh))
            self.assertEqual(db.execute('SELECT * FROM management_actions').fetchall(),before)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],len(current_scripts))
        finally:db.close();fresh.close()

    def test_wrong_version_and_partial_failure_roll_back(self):
        for version in (20,21,22):
            db=sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA user_version='+str(version))
                sql=(SQL_DIR/'022_keep_imports.sql').read_text()
                if version==21:sql=sql.replace('PRAGMA user_version=22;','SELECT * FROM missing;\nPRAGMA user_version=22;')
                with self.assertRaises(sqlite3.Error):db.executescript(sql)
                db.rollback()
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='keep_imports'").fetchone())
            finally:db.close()
