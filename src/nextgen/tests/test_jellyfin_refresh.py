"""Offline refresh transport, follow-up boundaries and secrets; authored/unrun."""
from functools import partial
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
import urllib.error

from src.nextgen.configuration_schema import ConfigurationSnapshot, CONVERSIONS, convert_document, ConfigurationError
from src.nextgen.jellyfin import request_refresh, RefreshConfiguration, RefreshUnavailable, RefreshReview
from src.nextgen.jobs.refresh_jellyfin_job import registration
from src.nextgen.secrets import SecretStore
from src.nextgen.tests.test_stage_media_job import StageMediaJobTests
from src.nextgen.tests.test_acquisition_preparation import AcquisitionPreparationTests
from src.nextgen.tests.test_verify_media_job import VerifyMediaJobTests
from src.nextgen.tests.test_torrent_transfer import TorrentTransferTests


class Response(io.BytesIO):
    def __init__(self,code):super().__init__(b'');self.code=code
    def getcode(self):return self.code


class RefreshTests(unittest.TestCase):
    def test_exact_post_and_acceptance_only(self):
        configuration=ConfigurationSnapshot(1,{'jellyfin':{'base_url':'http://jellyfin.invalid'}})
        secrets=Mock();secrets.jellyfin_api_key.return_value='private-key'
        send=Mock(return_value=Response(204))
        self.assertIsNone(request_refresh(configuration,secrets,transport=send))
        request,timeout=send.call_args.args
        self.assertEqual(request.full_url,'http://jellyfin.invalid/Library/Refresh')
        self.assertEqual(request.method,'POST');self.assertEqual(request.data,b'')
        self.assertEqual(request.get_header('X-emby-token'),'private-key')
        self.assertEqual(timeout,30)
        self.assertNotIn('private-key',request.full_url)

    def test_auth_transient_and_unexpected_results(self):
        configuration=ConfigurationSnapshot(1,{'jellyfin':{'base_url':'http://jellyfin.invalid'}})
        secrets=Mock();secrets.jellyfin_api_key.return_value='private-key'
        for code,error in ((401,RefreshConfiguration),(403,RefreshConfiguration),(429,RefreshUnavailable),
                           (503,RefreshUnavailable),(302,RefreshReview),(200,RefreshReview),(404,RefreshReview)):
            with self.subTest(code=code),self.assertRaises(error):
                request_refresh(configuration,secrets,transport=lambda *_:Response(code))
        with self.assertRaises(RefreshUnavailable):
            request_refresh(configuration,secrets,transport=Mock(side_effect=urllib.error.URLError('offline')))
        with self.assertRaises(RefreshConfiguration):request_refresh(ConfigurationSnapshot(1,{}),secrets)

    def test_configuration_history_and_private_key_replacement(self):
        doc={}
        for version in range(8):
            doc=CONVERSIONS[version](doc)
            self.assertNotIn('jellyfin',doc['settings'])
        current=convert_document(doc)
        self.assertEqual(current.schema_version,13)
        self.assertEqual(current.value('jellyfin.base_url'),'')
        with self.assertRaises(ConfigurationError):
            convert_document({**doc,'settings':{**doc['settings'],'jellyfin':{}}})
        with tempfile.TemporaryDirectory() as directory:
            store=SecretStore(directory);store.initialize(sonarr_api_key='sonarr-key')
            status=store.update_jellyfin_api_key('jellyfin-key',expected_revision=1).status
            self.assertTrue(status.jellyfin_configured)
            self.assertNotIn('jellyfin-key',repr(status))
            self.assertEqual(store.sonarr_api_key(),'sonarr-key')
            store.update_tvdb_api_key('tvdb-key',expected_revision=2)
            self.assertEqual(store.jellyfin_api_key(),'jellyfin-key')
            self.assertEqual(json.loads((Path(directory)/'secrets.json').read_text())['schema_version'],8)
            store.update_jellyfin_api_key('',expected_revision=3)
            self.assertFalse(store.status().jellyfin_configured)


class RefreshJobTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    queued=TorrentTransferTests.queued
    due=TorrentTransferTests.due
    ready=VerifyMediaJobTests.ready

    async def test_refresh_failure_does_not_undo_staging_and_retry_reuses_job(self):
        refresh=Mock(side_effect=[RefreshUnavailable(),None])
        worker=StageMediaJobTests.worker(self,additional=(registration(None,refresh=refresh),))
        await StageMediaJobTests.staged_ready(self,worker)
        current=self.store.read()
        self.store.update({'jellyfin':{'base_url':'http://jellyfin.invalid'}},expected_revision=current.revision)
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='STAGE_TRIAL_MEDIA'"),[('DONE',)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='REFRESH_JELLYFIN'"),[('PENDING',)])
        self.assertEqual(self.rows("SELECT count(*) FROM job_operations WHERE state='STAGED'"),[(1,)])
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='REFRESH_JELLYFIN'"),[('RETRY_WAIT',)])
        from src.nextgen.storage.sqlite import connect,transaction
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE jobs SET available_at_ms=0 WHERE kind='REFRESH_JELLYFIN'")
        finally:db.close()
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='REFRESH_JELLYFIN'"),[('DONE',)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='REFRESH_JELLYFIN'"),[(1,)])


class RefreshCredentialApiTests(unittest.IsolatedAsyncioTestCase):
    from src.nextgen.tests.test_api_v1 import ApiV1Tests
    asyncSetUp=ApiV1Tests.asyncSetUp
    request=ApiV1Tests.request

    async def test_private_key_route_requires_management_and_does_not_reveal_key(self):
        from src.nextgen.tests.test_api_v1 import ADMIN, WEB
        body={'expected_revision':1,'api_key':'private-jellyfin-key'}
        status,_,_=await self.request('PATCH','/api/v1/credentials/jellyfin',body,token=WEB)
        self.assertEqual(status,401)
        status,data,_=await self.request('PATCH','/api/v1/credentials/jellyfin',body,token=ADMIN)
        self.assertEqual(status,200)
        self.assertTrue(data['data']['status']['jellyfin_configured'])
        self.assertIn('reason',data['data'])
        self.assertNotIn('private-jellyfin-key',json.dumps(data))
        self.assertEqual(self.secrets.jellyfin_api_key(),'private-jellyfin-key')
        status,_,_=await self.request('PATCH','/api/v1/credentials/jellyfin',body,token=ADMIN)
        self.assertEqual(status,409)
