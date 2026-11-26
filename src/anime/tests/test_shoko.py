"""Offline optional AniDB metadata transport, queue boundaries and secrets; unrun."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import urllib.error

from src.anime.configuration_schema import ConfigurationSnapshot, CONVERSIONS, convert_document, ConfigurationError
from src.anime.job_contracts import Registry
from src.anime.jobs.enrich_shoko_job import registration
from src.anime.secrets import SecretStore
from src.anime.shoko import lookup, ShokoConfiguration, ShokoReview, ShokoUnavailable
from src.anime.storage.sqlite import connect, transaction
from src.anime.tests.test_acquisition_preparation import AcquisitionPreparationTests
from src.anime.tests.test_api_v1 import ApiV1Tests, ADMIN, WEB
from src.anime.tests.test_keep_sonarr import KeepSonarrTests
from src.anime.tests.test_stage_media_job import StageMediaJobTests
from src.anime.tests.test_torrent_transfer import TorrentTransferTests
from src.anime.tests.test_verify_media_job import VerifyMediaJobTests


class Response(io.BytesIO):
    def __init__(self,value=None,code=200,raw=None):
        super().__init__(json.dumps(value).encode() if raw is None else raw);self.code=code
    def getcode(self):return self.code


class ShokoTests(unittest.TestCase):
    def configured(self):
        config=ConfigurationSnapshot(1,{'shoko':{'base_url':'http://shoko.invalid'}})
        secrets=Mock();secrets.shoko_api_key.return_value='private-key'
        return config,secrets

    def test_exact_anidb_ids_and_header_only_read_transport(self):
        config,secrets=self.configured()
        send=Mock(side_effect=[Response({'ID':123,'Title':'Anime'}),Response({'ID':789,'Title':'Episode'})])
        result=lookup(config,secrets,123,789,transport=send)
        self.assertEqual(result,{'anidb_id':123,'anidb_episode_id':789,'title':'Anime','episode_title':'Episode'})
        for call,path in zip(send.call_args_list,['Series/AniDB/123','Episode/AniDB/789']):
            request,timeout=call.args
            self.assertEqual(request.full_url,'http://shoko.invalid/api/v3/'+path)
            self.assertEqual(request.method,'GET');self.assertIsNone(request.data)
            self.assertEqual(request.get_header('Apikey'),'private-key');self.assertEqual(timeout,30)
            self.assertNotIn('private-key',request.full_url)

    def test_auth_unavailable_and_missing_catalog_are_distinct(self):
        config,secrets=self.configured()
        for code,error in [(401,ShokoConfiguration),(403,ShokoConfiguration),(404,ShokoReview),
                           (429,ShokoUnavailable),(503,ShokoUnavailable),(302,ShokoReview)]:
            with self.subTest(code=code),self.assertRaises(error):lookup(config,secrets,123,789,transport=lambda *_:Response(code=code))
        with self.assertRaises(ShokoUnavailable):lookup(config,secrets,123,789,transport=Mock(side_effect=urllib.error.URLError('offline')))

    def test_mismatched_duplicate_oversized_nonfinite_and_invalid_titles_rejected(self):
        config,secrets=self.configured()
        for raw in [b'{"ID":124,"Title":"Wrong"}',b'{"ID":123,"ID":123,"Title":"Duplicate"}',
                    b'{"ID":123,"Title":"Bad","Rating":NaN}',b'x'*1048577,b'{"ID":true,"Title":"Bad"}',
                    json.dumps({'ID':123,'Title':'x'*501}).encode(),b'{"ID":123,"Title":""}']:
            with self.subTest(raw=raw[:50]),self.assertRaises(ShokoReview):lookup(config,secrets,123,789,transport=lambda *_:Response(raw=raw))

    def test_v9_upgrade_and_v5_credentials_preserve_existing_values(self):
        document={}
        for version in range(9):
            document=CONVERSIONS[version](document)
            self.assertNotIn('shoko',document['settings'])
        upgraded=convert_document(document)
        self.assertEqual((upgraded.schema_version,upgraded.revision),(17,17))
        self.assertEqual(upgraded.value('shoko.base_url'),'')
        with self.assertRaises(ConfigurationError):convert_document({**document,'settings':{**document['settings'],'shoko':{}}})
        with tempfile.TemporaryDirectory() as directory:
            store=SecretStore(directory);store.initialize('tvdb',sonarr_api_key='sonarr',jellyfin_api_key='jellyfin')
            self.assertEqual(json.loads((Path(directory)/'secrets.json').read_text())['schema_version'],9)
            status=store.update_shoko_api_key('private-shoko',expected_revision=1).status
            self.assertTrue(status.shoko_configured);self.assertNotIn('private-shoko',repr(status))
            store.update_tvdb_api_key('replacement',expected_revision=2)
            self.assertEqual(store.shoko_api_key(),'private-shoko');self.assertEqual(store.sonarr_api_key(),'sonarr')
            self.assertEqual(store.jellyfin_api_key(),'jellyfin')
            self.assertEqual(json.loads((Path(directory)/'secrets.json').read_text())['schema_version'],9)


class ShokoJobTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    queued=TorrentTransferTests.queued
    due=TorrentTransferTests.due
    ready=VerifyMediaJobTests.ready
    staged_ready=StageMediaJobTests.staged_ready
    prepared=KeepSonarrTests.prepared
    make_due=KeepSonarrTests.make_due

    async def test_keep_always_queues_the_shoko_link(self):
        # Owner, 2026-11-23: every Keep tells Shoko which episode the kept file is.
        worker,fake,_,_,_=await self.prepared();await worker.attempt()
        self.assertEqual(self.rows("SELECT json_extract(payload_json,'$.anidb_id') FROM jobs WHERE kind='LINK_SHOKO'"),
                         [(fake.selection.anidb_id,)])

    async def test_blank_address_adds_no_optional_job(self):
        worker,_,_,_,_=await self.prepared();await worker.attempt()
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='ENRICH_SHOKO_METADATA'"),[(0,)])


class ShokoCredentialApiTests(ApiV1Tests):
    async def test_shoko_replacement_is_management_only_and_presence_only(self):
        status,_,_=await self.request('PATCH','/api/v1/credentials/shoko',{'expected_revision':1,'api_key':'private-shoko'},token=WEB)
        self.assertEqual(status,401)
        status,data,_=await self.request('PATCH','/api/v1/credentials/shoko',{'expected_revision':1,'api_key':'private-shoko'},token=ADMIN)
        self.assertEqual(status,200);self.assertTrue(data['data']['status']['shoko_configured'])
        self.assertNotIn('private-shoko',json.dumps(data))
