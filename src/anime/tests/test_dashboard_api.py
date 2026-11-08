"""Offline dashboard assets, authenticated views and operation pagination; unrun."""
import json
import unittest

from src.anime.api.auth import COOKIE
from src.anime.storage.sqlite import connect, transaction
from src.anime.tests.test_api_v1 import ApiV1Tests, ADMIN, WEB, ORIGIN
from src.anime.tests.test_management_actions import reserve_without_metadata


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
            fixture.remember(11,1)
            fixture.second_work()
            with connect(self.database) as target:
                fixture.db.backup(target)
        finally:
            fixture.doCleanups()
        status,result,_=await self.request('GET','/api/v1/recognized',token=ADMIN)
        self.assertEqual(status,200)
        self.assertEqual(len(result['data']['items']),2)
        self.assertEqual([item['verified_episode_count'] for item in result['data']['items']],[1,1])
        for aid,eid in ((1001,10011),(1002,10013)):
            status,result,_=await self.request('GET',f'/api/v1/recognized/2001/1/{aid}/episodes',token=ADMIN)
            self.assertEqual(status,200)
            self.assertEqual(result['data']['anidb_id'],aid)
            self.assertEqual([row['anidb_episode_id'] for row in result['data']['items']],[eid])
        status,_,_=await self.request('GET','/api/v1/recognized/2001/1/episodes',token=ADMIN)
        self.assertEqual(status,404)

    async def test_public_shell_and_assets_contain_no_private_data_and_have_strict_csp(self):
        await self.request('POST','/api/v1/webhook',self.release(),token=WEB)
        for route,mime in [('/','text/html'),('/jobs','text/html'),('/settings','text/html'),
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
        self.assertEqual(self.rows('SELECT count(*) FROM jobs'),[(1,)])

    async def test_dashboard_works_on_other_valid_request_hosts(self):
        status,body,headers=await self.asset('/',host='wrong.example')
        self.assertEqual(status,200)
        self.assertTrue(headers[b'content-type'].startswith(b'text/html'))
        self.assertIn(b'/assets/dashboard.js',body)
        self.assertNotIn(b'API_ORIGIN_INVALID',body)
        self.assertEqual(headers[b'cache-control'],b'no-store')
        for path in ('/jobs','/settings','/recognized'):
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

    async def test_matching_details_are_authenticated_durable_and_available_on_later_jobs(self):
        from src.anime.history import append_job_history
        from src.anime.matching import MatchingTrace
        from src.anime.storage.sqlite import enqueue_job
        status, receipt, _ = await self.request('POST', '/api/v1/webhook',
                                               self.release(), token=WEB)
        self.assertEqual(status, 202)
        job=receipt['data']['job_id']
        trace=MatchingTrace()
        trace.queries(['Higurashi When They Cry'], season=2)
        trace.check('release_title','passed')
        trace.check('season_scope','failed','unverified_sequel')
        with connect(self.database) as db, transaction(db):
            db.execute("UPDATE jobs SET state='NEEDS_REVIEW',attempt_count=1,"
                       "last_reason_code='IDENTITY_INCOMPLETE' WHERE job_id=?",(job,))
            append_job_history(db,job_id=job,event_code='NEEDS_REVIEW',
                               reason_code='IDENTITY_INCOMPLETE',component='worker',
                               occurred_at_ms=1000,details={'attempt':1,'matching':trace.data})
            followup=enqueue_job(db,kind='VERIFY_RELEASE',payload={'anidb_id':123},
                                 component='worker',event_id=receipt['data']['event_id'],
                                 created_by_job_id=job)
        for credential in (None,WEB):
            status,_,_=await self.request('GET',f'/api/v1/jobs/{job}/matching',token=credential)
            self.assertEqual(status,401)
        for requested in (job,followup):
            status,result,headers=await self.request('GET',f'/api/v1/jobs/{requested}/matching',token=ADMIN)
            self.assertEqual(status,200)
            data=result['data']
            self.assertTrue(data['available'])
            self.assertEqual(data['job_id'],job)
            self.assertEqual(data['requested_job_id'],requested)
            self.assertEqual(data['evidence']['season'],2)
            self.assertIn({'step':'season_scope','status':'failed','note':'unverified_sequel'},
                          data['evidence']['checks'])
            self.assertEqual(headers[b'cache-control'],b'no-store')
            self.assertNotIn(WEB,json.dumps(result))
            self.assertNotIn(ADMIN,json.dumps(result))
        for path,method,expected in [
            (f'/api/v1/jobs/{job}/matching?token=secret','GET',400),
            (f'/api/v1/jobs/{job}/matching','POST',405),
            ('/api/v1/jobs/999999/matching','GET',404)]:
            status,_,_=await self.request(method,path,{} if method=='POST' else None,token=ADMIN)
            self.assertEqual(status,expected)

    async def test_matching_details_report_no_recorded_evidence_for_legacy_attempts(self):
        _,receipt,_=await self.request('POST','/api/v1/webhook',self.release(),token=WEB)
        job=receipt['data']['job_id']
        status,result,_=await self.request('GET',f'/api/v1/jobs/{job}/matching',token=ADMIN)
        self.assertEqual(status,200)
        self.assertFalse(result['data']['available'])
        self.assertIsNone(result['data']['evidence'])

    async def test_intake_uses_management_session_and_presents_one_row_per_explicit_scope(self):
        evaluation,target=await reserve_without_metadata(self)
        for credential in (None,WEB):
            status,_,_=await self.request('GET','/api/v1/intake',token=credential)
            self.assertEqual(status,401)
        status,session,headers=await self.request('POST','/api/v1/auth/session',{'password':ADMIN},headers={'Origin':ORIGIN})
        self.assertEqual(status,201)
        cookie=headers[b'set-cookie'].decode().split(';',1)[0]
        self.assertTrue(cookie.startswith(COOKIE+'='))
        status,result,_=await self.request('GET','/api/v1/intake',headers={'Cookie':cookie})
        self.assertEqual(status,200)
        matching=[item for item in result['data']['items'] if item['job_id']==target]
        self.assertEqual(len(matching),1)
        self.assertNotIn(evaluation,[item['job_id'] for item in result['data']['items']])
        item=matching[0]
        self.assertEqual(item['release_name'],'Test Anime - 10')
        self.assertEqual(item['anime_title'],'Test Anime')
        self.assertEqual(item['acquisition_status']['message'],'Not staged')
        self.assertTrue(item['actions']['cancel']['enabled'])
        self.assertEqual(item['label']['message_key'],'handler.RESERVE_EPISODE.label')
        for private in ('raw_body','payload_json','lease_token','torrent_infohash',ADMIN,WEB):
            self.assertNotIn(private,json.dumps(item))
        status,_,_=await self.request('POST',f'/api/v1/jobs/{target}/cancel',{},headers={'Cookie':cookie,'Origin':ORIGIN})
        self.assertEqual(status,403)
        status,_,_=await self.request('POST',f'/api/v1/jobs/{target}/cancel',{},headers={
            'Cookie':cookie,'Origin':ORIGIN,'X-CSRF-Token':session['data']['csrf_token']})
        self.assertEqual(status,200)

    async def test_intake_newest_pagination_preserves_deliveries_and_excludes_control_scopes(self):
        targets=[]
        for _ in range(3):
            _,result,_=await self.request('POST','/api/v1/webhook',self.release(),token=WEB)
            targets.append(result['data']['job_id'])
        from src.anime.storage.job_queue import JobQueue
        JobQueue(self.database).claim()
        _,control,_=await self.request('POST',f'/api/v1/jobs/{targets[0]}/cancel',{},token=ADMIN)
        control_id=control['data']['job_id']
        status,page,_=await self.request('GET','/api/v1/intake?limit=2',token=ADMIN)
        self.assertEqual(status,200)
        self.assertEqual([r['job_id'] for r in page['data']['items']],list(reversed(targets[1:])))
        cursor=page['data']['next_before_id']
        _,last,_=await self.request('GET',f'/api/v1/intake?limit=2&before_id={cursor}',token=ADMIN)
        self.assertEqual([r['job_id'] for r in last['data']['items']],[targets[0]])
        self.assertIsNone(last['data']['next_before_id'])
        _,job,_=await self.request('GET',f'/api/v1/jobs/{control_id}',token=ADMIN)
        self.assertTrue(job['data']['is_management_action'])

    async def test_reverse_job_pagination_is_additive_and_rejects_mixed_cursors(self):
        for _ in range(3):await self.request('POST','/api/v1/webhook',self.release(),token=WEB)
        _,old,_=await self.request('GET','/api/v1/jobs?limit=2',token=ADMIN)
        self.assertEqual([r['job_id'] for r in old['data']['items']],[1,2])
        self.assertEqual(old['data']['next_after_id'],2)
        _,new,_=await self.request('GET','/api/v1/jobs?before_id=9223372036854775807&limit=2',token=ADMIN)
        self.assertEqual([r['job_id'] for r in new['data']['items']],[3,2])
        self.assertEqual(new['data']['next_before_id'],2)
        for route in ('/api/v1/jobs?before_id=3&after_id=0','/api/v1/jobs/1/history?before_id=3',
                      '/api/v1/intake?after_id=0','/api/v1/intake?before_id=0'):
            status,_,_=await self.request('GET',route,token=ADMIN)
            self.assertEqual(status,400)

    async def test_job_visibility_filter_uses_database_before_pagination(self):
        jobs=[]
        for index in range(7):
            _,created,_=await self.request('POST','/api/v1/webhook',
                self.release(release_name=f'Test Anime - {index}'),token=WEB)
            jobs.append(created['data']['job_id'])
        with connect(self.database) as db,transaction(db):
            db.execute("UPDATE jobs SET state='DONE' WHERE job_id IN (?,?)",(jobs[-1],jobs[-3]))
            db.execute("UPDATE jobs SET state='CANCELED' WHERE job_id=?",(jobs[-2],))
            db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state='COMPLETE',"
                       "reason_code='CANCELLATION_COMPLETE' WHERE operation_id="
                       "(SELECT operation_id FROM job_operation_members WHERE job_id=?)",(jobs[-2],))
        first=await self.request('GET','/api/v1/jobs?limit=2&before_id=9223372036854775807&show_completed=0&show_canceled=0',token=ADMIN)
        self.assertEqual(first[0],200)
        self.assertEqual([row['job_id'] for row in first[1]['data']['items']],[jobs[-4],jobs[-5]])
        cursor=first[1]['data']['next_before_id']
        second=await self.request('GET',
            f'/api/v1/jobs?limit=2&before_id={cursor}&show_completed=0&show_canceled=0',token=ADMIN)
        self.assertEqual(second[0],200)
        self.assertEqual([row['job_id'] for row in second[1]['data']['items']],[jobs[-6],jobs[-7]])
        self.assertIsNone(second[1]['data']['next_before_id'])
        done=await self.request('GET','/api/v1/jobs?before_id=9223372036854775807&state=DONE',token=ADMIN)
        self.assertEqual([row['job_id'] for row in done[1]['data']['items']],[jobs[-1],jobs[-3]])
        for route in ('/api/v1/jobs?state=WRONG', '/api/v1/jobs?show_completed=2',
                      '/api/v1/jobs?show_canceled=maybe',
                      '/api/v1/jobs/1/history?show_completed=0'):
            self.assertEqual((await self.request('GET',route,token=ADMIN))[0],400)

    async def test_related_jobs_filter_uses_verified_episode_reservations(self):
        evaluation, reservation_job = await reserve_without_metadata(self)
        aid, season, episode = self.rows(
            "SELECT anidb_id,tvdb_season,tvdb_episode FROM episode_reservations")[0]
        path = f'/api/v1/jobs?anidb_id={aid}&season={season}&episode={episode}'
        status, result, _ = await self.request('GET', path, token=ADMIN)
        self.assertEqual(status, 200)
        matched = {item['job_id']: item for item in result['data']['items']}
        self.assertIn(reservation_job, matched)
        self.assertIn(evaluation, matched)
        self.assertIn({'season': season, 'episode': episode},
                      matched[reservation_job]['episode_targets'])
        self.assertEqual(matched[reservation_job]['anime_title'], 'Test Anime')
        status, absent, _ = await self.request('GET',
            f'/api/v1/jobs?anidb_id={aid}&season={season}&episode={episode+1}', token=ADMIN)
        self.assertEqual(status, 200)
        self.assertNotIn(reservation_job, [item['job_id'] for item in absent['data']['items']])
        for invalid in ('/api/v1/jobs?anidb_id=1',
                        '/api/v1/jobs?anidb_id=1&season=1&episode=bad',
                        '/api/v1/jobs?anidb_id=1&season=-1&episode=1'):
            self.assertEqual((await self.request('GET', invalid, token=ADMIN))[0], 400)

    async def test_display_context_ignores_unknown_and_malformed_stored_declarations(self):
        _,result,_=await self.request('POST','/api/v1/webhook',self.release(),token=WEB)
        job=result['data']['job_id'];db=connect(self.database)
        try:
            for raw in (b'{broken',b'[]',b'{"token":"private-value","unknown":"private-value"}'):
                with transaction(db):db.execute('UPDATE incoming_events SET raw_body=?',(raw,))
                status,result,_=await self.request('GET',f'/api/v1/jobs/{job}',token=ADMIN)
                self.assertEqual(status,200)
                self.assertIsNone(result['data']['release_name'])
                self.assertNotIn('private-value',json.dumps(result))
        finally:db.close()

    async def test_backfill_discovery_episode_targets_resolve_fallback_and_retry_episodes(self):
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'test','{}',0)")
                db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,attempt_count,created_at_ms,updated_at_ms) "
                           "VALUES(1,1,'VERIFY_RELEASE','{}','DONE',0,1,0,0)")
                db.execute("INSERT INTO job_operations(operation_id,created_at_ms,updated_at_ms) VALUES(1,0,0)")
                db.execute("INSERT INTO job_operation_members(job_id,operation_id) VALUES(1,1)")
                db.execute("INSERT INTO identified_anime VALUES(50, 100, 'Test Series')")
                db.execute("INSERT INTO identified_episodes VALUES(101,50)")
                db.execute("INSERT INTO identity_results(result_id,event_id,anidb_id,schema_version,series_json,validated_at_ms) VALUES(1,1,50,2,'{}',0)")
                db.execute("INSERT INTO release_verifications VALUES(1,1,1,50,?,'DECLARED_SINGLE_EPISODE',1,101,100,2,1,1001,'official',0)", ('a'*64,))
                db.execute("INSERT INTO user_evaluation_plans VALUES(1,50,100,2,'official',1,1,6,1,1,0)")
                db.execute("INSERT INTO episode_release_selections VALUES(1,2,2,2,1002,? ,0)", ('b'*40,))
                jid_fb = db.execute(
                    "INSERT INTO jobs(event_id,kind,payload_json,state,available_at_ms,attempt_count,created_at_ms,updated_at_ms) "
                    "VALUES(1,'DISCOVER_EPISODE_RELEASES','{\"plan_id\":1,\"start\":1,\"fallback_episode\":2}','NEEDS_REVIEW',0,1,0,0)"
                ).lastrowid
                db.execute("INSERT INTO job_operation_members(job_id,operation_id) VALUES(?,1)", (jid_fb,))
                jid_rt = db.execute(
                    "INSERT INTO jobs(event_id,kind,payload_json,state,available_at_ms,attempt_count,created_at_ms,updated_at_ms) "
                    "VALUES(1,'DISCOVER_EPISODE_RELEASES','{\"plan_id\":1,\"start\":1,\"retry_episode\":1}','NEEDS_REVIEW',0,1,0,0)"
                ).lastrowid
                db.execute("INSERT INTO job_operation_members(job_id,operation_id) VALUES(?,1)", (jid_rt,))
            status, res_fb, _ = await self.request('GET', f'/api/v1/jobs/{jid_fb}', token=ADMIN)
            self.assertEqual(status, 200)
            self.assertEqual(res_fb['data']['episode_targets'], [{'season': 2, 'episode': 2}])

            status, res_rt, _ = await self.request('GET', f'/api/v1/jobs/{jid_rt}', token=ADMIN)
            self.assertEqual(status, 200)
            self.assertEqual(res_rt['data']['episode_targets'], [{'season': 2, 'episode': 1}])
        finally:
            db.close()

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

    async def test_matching_endpoint_returns_release_name(self):
        """W-25: Matching endpoint surfaces original release name for failed identity."""
        _, res, _ = await self.request('POST', '/api/v1/webhook', self.release(), token=WEB)
        job = res['data']['job_id']
        status, match_data, _ = await self.request('GET', f'/api/v1/jobs/{job}/matching', token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(match_data['data']['release_name'], 'Test Anime - 10')

