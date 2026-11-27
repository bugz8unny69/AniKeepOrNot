"""Offline ASGI requests with temporary files/SQLite and fake cleanup; unexecuted."""
import json
import unittest
from src.anime.api.app import Application, MAX_BODY
from src.anime.api.auth import COOKIE
from src.anime.secrets import SecretStore
from src.anime.tests import support as fixture

WEB='w'*43
ADMIN='m'*43
ORIGIN='https://anime.example'


IDENTITY={'schema_version':2,'anime':{'anidb_id':1001,'tvdb_series_id':2002,'title':'Test Anime'},'episodes':[]}


class InstantBridge:
    """Tests: identify each queued release at once, as the API's identity process would."""
    def __init__(self,test):self.test=test;self.created={}
    def wake(self):
        import logging,sqlite3
        from src.anime.identity_bridge import apply_result
        db=sqlite3.connect(self.test.database)
        try:waiting=[row[0] for row in db.execute('SELECT request_id FROM identity_requests WHERE outcome IS NULL ORDER BY request_id')]
        finally:db.close()
        for request_id in waiting:
            series=getattr(self.test,'identity',IDENTITY)
            if callable(series):   # Per release: identity(release_name) -> series.
                db=sqlite3.connect(self.test.database)
                try:raw=db.execute('SELECT raw_body FROM identity_requests WHERE request_id=?',(request_id,)).fetchone()[0]
                finally:db.close()
                series=series(json.loads(raw)['release_name'])
            if series is None:continue   # Left queued: still being identified.
            apply_result(self.test.database,{'request_id':request_id,'result':'identified',
                                             'series':series},logging.getLogger('test.bridge'))
            self.created[request_id]=series['anime']['anidb_id']     # The series it woke.
    async def run(self):pass
    async def close(self):pass


class ApiV1Tests(unittest.IsolatedAsyncioTestCase):
    identity=IDENTITY
    rows=fixture.DatabaseSetup.rows
    async def asyncSetUp(self):
        await fixture.DatabaseSetup.asyncSetUp(self)
        self.secrets=SecretStore(self.root)
        self.secrets.initialize(webhook_token=WEB)
        self.calls=[]
        self.app=Application(self.root,origin=ORIGIN)
        self.app.bridge=InstantBridge(self)
        self.app.passwords.replace(ADMIN,expected_revision=0)
        session,csrf=self.app.sessions.create(self.app.passwords.read()[1])
        self.admin_cookie=COOKIE+'='+session
        self.admin_csrf=csrf
        self.addCleanup(self.app.close)

    async def request(self,method,path,body=None,*,token=None,headers=None,raw=None,scope_changes=None):
        route,_,query=path.partition('?')
        raw=(b'' if body is None else json.dumps(body).encode()) if raw is None else raw
        base=[(b'host',b'anime.example'),(b'content-length',str(len(raw)).encode())]
        if raw:base.append((b'content-type',b'application/json'))
        if token==ADMIN:
            session_headers={'cookie':self.admin_cookie}
            if method!='GET':session_headers.update({'origin':ORIGIN,'x-csrf-token':self.admin_csrf})
            headers={**session_headers,**(headers or {})};token=None
        if token is not None:base.append((b'authorization',('Bearer '+token).encode()))
        base.extend((k.lower().encode(),v.encode()) for k,v in (headers or {}).items())
        scope={'type':'http','http_version':'1.1','method':method,'path':route,'raw_path':route.encode(),
               'query_string':query.encode(),'headers':base,'client':('127.0.0.1',123)}
        scope.update(scope_changes or {})
        async def receive():return {'type':'http.request','body':raw,'more_body':False}
        output=[]
        async def send(message):output.append(message)
        await self.app(scope,receive,send)
        result=json.loads(output[1]['body'])
        # Tests: the series the (instant) identity bridge woke for the delivery.
        created=getattr(self.app.bridge,'created',{})
        if type(result.get('data')) is dict and result['data'].get('request_id') in created:
            result['data'].update(anidb_id=created[result['data']['request_id']])
        return output[0]['status'],result,dict(output[0]['headers'])

    def release(self,**extra):return {'release_name':'Test Anime - 10','torrent_infohash':'a'*40,**extra}

    async def test_reverse_proxy_hostname_is_not_bound_to_local_api_origin(self):
        # Simulate HTTPS Nginx forwarding an externally addressed request.
        host=b'anime.proxy.example'
        headers=[(b'host',host),(b'origin',b'https://anime.proxy.example'),(b'content-length',b'0')]
        scope={'headers':headers}
        status,_,_=await self.request('GET','/api/v1/intake',token=ADMIN,scope_changes={'headers':[(b'host',host),(b'cookie',self.admin_cookie.encode()),(b'content-length',b'0')]})
        self.assertEqual(status,200)
        status,_,_=await self.request('POST','/api/v1/auth/session',{'password':ADMIN},
            scope_changes={'headers':[(b'host',host),(b'origin',b'https://anime.proxy.example'),
                (b'content-type',b'application/json'),
                (b'content-length',str(len(json.dumps({'password':ADMIN}).encode())).encode())]})
        self.assertEqual(status,201)
        status,_,_=await self.request('POST','/api/v1/auth/session',{'password':ADMIN},
            scope_changes={'headers':[(b'host',host),(b'origin',b'https://evil.example'),
                (b'content-type',b'application/json'),
                (b'content-length',str(len(json.dumps({'password':ADMIN}).encode())).encode())]})
        self.assertEqual(status,403)

    async def test_webhook_body_bearer_and_query_auth_commit_sanitized_intake(self):
        attempts=[('/api/v1/webhook',self.release(token=WEB,source='untrusted-feed',tvdb_id=123),None),
                  ('/api/v1/webhook',self.release(torrent_infohash='b'*40),WEB),
                  ('/api/v1/webhook?token='+WEB,self.release(torrent_infohash='c'*40),None)]
        for path,body,token in attempts:
            status,result,headers=await self.request('POST',path,body,token=token)
            self.assertEqual(status,202)
            self.assertTrue(result['data']['request_id'])
        for (raw,) in self.rows('SELECT raw_body FROM identity_requests'):
            self.assertEqual(set(json.loads(raw)),{'release_name','torrent_infohash'})
            self.assertNotIn(WEB.encode(),raw)
        # The API identified each release and gave it to its series (one row), never a job.
        self.assertEqual(self.rows("SELECT json_array_length(torrents_json) FROM series"),[(3,)])
        # Each release keeps only its answer, for Autobrr's retries and its later action.
        self.assertEqual(self.rows('SELECT outcome FROM identity_requests'),[('accepted',)]*3)

    async def test_check_asks_autobrr_to_retry_until_anidb_answers_then_the_action_adds_nothing(self):
        self.identity=None    # Not known locally: the identity process must ask AniDB first.
        status,result,headers=await self.request('POST','/api/v1/check',self.release(token=WEB))
        self.assertEqual((status,result['data']['reason_code'],headers[b'retry-after']),(503,'IDENTITY_PENDING',b'15'))
        self.assertEqual(self.rows('SELECT count(*) FROM series'),[(0,)])
        self.identity=IDENTITY  # AniDB answered.
        await self.request('POST','/api/v1/check',self.release(token=WEB))
        status,result,_=await self.request('POST','/api/v1/check',self.release(token=WEB))
        self.assertEqual((status,result['data']['accepted']),(200,True))
        status,result,_=await self.request('POST','/api/v1/webhook',self.release(token=WEB))
        self.assertEqual((status,result['data']['accepted']),(200,True))
        self.assertEqual(self.rows("SELECT json_array_length(torrents_json) FROM series"),[(1,)])
        self.assertEqual(self.rows('SELECT count(*) FROM identity_requests'),[(1,)])

    async def test_check_rejects_with_why_once_identification_refuses_it(self):
        import logging
        from src.anime.identity_bridge import apply_result
        self.identity=None
        status,result,_=await self.request('POST','/api/v1/check',self.release(token=WEB))
        apply_result(self.database,{'request_id':result['data']['request_id'],'result':'rejected',
                                    'reason_code':'RELEASE_MOVIE_SKIPPED'},logging.getLogger('test'))
        status,result,_=await self.request('POST','/api/v1/check',self.release(token=WEB))
        self.assertEqual((status,result['data']['accepted'],result['data']['reason_code']),
                         (422,False,'RELEASE_MOVIE_SKIPPED'))
        self.assertEqual(self.rows("SELECT count(*) FROM notifications WHERE type='API_REJECTION'"),[(1,)])

    async def test_check_answers_a_known_release_at_once_from_local_data(self):
        from src.anime.storage import manual_identity
        release=self.release(release_name='Test Anime S01E10')
        manual_identity.save(self.database,json.dumps(release).encode(),anidb_id=1001,
                             tvdb_series_id=2002,title='Test Anime',season=1)
        self.identity=None    # The identity process is never needed.
        status,result,_=await self.request('POST','/api/v1/check',{**release,'token':WEB})
        self.assertEqual((status,result['data']['accepted']),(200,True))
        self.assertEqual(self.rows("SELECT anidb_id,json_array_length(torrents_json) FROM series"),[(1001,1)])

    async def test_a_program_error_answers_500_with_where_and_logs_the_full_trace(self):
        from unittest import mock
        def broken(*args,**kwargs):return {}['missing']
        with self.assertLogs(self.app.logger,'ERROR') as logs,\
             mock.patch('src.anime.api.app.webhook_decision',side_effect=broken):
            status,result,_=await self.request('POST','/api/v1/check',self.release(token=WEB))
        self.assertEqual((status,result['error']['code']),(500,'API_PROGRAM_ERROR'))
        self.assertIn('in broken (KeyError)',result['error']['message'])
        text='\n'.join(logs.output)
        self.assertIn('POST /api/v1/check failed with a program error',text)
        self.assertIn('Traceback (most recent call last)',text)
        self.assertNotIn(WEB,text)

    async def test_the_api_logs_what_it_did_with_a_release(self):
        self.identity=None
        with self.assertLogs(self.app.logger,'INFO') as logs:
            await self.request('POST','/api/v1/check',self.release(token=WEB))
        text='\n'.join(logs.output)
        self.assertIn('Test Anime - 10: new release, asked the identity process; waiting for AniDB',text)
        self.assertIn('POST /api/v1/check answered 503',text)
        self.assertNotIn(WEB,text)

    async def test_settings_lists_sonarr_quality_profiles_by_name(self):
        from unittest import mock
        rows=[{'id':1,'name':'Any'},{'id':8,'name':'Anime'},{'name':'no id'}]
        self.app.store.update({'sonarr':{'base_url':'http://sonarr.test:8989'}},
                              expected_revision=self.app.store.read().revision)
        with mock.patch.object(self.app.secrets,'sonarr_api_key',return_value='k'*32),\
             mock.patch('src.anime.sonarr.Client.request',return_value=rows) as asked:
            status,result,_=await self.request('GET','/api/v1/settings/sonarr/quality-profiles',token=ADMIN)
        self.assertEqual((status,result['data']['profiles']),(200,[{'id':1,'name':'Any'},{'id':8,'name':'Anime'}]))
        asked.assert_called_once_with('qualityprofile')
        status,_,_=await self.request('GET','/api/v1/settings/sonarr/quality-profiles')
        self.assertEqual(status,401)

    async def test_check_needs_the_webhook_token(self):
        status,_,_=await self.request('POST','/api/v1/check',self.release())
        self.assertEqual(status,401)
        self.assertEqual(self.rows('SELECT count(*) FROM identity_requests'),[(0,)])

    async def test_bad_conflicting_or_management_webhook_credentials_create_no_work(self):
        for path,body,token in [('/api/v1/webhook',self.release(),None),('/api/v1/webhook',self.release(),'n'*43),
             ('/api/v1/webhook',self.release(token=WEB),'n'*43),('/api/v1/webhook?token='+WEB+'&token='+WEB,self.release(),None)]:
            status,_,_=await self.request('POST',path,body,token=token)
            self.assertEqual(status,401)
        self.assertEqual(self.rows('SELECT count(*) FROM identity_requests'),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM series'),[(0,)])

    async def test_webhook_key_has_no_management_authority(self):
        for method,path,body in [('GET','/api/v1/settings',None),('GET','/api/v1/jobs',None),
                                  ('POST','/api/v1/jobs/1/cancel',{}),('POST','/api/v1/jobs/1/cleanup',{})]:
            status,_,_=await self.request(method,path,body,token=WEB)
            self.assertEqual(status,401)
        self.assertEqual(self.calls,[])

    async def test_cookie_mutations_require_origin_and_session_csrf_and_logout_revokes(self):
        await self.request('POST','/api/v1/webhook',self.release(),token=WEB)
        status,result,headers=await self.request('POST','/api/v1/auth/session',{'password':ADMIN},headers={'origin':ORIGIN})
        self.assertEqual(status,201)
        cookie=headers[b'set-cookie'].decode().split(';')[0]
        self.assertIn(b'HttpOnly',headers[b'set-cookie']);self.assertIn(b'Secure',headers[b'set-cookie'])
        csrf=result['data']['csrf_token']
        for extra in ({'cookie':cookie},{'cookie':cookie,'origin':ORIGIN},
                      {'cookie':cookie,'origin':'https://evil.example','x-csrf-token':csrf}):
            status,_,_=await self.request('POST','/api/v1/series/1001/cancel',{},headers=extra)
            self.assertEqual(status,403)
        status,_,_=await self.request('POST','/api/v1/series/1001/cancel',{},headers={'cookie':cookie,'origin':ORIGIN,'x-csrf-token':csrf})
        self.assertEqual(status,200)        # Past the session checks: the cancel is recorded.
        self.assertEqual(self.rows("SELECT json_type(work_json,'$.cancel') FROM series WHERE anidb_id=1001"),[('integer',)])
        status,_,_=await self.request('DELETE','/api/v1/auth/session',{},headers={'cookie':cookie,'origin':ORIGIN,'x-csrf-token':csrf})
        self.assertEqual(status,200)
        status,_,_=await self.request('GET','/api/v1/intake',headers={'cookie':cookie})
        self.assertEqual(status,401)

    async def test_webhook_rotation_does_not_revoke_independent_password_sessions(self):
        _,result,headers=await self.request('POST','/api/v1/auth/session',{'password':ADMIN},headers={'origin':ORIGIN})
        cookie=headers[b'set-cookie'].decode().split(';')[0]
        self.secrets.update_webhook_token('x'*43,expected_revision=1)
        status,_,_=await self.request('POST','/api/v1/webhook',self.release(token=WEB))
        self.assertEqual(status,401)
        status,_,_=await self.request('GET','/api/v1/intake',token=ADMIN)
        self.assertEqual(status,200)
        status,_,_=await self.request('GET','/api/v1/intake',headers={'cookie':cookie})
        self.assertEqual(status,200)
        status,_,_=await self.request('POST','/api/v1/webhook',self.release(token='x'*43))
        self.assertEqual(status,202)

    async def test_settings_revision_and_restart_fields_and_presence_only_credentials(self):
        status,result,_=await self.request('PATCH','/api/v1/settings',{'expected_revision':1,'settings':{'api':{'port':9876}}},token=ADMIN)
        self.assertEqual(status,200);self.assertIn('api.port',result['data']['restart_fields'])
        status,_,_=await self.request('PATCH','/api/v1/settings',{'expected_revision':1,'settings':{'logging':{'level':'DEBUG'}}},token=ADMIN)
        self.assertEqual(status,409)
        status,result,_=await self.request('GET','/api/v1/settings',token=ADMIN)
        self.assertEqual(result['data']['desired']['settings']['api']['port'],9876)
        self.assertEqual(result['data']['effective']['settings']['api']['port'],8765)
        self.assertEqual(status,200)
        status,result,_=await self.request('PATCH','/api/v1/credentials/tvdb',{'expected_revision':1,'api_key':'private-provider-value'},token=ADMIN)
        self.assertEqual(status,200);self.assertIn('reason',result['data']);self.assertNotIn('private-provider-value',json.dumps(result))
        status,result,_=await self.request('GET','/api/v1/credentials',token=ADMIN)
        self.assertEqual(status,200);self.assertFalse(result['data']['management_configured'])
        self.assertNotIn(ADMIN,json.dumps(result));self.assertNotIn(WEB,json.dumps(result))


    async def test_malformed_json_duplicate_headers_and_oversized_input_never_commit(self):
        for raw in (b'{"token":"x","token":"y"}',b'[]',b'{"x":NaN}',b'not-json'):
            status,_,_=await self.request('POST','/api/v1/webhook',raw=raw)
            self.assertEqual(status,400)
        status,_,_=await self.request('POST','/api/v1/webhook',raw=b'x'*(MAX_BODY+1))
        self.assertEqual(status,413)
        scope_headers=[(b'host',b'anime.example'),(b'authorization',('Bearer '+WEB).encode()),(b'authorization',('Bearer '+WEB).encode())]
        status,_,_=await self.request('POST','/api/v1/webhook',self.release(),scope_changes={'headers':scope_headers})
        self.assertEqual(status,400)
        status,_,_=await self.request('POST','/api/v1/webhook',self.release(token=WEB),scope_changes={'raw_path':b'/api%2fv1/webhook'})
        self.assertEqual(status,400)
        self.assertEqual(self.rows('SELECT count(*) FROM identity_requests'),[(0,)])

    async def test_invalid_origin_and_unreadable_auth_storage_have_no_fallback(self):
        status,_,_=await self.request('GET','/api/v1/jobs',token=ADMIN,headers={'origin':'https://evil.example'})
        self.assertEqual(status,403)
        (self.root/'secrets.json').chmod(0o644)
        status,result,_=await self.request('POST','/api/v1/webhook',self.release(token=WEB))
        self.assertEqual(status,503)
        self.assertEqual(result['error']['code'],'SECRET_STORAGE_UNAVAILABLE')
        self.assertEqual(self.rows('SELECT count(*) FROM identity_requests'),[(0,)])

    async def test_legacy_field_aliases_are_only_release_hints(self):
        body={'token':WEB,'torrentName':'Test Anime - 10','torrentHash':'A'*40,'anidb_id':123,
              'tvdb_id':456,'verified':True,'magnet':'magnet:?private_secret=value'}
        self.identity=None   # Still being identified: the stored release is only the cleaned hints.
        status,_,_=await self.request('POST','/api/v1/webhook',body)
        self.assertEqual(status,202)
        raw=self.rows('SELECT raw_body FROM identity_requests')[0][0]
        self.assertEqual(json.loads(raw),{'release_name':'Test Anime - 10','torrent_infohash':'a'*40})
        self.assertNotIn(b'private_secret',raw)
        self.assertEqual(self.rows('SELECT count(*) FROM series'),[(0,)])

    async def test_declared_credentials_inside_title_and_ambiguous_framing_are_rejected(self):
        status,_,_=await self.request('POST','/api/v1/webhook',self.release(token=WEB,release_name='Title '+WEB))
        self.assertEqual(status,400)
        status,_,_=await self.request('POST','/api/v1/webhook',self.release(token=WEB),headers={'transfer-encoding':'chunked'})
        self.assertEqual(status,400)  # Helper also supplies Content-Length.
        status,_,_=await self.request('GET','/api/v1/jobs',token=ADMIN,headers={'cookie':COOKIE+'='+'x'*43,'origin':'null'})
        self.assertEqual(status,403)
        self.assertEqual(self.rows('SELECT count(*) FROM identity_requests'),[(0,)])

    async def test_management_session_never_authenticates_webhook_and_logs_hide_keys(self):
        _,_,headers=await self.request('POST','/api/v1/auth/session',{'password':ADMIN},headers={'origin':ORIGIN})
        cookie=headers[b'set-cookie'].decode().split(';')[0]
        status,_,_=await self.request('POST','/api/v1/webhook',self.release(),headers={'cookie':cookie,'origin':ORIGIN})
        self.assertEqual(status,401)
        with self.assertLogs(self.app.logger,level='WARNING') as captured:
            status,_,_=await self.request('POST','/api/v1/webhook?token='+WEB,self.release(token=WEB),token='n'*43)
        self.assertEqual(status,401)
        logged='\n'.join(captured.output)
        self.assertNotIn(WEB,logged);self.assertNotIn(ADMIN,logged);self.assertNotIn('token=',logged)
        self.assertEqual(self.rows('SELECT count(*) FROM identity_requests'),[(0,)])
