"""Transcode settings use the existing authenticated API and private secret store."""
import json
import unittest

from src.anime.tests import test_api_v1 as fixture


class TranscodeApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ApiV1Tests.asyncSetUp
    request = fixture.ApiV1Tests.request

    async def test_tokens_presence_only_and_revision_conflicts(self):
        body = {'expected_revision':1,'agent':'agent:8787','token':'private-transcode-token'}
        code, result, _ = await self.request('PATCH','/api/v1/credentials/transcode',body,token=fixture.ADMIN)
        self.assertEqual(code,200)
        self.assertEqual(result['data']['status']['transcode_agents'], ['http://agent:8787'])
        self.assertNotIn(body['token'],json.dumps(result))
        code, result, _ = await self.request('GET','/api/v1/credentials',token=fixture.ADMIN)
        self.assertNotIn(body['token'],json.dumps(result))
        code, _, _ = await self.request('PATCH','/api/v1/credentials/transcode',body,token=fixture.ADMIN)
        self.assertEqual(code,409)
        code, _, _ = await self.request('POST','/api/v1/credentials/transcode/reveal',{},token=fixture.ADMIN)
        self.assertEqual(code,404)

    async def test_service_bar_hidden_except_remote_and_agent_order_kept(self):
        for mode in ('off','local','remote'):
            current = self.store.read()
            code, _, _ = await self.request('PATCH','/api/v1/settings',{'expected_revision':current.revision,
                'settings':{'transcode':{'mode':mode,'agents':['second:8787','first:8787']}}},token=fixture.ADMIN)
            self.assertEqual(code,200)
            code, result, _ = await self.request('GET','/api/v1/services',token=fixture.ADMIN)
            self.assertEqual('transcode' in [s['service'] for s in result['data']['services']],mode=='remote')
        self.assertEqual(tuple(self.store.read().value('transcode.agents')),('http://second:8787','http://first:8787'))
