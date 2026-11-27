"""Offline dashboard assets, authenticated views and operation pagination; unrun."""
import json
import unittest

from src.anime.api.auth import COOKIE
from src.anime.storage.sqlite import connect, transaction
from src.anime.tests.test_api_v1 import ApiV1Tests, ADMIN, WEB, ORIGIN


class DashboardApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ApiV1Tests.asyncSetUp
    rows=ApiV1Tests.rows
    request=ApiV1Tests.request
    release=ApiV1Tests.release

    async def asset(self,path,*,method='GET',host='anime.example'):
        route,_,query=path.partition('?')
        scope={'type':'http','method':method,'path':route,'raw_path':route.encode(),
               'query_string':query.encode(),'headers':[(b'host',host.encode()),(b'content-length',b'0')]}
        output=[]
        async def receive():return {'type':'http.request','body':b'','more_body':False}
        async def send(message):output.append(message)
        await self.app(scope,receive,send)
        return output[0]['status'],output[1]['body'],dict(output[0]['headers'])

    async def test_recognized_episodes_are_scoped_to_each_verified_work(self):
        from src.anime.tests.test_recognized_seasons import RecognitionTests
        fixture=RecognitionTests()
        fixture.setUp()
        try:
            fixture.remember(1,1,number=1)
            fixture.remember(1,3,aid=1002,title='Second Cour',number=1)
            with connect(self.database) as target:
                fixture.db.backup(target)
        finally:
            fixture.doCleanups()
        status,result,_=await self.request('GET','/api/v1/recognized',token=ADMIN)
        self.assertEqual(status,200)
        self.assertEqual(len(result['data']['items']),2)
        self.assertEqual([item['verified_episode_count'] for item in result['data']['items']],[1,1])
        self.assertEqual([item['seasons'] for item in result['data']['items']],[[1],[1]])
        for aid,eid in ((1001,10011),(1002,10021)):
            status,result,_=await self.request('GET',f'/api/v1/recognized/{aid}/episodes',token=ADMIN)
            self.assertEqual(status,200)
            self.assertEqual(result['data']['anidb_id'],aid)
            self.assertEqual([row['anidb_episode_id'] for row in result['data']['items']],[eid])
            self.assertEqual([row['tvdb_season'] for row in result['data']['items']],[1])
        status,_,_=await self.request('GET','/api/v1/recognized/9999/episodes',token=ADMIN)
        self.assertEqual(status,404)

    async def test_public_shell_and_assets_contain_no_private_data_and_have_strict_csp(self):
        await self.request('POST','/api/v1/webhook',self.release(),token=WEB)
        for route,mime in [('/','text/html'),('/running','text/html'),('/settings','text/html'),
                           ('/assets/dashboard.css','text/css'),('/assets/dashboard.js','text/javascript')]:
            status,body,headers=await self.asset(route)
            self.assertEqual(status,200)
            self.assertTrue(headers[b'content-type'].decode().startswith(mime))
            self.assertEqual(headers[b'cache-control'],b'no-store')
            csp=headers[b'content-security-policy'].decode()
            self.assertIn("script-src 'self'",csp)
            self.assertIn("frame-ancestors 'none'",csp)
            self.assertNotIn('unsafe-inline',csp)
            for private in (ADMIN,WEB,'Test Anime - 10'):
                self.assertNotIn(private.encode(),body)
        self.assertEqual(self.rows('SELECT json_array_length(torrents_json) FROM series'),[(1,)])

    async def test_dashboard_works_on_other_valid_request_hosts(self):
        status,body,headers=await self.asset('/',host='wrong.example')
        self.assertEqual(status,200)
        self.assertTrue(headers[b'content-type'].startswith(b'text/html'))
        self.assertIn(b'/assets/dashboard.js',body)
        self.assertNotIn(b'API_ORIGIN_INVALID',body)
        self.assertEqual(headers[b'cache-control'],b'no-store')
        for path in ('/running','/settings','/recognized'):
            status,body,headers=await self.asset(path,host='wrong.example')
            self.assertEqual(status,200)
            self.assertTrue(headers[b'content-type'].startswith(b'text/html'))
        status,body,headers=await self.asset('/assets/dashboard.js',host='wrong.example')
        self.assertEqual(status,200)
        self.assertTrue(headers[b'content-type'].startswith(b'text/javascript'))

    async def test_asset_allowlist_rejects_mutation_query_paths_and_untrusted_host(self):
        for route,method,host,expected in [('/assets/../secrets.json','GET','anime.example',404),
                ('/assets/secrets.json','GET','anime.example',404),('/','POST','anime.example',405),
                ('/?token=secret','GET','anime.example',400),('/','GET','evil/example',400)]:
            status,_,_=await self.asset(route,method=method,host=host)
            self.assertEqual(status,expected)

    async def test_credentials_reveal_requires_session_csrf_and_never_exposes_admin_password(self):
        current = self.app.secrets.status()
        self.app.secrets.update_sonarr_api_key('integration-test-sonarr-key', expected_revision=current.revision)
        status, summary, _ = await self.request('GET', '/api/v1/credentials', token=ADMIN)
        self.assertEqual(status, 200)
        self.assertNotIn('integration-test-sonarr-key', json.dumps(summary))
        status, _, _ = await self.request('POST', '/api/v1/credentials/sonarr/reveal', {}, token=WEB)
        self.assertEqual(status, 401)
        status, session, headers = await self.request('POST', '/api/v1/auth/session',
            {'password': ADMIN}, headers={'Origin': ORIGIN})
        self.assertEqual(status, 201)
        cookie = headers[b'set-cookie'].decode().split(';', 1)[0]
        status, _, _ = await self.request('POST', '/api/v1/credentials/sonarr/reveal', {},
            headers={'Cookie': cookie, 'Origin': ORIGIN})
        self.assertEqual(status, 403)
        status, result, response_headers = await self.request('POST', '/api/v1/credentials/sonarr/reveal', {},
            headers={'Cookie': cookie, 'Origin': ORIGIN,
                     'X-CSRF-Token': session['data']['csrf_token']})
        self.assertEqual(status, 200)
        self.assertEqual(result['data'], {'api_key': 'integration-test-sonarr-key'})
        self.assertNotIn(ADMIN, json.dumps(result))
        self.assertEqual(response_headers[b'cache-control'], b'no-store')
        status, _, _ = await self.request('POST', '/api/v1/credentials/management/reveal', {},
            headers={'Cookie': cookie, 'Origin': ORIGIN,
                     'X-CSRF-Token': session['data']['csrf_token']})
        self.assertEqual(status, 404)










    def test_failed_detail_formatting_with_causes_and_blockers(self):
        """W-22/W-23/W-25: failed_detail shows cause, failed_check, file, and reservation blocker."""
        from src.anime.api.app import failed_detail
        # Location + file
        self.assertEqual(
            failed_detail({"failed_check": "src/anime/foo.py:10 bar", "failed_path": "/tmp/test.mkv"}),
            "Stopped at src/anime/foo.py:10 bar · file: /tmp/test.mkv"
        )
        # Location + cause + file
        self.assertEqual(
            failed_detail({"failed_check": "src/anime/foo.py:10 bar", "cause_reason_code": "TORRENT_TIMEOUT", "failed_path": "/tmp/test.mkv"}),
            "Stopped at src/anime/foo.py:10 bar (cause: TORRENT_TIMEOUT) · file: /tmp/test.mkv"
        )
        # Cause only
        self.assertEqual(
            failed_detail({"cause_reason_code": "TORRENT_TIMEOUT"}),
            "Caused by TORRENT_TIMEOUT"
        )
        # Reservation blocker
        self.assertEqual(
            failed_detail({"blocking_job_id": 105, "operation_id": 12}),
            "Waiting for reservation owned by Job #105 (operation #12)"
        )


