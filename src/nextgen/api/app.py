"""Small versioned ASGI application. No worker execution or provider logic."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import logging
import os
import re
import uuid
from urllib.parse import parse_qs, urlsplit

from src.nextgen.async_io import blocking
from src.nextgen.systemd_notify import notify, watchdog_interval
from src.nextgen.api.password_store import PasswordStore, PasswordConflict, PasswordStorageError
from src.nextgen.api.passwords import verify_password
from src.nextgen.api.admin_socket import start_admin
from src.nextgen.api.auth import (ApiError, COOKIE, Sessions, bearer, matches, public_origin, check_origin, valid_host, webhook_key)
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationError, ConfigurationConflict, ConfigurationSnapshot, for_operation, field_definitions
from src.nextgen.cancellation import CancellationError
from src.nextgen.diagnostics import configure_logging, event, log_history
from src.nextgen.history import encode_details
from src.nextgen.matching import validated as validate_matching
from src.nextgen.maintenance import MaintenanceGate, MaintenanceError, MaintenancePending, MaintenanceBusy
from src.nextgen.messages import MessageCatalog
from src.nextgen.dashboard.assets import load_assets, address_mismatch, CSP as DASHBOARD_CSP
from src.nextgen.dashboard.presentation import HANDLERS
from src.nextgen.search import SearchInvalid, match as search_match, words as search_words
from src.nextgen.secrets import SecretStore
from src.nextgen.storage.job_queue import JobQueue, QueueUnavailable
from src.nextgen.storage.review_retry import request_review_retry, capability as retry_capability, RetryNotAllowed, RetryStale
from src.nextgen.storage.management_actions import ManagementActions, ActionUnavailable, ActionNotFound
from src.nextgen.storage.series_intake import (preferred_title, detail as series_detail, list_items as series_items,
    SERIES_STATUSES, keep_targets as series_keep_targets, retry_episode as retry_missing_episode, reject_series as reject_intake_series, IntakeUnavailable)
from src.nextgen.storage.migrations import LATEST_VERSION
from src.nextgen.storage.sqlite import accept_event_and_job

PREFIX='/api/v1'
MAX_BODY=65536
MAX_TORRENT_BODY=6*1024*1024  # base64 of a bounded 4 MiB BEP3 file
CATALOG=MessageCatalog(handlers=HANDLERS)


def object_json(raw):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:raise ApiError(400,'API_REQUEST_INVALID')
            result[k]=v
        return result
    try:
        result=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,
                          parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
        if type(result) is not dict:raise ValueError()
        return result
    except (UnicodeError,ValueError,TypeError,RecursionError):raise ApiError(400,'API_REQUEST_INVALID') from None


def exact(body,keys):
    if set(body)!=set(keys):raise ApiError(400,'API_REQUEST_INVALID')


def page_number(query,key,default,lower,upper):
    values=query.get(key,[str(default)])
    if len(values)!=1 or not re.fullmatch('[0-9]{1,19}',values[0]):raise ApiError(400,'API_REQUEST_INVALID')
    value=int(values[0])
    if not lower<=value<=upper:raise ApiError(400,'API_REQUEST_INVALID')
    return value


def validate_secret_fields(kind,body,fields):
    if any(type(body[k]) is not str or len(body[k])>4096 for k in fields):raise ApiError(400,'API_REQUEST_INVALID')
    try:
        if kind=='access':
            from src.nextgen.secrets import _access_values
            _access_values(body['webhook_token'],'')
        elif kind=='qbittorrent':
            from src.nextgen.acquisition.settings import DownloadCredentials
            if body['username'] or body['password']:DownloadCredentials(body['username'],body['password'])
        elif body['api_key']:
            from src.nextgen.providers.http import Credentials
            Credentials(body['api_key'])
    except (ValueError,RuntimeError):raise ApiError(400,'API_REQUEST_INVALID') from None


def normalize_webhook(body):
    def one(keys,*,lower=False):
        values=[body[k] for k in keys if k in body]
        if not values or any(type(v) is not str or not v.strip() for v in values):raise ApiError(400,'API_WEBHOOK_INVALID')
        values=[v.strip().lower() if lower else v.strip() for v in values]
        if len(set(values))!=1:raise ApiError(400,'API_WEBHOOK_INVALID')
        return values[0]
    title=one(('release_name','torrentName','name'))
    digest=one(('torrent_infohash','info_hash','torrentHash','hash'),lower=True)
    if (len(title.encode('utf-8'))>1024 or any(ord(c)<32 or ord(c)==127 for c in title)
            or not re.fullmatch('[a-f0-9]{40}',digest)):raise ApiError(400,'API_WEBHOOK_INVALID')
    declaration={'release_name':title,'torrent_infohash':digest}
    # Optional autobrr {{ .MagnetURI }}. It must bind the same hash; only
    # xt/dn and plain public announce trackers survive sanitization.
    # An unusable or mismatched magnet is dropped, never trusted; the
    # acquisition then resolves the bare info hash instead.
    from src.nextgen.acquisition.metadata import hash_magnet
    for key in ('magnet_uri','magnet','magnetUri','MagnetURI'):
        value=body.get(key)
        cleaned=hash_magnet(digest,value.strip()) if type(value) is str and value.strip() else None
        if cleaned is not None:
            declaration['magnet_uri']=cleaned
            break
    # Persist ONLY the normalized release declaration; no credential, torrent
    # URL, source label, raw payload or self-declared verified identity.
    return json.dumps(declaration,sort_keys=True).encode()


def size_detail(details):
    """The actual numbers behind a size-limit stop, in plain words."""
    video,limit,used=(details.get(k) for k in ('video_mb','limit_mb','plan_used_mb'))
    if type(video) is not int or type(limit) is not int:return None
    if used is None:return f'The video is {video:,} MB; the episode limit is {limit:,} MB.'
    return (f'The video is {video:,} MB; this anime\'s trial episodes already use {used:,} MB '
            f'of their {limit:,} MB total limit.')


def intake_status(query):
    value=query.get('status',[])
    if not value:return None
    if len(value)!=1 or value[0] not in SERIES_STATUSES:raise ApiError(400,'API_REQUEST_INVALID')
    return value[0]


def failed_detail(details):
    """Which check stopped the work and the file it was looking at."""
    check,path=details.get('failed_check'),details.get('failed_path')
    cause=details.get('cause_reason_code')
    blocker=details.get('blocking_job_id')
    op=details.get('operation_id')
    if blocker is not None:
        msg = f'Waiting for reservation owned by Job #{blocker}' + (f' (operation #{op})' if op is not None else '')
        return msg
    if type(check) is not str:
        if type(cause) is str:
            return f'Caused by {cause}'
        return None
    res = f'Stopped at {check}'
    if type(cause) is str:
        res += f' (cause: {cause})'
    if type(path) is str:
        res += f' · file: {path}'
    return res


class Application:
    def __init__(self,directory,*,origin=None,secrets=None,sessions=None):
        self.store=ConfigurationStore(directory);self.startup=self.store.read()
        self.secrets=SecretStore(directory) if secrets is None else secrets
        host=self.startup.value('api.host');port=self.startup.value('api.port')
        authority=('['+host+']' if ':' in host else host)+':'+str(port)
        self.origin=public_origin(origin or 'http://'+authority)
        self.authorities={urlsplit(self.origin).netloc,authority.lower()}
        self.sessions=sessions or Sessions()
        self.passwords=PasswordStore(directory)
        self.admin_server=None
        self.admin_path=os.environ.get('ANIME_DISCOVERY_ADMIN_SOCKET')
        self.assets=load_assets()
        self.pool=ThreadPoolExecutor(max_workers=8,thread_name_prefix='nextgen-api')
        self.slots=asyncio.Semaphore(8)
        self.logger=configure_logging(self.startup.logging_settings(),'api',logger=logging.Logger('api.request'))
        self.closed=False

    def close(self):
        self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=False)

    async def _watchdog(self, interval):
        """Heartbeat from the serving event loop, not a background OS thread."""
        while True:
            await asyncio.sleep(interval)
            if not self.closed:
                notify('WATCHDOG=1')

    async def _body(self,receive,limit=MAX_BODY):
        chunks=[];size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect':raise ApiError(400,'API_REQUEST_INVALID')
            if message['type']!='http.request':raise ApiError(400,'API_REQUEST_INVALID')
            chunk=message.get('body',b'')
            if type(chunk) is not bytes:raise ApiError(400,'API_REQUEST_INVALID')
            size+=len(chunk)
            if size>limit:raise ApiError(413,'API_BODY_TOO_LARGE')
            if chunk:chunks.append(chunk)
            if not message.get('more_body',False):return b''.join(chunks)

    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan':
            watchdog_task = None
            while True:
                message=await receive()
                if message['type']=='lifespan.startup':
                    try:
                        if self.admin_path:
                            self.admin_server=await start_admin(self.admin_path,self.passwords)
                    except Exception:
                        await send({'type':'lifespan.startup.failed','message':'Admin socket could not be started'})
                        return
                    await send({'type':'lifespan.startup.complete'})
                    interval = watchdog_interval()
                    if interval is not None:
                        watchdog_task = asyncio.create_task(self._watchdog(interval))
                elif message['type']=='lifespan.shutdown':
                    notify('STOPPING=1')
                    if self.admin_server is not None:
                        self.admin_server.close()
                        await self.admin_server.wait_closed()
                        try:os.unlink(self.admin_path)
                        except FileNotFoundError:pass  # Python 3.13 asyncio owns socket cleanup.
                    if watchdog_task is not None:
                        watchdog_task.cancel()
                        await asyncio.gather(watchdog_task, return_exceptions=True)
                    await blocking(self.close)
                    await send({'type':'lifespan.shutdown.complete'});return
            return
        if scope['type']=='websocket':
            await send({'type':'websocket.close','code':1008});return
        if scope['type']!='http':return
        request_id=uuid.uuid4().hex;extra=[]
        try:
            if self.closed:raise ApiError(503,'API_UNAVAILABLE')
            headers={}
            raw_headers=scope.get('headers',[])
            if len(raw_headers)>100 or sum(len(k)+len(v) for k,v in raw_headers)>16384:raise ApiError(400,'API_REQUEST_INVALID')
            for raw_key,raw_value in raw_headers:
                key=raw_key.decode('ascii').lower();value=raw_value.decode('latin-1')
                if key in headers or any(ord(c)<32 or ord(c)==127 for c in value):raise ApiError(400,'API_REQUEST_INVALID')
                headers[key]=value
            if not valid_host(headers.get('host','')):
                raise ApiError(400,'API_ORIGIN_INVALID')
            if 'content-encoding' in headers:raise ApiError(415,'API_MEDIA_TYPE_INVALID')
            if headers.get('transfer-encoding') not in (None,'chunked') or 'transfer-encoding' in headers and 'content-length' in headers:raise ApiError(400,'API_REQUEST_INVALID')
            raw_query=scope.get('query_string',b'')
            path=scope.get('path','')
            manual_upload=scope['method']=='POST' and bool(re.fullmatch(
                PREFIX+r'/intake/series/[1-9][0-9]{0,18}/episodes/[0-9]{1,4}/[1-9][0-9]{0,3}/submit',path))
            body_limit=MAX_TORRENT_BODY if manual_upload else MAX_BODY
            length=headers.get('content-length')
            if length is not None and (not re.fullmatch('[0-9]{1,8}',length) or int(length)>body_limit):raise ApiError(413,'API_BODY_TOO_LARGE')
            if len(raw_query)>2048 or len(path)>512 or '%' in path or not (path.startswith(PREFIX+'/') or path in self.assets):raise ApiError(404,'API_ROUTE_NOT_FOUND')
            if scope.get('raw_path',path.encode('utf-8'))!=path.encode('utf-8'):raise ApiError(400,'API_REQUEST_INVALID')
            query=parse_qs(raw_query.decode('ascii'),keep_blank_values=True,max_num_fields=16,strict_parsing=True) if raw_query else {}
            raw=await asyncio.wait_for(self._body(receive,body_limit),timeout=15)
            if length is not None and len(raw)!=int(length):raise ApiError(400,'API_REQUEST_INVALID')
            if raw and headers.get('content-type','').lower() not in ('application/json','application/json; charset=utf-8'):raise ApiError(415,'API_MEDIA_TYPE_INVALID')
            body=object_json(raw) if raw else {}
            if scope['method']=='GET' and raw:raise ApiError(400,'API_REQUEST_INVALID')
            if path in self.assets:
                if scope['method']!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query or raw:raise ApiError(400,'API_REQUEST_INVALID')
                mime,content=self.assets[path]
                await send({'type':'http.response.start','status':200,'headers':[
                    (b'content-type',mime.encode()),(b'content-length',str(len(content)).encode()),
                    (b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),
                    (b'referrer-policy',b'no-referrer'),(b'content-security-policy',DASHBOARD_CSP.encode()),
                    (b'x-request-id',request_id.encode())]})
                await send({'type':'http.response.body','body':content})
                return
            peer=str((scope.get('client') or ('local',0))[0])
            async with self.slots:
                status,data,extra=await blocking(self._dispatch,scope['method'],path,headers,query,body,peer,executor=self.pool)
            result={'api_version':'v1','request_id':request_id,'data':data}
        except asyncio.TimeoutError:
            status=408;result={'api_version':'v1','request_id':request_id,'error':CATALOG.reason('API_REQUEST_INVALID')}
        except ApiError as exc:
            status=exc.status;result={'api_version':'v1','request_id':request_id,'error':CATALOG.reason(exc.reason_code)}
            event(self.logger,logging.WARNING,'API_REQUEST_REJECTED',component='api',reason_code=exc.reason_code)
        except (UnicodeError,ValueError):
            status=400;result={'api_version':'v1','request_id':request_id,'error':CATALOG.reason('API_REQUEST_INVALID')}
        except Exception:
            # No request URL, header, body, exception text or ASGI scope is logged.
            status=503;result={'api_version':'v1','request_id':request_id,'error':CATALOG.reason('API_UNAVAILABLE')}
            event(self.logger,logging.ERROR,'API_REQUEST_REJECTED',component='api',reason_code='API_UNAVAILABLE')
        encoded=json.dumps(result,allow_nan=False,separators=(',',':')).encode()
        response_headers=[(b'content-type',b'application/json; charset=utf-8'),(b'cache-control',b'no-store'),
            (b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer'),
            (b'content-security-policy',b"default-src 'none'; frame-ancestors 'none'"),
            (b'x-request-id',request_id.encode()),(b'content-length',str(len(encoded)).encode()),*extra]
        if status in (429,503):response_headers.append((b'retry-after',b'60'))
        await send({'type':'http.response.start','status':status,'headers':response_headers})
        await send({'type':'http.response.body','body':encoded})

    def _dispatch(self,method,path,headers,query,body,peer):
        try:return self._route(method,path,headers,query,body,peer)
        except ApiError:raise
        except ActionNotFound as exc:raise ApiError(404,exc.reason_code) from None
        except ActionUnavailable as exc:raise ApiError(409,exc.reason_code) from None
        except IntakeUnavailable as exc:raise ApiError(409,exc.reason_code) from None
        except (RetryNotAllowed,RetryStale) as exc:raise ApiError(409,exc.reason_code) from None
        except ConfigurationConflict as exc:raise ApiError(409,exc.reason_code) from None
        except ConfigurationError as exc:raise ApiError(503,exc.reason_code) from None
        except (MaintenancePending,MaintenanceBusy,MaintenanceError) as exc:raise ApiError(503,exc.reason_code) from None
        except QueueUnavailable as exc:raise ApiError(503,exc.reason_code) from None
        except CancellationError as exc:raise ApiError(409,exc.reason_code) from None

    def _route(self,method,path,headers,query,body,peer):
        access=self.secrets.access_credentials()  # Fresh, fail closed. No fallback.
        if path==PREFIX+'/webhook':
            if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
            if set(query)-{'token'}:raise ApiError(400,'API_REQUEST_INVALID')
            webhook_key(headers,query,body,access.webhook_token)
            normalized=normalize_webhook(body)
            if any(key and key.encode() in normalized for key in (access.webhook_token,)):raise ApiError(400,'API_WEBHOOK_INVALID')
            desired=self.store.read();configuration=for_operation(desired,self.startup,component='api')
            configure_logging(configuration.logging_settings(),'api',logger=self.logger)
            with MaintenanceGate(configuration.value('storage.database_path')).operation():
                with JobQueue(configuration.value('storage.database_path')).connection() as db:
                    event_id,job_id=accept_event_and_job(db,'autobrr',normalized,kind='IDENTIFY_ANIME',payload={},component='api',logger=self.logger)
            return 202,{'event_id':event_id,'job_id':job_id},[]
        check_origin(headers,self.origin)
        try:
            password_revision, password_digest = self.passwords.read()
        except PasswordStorageError:
            raise ApiError(503,'API_AUTH_UNCONFIGURED') from None
        if password_digest is None:
            raise ApiError(503,'API_AUTH_UNCONFIGURED')
        if path==PREFIX+'/auth/session' and method=='POST':
            if query:raise ApiError(400,'API_REQUEST_INVALID')
            check_origin(headers,self.origin,required=True)
            self.sessions.attempt(peer);exact(body,('password',))
            if not verify_password(body['password'],password_digest):raise ApiError(401,'API_AUTH_REQUIRED')
            token,csrf=self.sessions.create(password_digest)
            event(self.logger,logging.INFO,'API_SESSION_CREATED',component='api')
            return 201,{'csrf_token':csrf,'expires_in':self.sessions.ttl},[(b'set-cookie',self._cookie(token,secure=headers.get('origin','').startswith('https://')).encode())]
        if 'authorization' in headers:
            raise ApiError(401,'API_AUTH_REQUIRED')
        check_origin(headers,self.origin,required=method!='GET')
        digest,session=self.sessions.require(headers,password_digest,mutation=method!='GET')
        if path==PREFIX+'/auth/session':
            if query:raise ApiError(400,'API_REQUEST_INVALID')
            if method=='GET':return 200,{'authenticated':True,'csrf_token':session.csrf},[]
            if method=='DELETE':
                exact(body,())
                self.sessions.remove(digest)
                event(self.logger,logging.INFO,'API_SESSION_CLOSED',component='api')
                return 200,{'signed_out':True},[(b'set-cookie',self._cookie('',clear=True,secure=headers.get('origin','').startswith('https://')).encode())]
            raise ApiError(405,'API_METHOD_INVALID')
        if path==PREFIX+'/auth/password':
            if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
            if query:raise ApiError(400,'API_REQUEST_INVALID')
            exact(body,('current_password','new_password'))
            if not verify_password(body['current_password'],password_digest):raise ApiError(401,'API_AUTH_REQUIRED')
            try:
                new_revision=self.passwords.replace(body['new_password'],expected_revision=password_revision)
            except PasswordConflict:
                raise ApiError(409,'API_AUTH_REQUIRED') from None
            except ValueError:
                raise ApiError(400,'API_REQUEST_INVALID') from None
            self.sessions.clear()
            return 200,{'password_changed':True,'revision':new_revision},[(b'set-cookie',self._cookie('',clear=True).encode())]
        if 'token' in query:raise ApiError(400,'API_REQUEST_INVALID')
        desired=self.store.read();configuration=for_operation(desired,self.startup,component='api')
        configure_logging(configuration.logging_settings(),'api',logger=self.logger)
        # Authentication and CSRF above apply equally to all explicit actions.
        # HTTP performs local admission only; network/filesystem cleanup is queued.
        with MaintenanceGate(configuration.value('storage.database_path')).operation():
            if path==PREFIX+'/jobs/bulk':
                if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                exact(body,('action','job_ids'))
                action,ids=body['action'],body['job_ids']
                if type(action) is not str or action not in ('cancel','retry','cleanup') or (
                        type(ids) is not list or not 6<=len(ids)<=100 or
                        any(type(job) is not int or not 1<=job<=(1<<63)-1 for job in ids) or
                        len(set(ids))!=len(ids)):
                    raise ApiError(400,'API_REQUEST_INVALID')
                queue=JobQueue(configuration.value('storage.database_path'))
                from src.nextgen.storage.sqlite import transaction,enqueue_job_with_receipt
                from src.nextgen.jobs.management_bulk_job import KIND
                with queue.connection() as db,transaction(db):
                    placeholders=','.join('?' for _ in ids)
                    found=db.execute('SELECT count(*) FROM jobs WHERE job_id IN ('+placeholders+')',ids).fetchone()[0]
                    if found!=len(ids):raise ApiError(400,'API_REQUEST_INVALID')
                    batch,audit=enqueue_job_with_receipt(db,kind=KIND,payload={'action':action,'job_ids':ids},component='api')
                log_history(self.logger,audit)
                location=f'/api/v1/jobs/{batch}'
                return 202,{'job_id':batch,'state':'PENDING','status_url':location,
                    'history_url':location+'/history','count':len(ids),
                    'notification':CATALOG.reason('MANAGEMENT_JOB_SUBMITTED')},[(b'location',location.encode())]
            match=re.fullmatch(PREFIX+r'/jobs/([1-9][0-9]{0,18})/(keep|cancel|cleanup|cancellation|retry)',path)
            if match:
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                job=int(match[1]);action=match[2]
                if job>(1<<63)-1:raise ApiError(400,'API_REQUEST_INVALID')
                if action=='retry' and method=='POST':
                    exact(body,('expected_attempt','expected_reason'))
                    receipt=request_review_retry(configuration.value('storage.database_path'),job,
                        expected_attempt=body['expected_attempt'],expected_reason=body['expected_reason'])
                    location=f'/api/v1/jobs/{job}'
                    return 202,{'job_id':job,'state':'PENDING','status_url':location,
                        'history_url':location+'/history','reused':receipt.outcome=='ALREADY_QUEUED',
                        'notification':CATALOG.reason('MANAGEMENT_JOB_SUBMITTED')},[(b'location',location.encode())]
                actions=ManagementActions(configuration.value('storage.database_path'))
                if action=='cancellation' and method=='GET':
                    with actions.queue.connection() as db:
                        db.execute('BEGIN')
                        row=db.execute('SELECT job_id,event_id,created_by_job_id,kind,state,activity_code,attempt_count,last_reason_code FROM jobs WHERE job_id=?',(job,)).fetchone()
                        if row is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                        data=self._job(actions.queue,db,row)
                        return 200,{**data['cancellation'],'actions':data['actions']},[]
                if action in ('keep','cancel','cleanup') and method=='POST':
                    exact(body,())
                    status,data,records=actions.submit(job,action)
                    for record in records:log_history(self.logger,record)
                    return status,data,([] if 'status_url' not in data else [(b'location',data['status_url'].encode())])
                raise ApiError(405,'API_METHOD_INVALID')
            coverage=re.fullmatch(PREFIX+r'/jobs/([1-9][0-9]{0,18})/coverage',path)
            if coverage:
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                job=int(coverage[1])
                if job>(1<<63)-1:raise ApiError(400,'API_REQUEST_INVALID')
                with JobQueue(configuration.value('storage.database_path')).connection() as db:
                    db.execute('BEGIN')
                    row=db.execute("SELECT state,attempt_count,last_reason_code FROM jobs "
                                   "WHERE job_id=? AND kind='VERIFY_RELEASE'",(job,)).fetchone()
                    if row is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                    proof=db.execute("SELECT batch_id,tvdb_season,info_hash FROM batch_verifications "
                                     "WHERE job_id=?",(job,)).fetchone()
                    if proof is not None:
                        plan=db.execute('SELECT plan_id,planned_size_bytes FROM batch_trial_plans '
                                        'WHERE batch_id=?',(proof[0],)).fetchone()
                        planned_count=0 if plan is None else db.execute(
                            'SELECT count(*) FROM batch_trial_files WHERE plan_id=?',
                            (plan[0],)).fetchone()[0]
                        total=db.execute("SELECT count(*) FROM batch_verified_files WHERE batch_id=?",
                                         (proof[0],)).fetchone()[0]
                        rows=db.execute("SELECT f.file_index,f.file_path,f.anidb_episode_number,"
                                        "f.tvdb_episode,f.file_size_bytes,"
                                        "CASE WHEN p.file_index IS NOT NULL THEN 1 ELSE 0 END "
                                        "FROM batch_verified_files f LEFT JOIN batch_trial_files p "
                                        "ON p.plan_id=? AND p.file_index=f.file_index "
                                        "WHERE f.batch_id=? ORDER BY f.tvdb_episode,f.file_index LIMIT 100",
                                        (None if plan is None else plan[0],proof[0])).fetchall()
                        evidence={'kind':'batch','tvdb_season':proof[1],'episode_count':total,
                                  'trial_plan_ready':plan is not None,
                                  'planned_episode_count':planned_count,
                                  'planned_size_bytes':None if plan is None else plan[1],
                                  'truncated':total>len(rows),'episodes':[
                            {'file_index':item[0],'path':item[1],
                             'anidb_episode':item[2],'tvdb_episode':item[3],
                             'size_bytes':item[4],'planned':bool(item[5])} for item in rows]}
                    else:
                        single=db.execute("SELECT tvdb_season,tvdb_episode,anidb_episode_number "
                                          "FROM release_verifications WHERE job_id=?",(job,)).fetchone()
                        evidence=None if single is None else {
                            'kind':'single','tvdb_season':single[0],'episode_count':1,
                            'truncated':False,'episodes':[{'file_index':None,'path':None,
                                'anidb_episode':single[2],'tvdb_episode':single[1],
                                'size_bytes':None}]}
                    return 200,{'job_id':job,'available':evidence is not None,
                                'state':row[0],'attempts':row[1],
                                'reason':None if row[2] is None else CATALOG.reason(row[2]),
                                'evidence':evidence},[]
            manual=re.fullmatch(PREFIX+r'/jobs/([1-9][0-9]{0,18})/manual-match',path)
            if manual:
                if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                exact(body,('anidb_id','tvdb_series_id','title','season'))
                from src.nextgen.storage.manual_identity import save, ManualMatchError
                try:
                    result=save(configuration.value('storage.database_path'),int(manual[1]),
                        anidb_id=body['anidb_id'],tvdb_series_id=body['tvdb_series_id'],
                        title=body['title'],season=body['season'])
                except ManualMatchError:
                    raise ApiError(409,'MANAGEMENT_ACTION_UNAVAILABLE') from None
                return 200,result,[]
            matching=re.fullmatch(PREFIX+r'/jobs/([1-9][0-9]{0,18})/matching',path)
            if matching:
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                job=int(matching[1])
                if job>(1<<63)-1:raise ApiError(400,'API_REQUEST_INVALID')
                with JobQueue(configuration.value('storage.database_path')).connection() as db:
                    db.execute('BEGIN')
                    row=db.execute('SELECT kind,state,attempt_count,last_reason_code,event_id FROM jobs WHERE job_id=?',(job,)).fetchone()
                    if row is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                    identity_job=job
                    if row[0]!='IDENTIFY_ANIME':
                        if row[4] is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                        source=db.execute("SELECT job_id,kind,state,attempt_count,last_reason_code,event_id FROM jobs "
                            "WHERE event_id=? AND kind='IDENTIFY_ANIME' ORDER BY job_id LIMIT 1",(row[4],)).fetchone()
                        if source is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                        identity_job=source[0]
                        row=source[1:]
                    evidence=None
                    # Review a bounded set of durable, fenced lifecycle outcomes.
                    # Older jobs legitimately have no trace and must not be guessed.
                    records=db.execute(
                        "SELECT details_json FROM job_history WHERE job_id=? AND "
                        "event_code IN ('NEEDS_REVIEW','DONE','RETRY_SCHEDULED','DEFERRED','ON_HOLD','BLOCKED') "
                        "ORDER BY history_id DESC LIMIT 64",(identity_job,)).fetchall()
                    for record in records:
                        try:
                            details=json.loads(record[0] or '{}')
                            if type(details) is not dict or 'matching' not in details:continue
                            validate_matching(details['matching'])
                            evidence=details['matching']
                            break
                        except (ValueError,TypeError,KeyError,OverflowError):
                            continue
                    release_name = None
                    if row[4] is not None:
                        event_row = db.execute("SELECT raw_body FROM incoming_events WHERE event_id=?", (row[4],)).fetchone()
                        if event_row and event_row[0]:
                            try:
                                decl = json.loads(event_row[0])
                                if type(decl) is dict and type(decl.get("release_name")) is str:
                                    release_name = decl["release_name"]
                            except (ValueError, TypeError, UnicodeError):
                                pass
                    candidate_evidence = []
                    try:
                        from src.nextgen.storage.candidate_evidence import load_candidate_evidence
                        candidate_evidence = load_candidate_evidence(db, identity_job)
                    except Exception:
                        candidate_evidence = []
                return 200,{'job_id':identity_job,'requested_job_id':job,'available':evidence is not None,
                    'attempts':row[2],'state':row[1],
                    'reason':None if row[3] is None else CATALOG.reason(row[3]),
                    'evidence':evidence,'candidate_evidence':candidate_evidence,'release_name':release_name},[]
            single=re.fullmatch(PREFIX+r'/jobs/([1-9][0-9]{0,18})',path)
            if single:
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                job=int(single[1])
                if job>(1<<63)-1:raise ApiError(400,'API_REQUEST_INVALID')
                queue=JobQueue(configuration.value('storage.database_path'))
                with queue.connection() as db:
                    db.execute('BEGIN')
                    row=db.execute('SELECT job_id,event_id,created_by_job_id,kind,state,activity_code,attempt_count,last_reason_code FROM jobs WHERE job_id=?',(job,)).fetchone()
                    if row is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                    return 200,self._job(queue,db,row,language=configuration.value('anime.title_language')),[]
            if path==PREFIX+'/search':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                return 200,self._search(configuration,query),[]
            if path==PREFIX+'/' and method=='GET':
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                return 200,{'version':'v1','resources':['health','settings','credentials','intake','jobs','jobs/{id}/matching','auth/session','auth/password','recognized','search','webhook']},[]
            season_match=re.fullmatch(PREFIX+r'/recognized/([1-9][0-9]{0,17})/([0-9]{1,3})(?:/([1-9][0-9]{0,17}))?/episodes',path)
            if season_match:
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                tid,season=map(int,season_match.groups()[:2])
                aid=int(season_match.group(3)) if season_match.group(3) else None
                with JobQueue(configuration.value('storage.database_path')).connection() as db:
                    db.execute('BEGIN')
                    parents=db.execute(
                        "SELECT anidb_id,display_title FROM recognized_seasons "
                        "WHERE tvdb_series_id=? AND tvdb_season=?" +
                        (" AND anidb_id=?" if aid is not None else ""),
                        (tid,season,aid) if aid is not None else (tid,season)).fetchall()
                    if len(parents)!=1:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                    parent=parents[0]
                    translated=db.execute(
                        "SELECT english_title,romaji_title FROM anime_title_metadata WHERE anidb_id=?",
                        (parent[0],)).fetchone()
                    recognized_title=preferred_title(parent[1],
                        translated[0] if translated else None,
                        translated[1] if translated else None,
                        configuration.value('anime.title_language'))
                    records=db.execute(
                        "SELECT anidb_episode_number,anidb_episode_id,tvdb_episode,tvdb_episode_id "
                        "FROM recognized_episodes WHERE tvdb_series_id=? AND tvdb_season=? "
                        "AND anidb_id=? ORDER BY tvdb_episode LIMIT 501",(tid,season,parent[0])).fetchall()
                if len(records)>500:raise ApiError(503,'API_UNAVAILABLE')
                return 200,{'tvdb_series_id':tid,'season':season,'anidb_id':parent[0],
                    'title':recognized_title,'items':[{'anidb_episode_number':an,
                    'anidb_episode_id':ae,'tvdb_episode':te,'tvdb_episode_id':tid_episode}
                    for an,ae,te,tid_episode in records]},[]
            if path==PREFIX+'/recognized':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                with JobQueue(configuration.value('storage.database_path')).connection() as db:
                    db.execute('BEGIN')
                    return 200,self._recognized(db,configuration.value('anime.title_language')),[]
            # Series-first Intake reuses existing jobs and staging evidence.
            if path==PREFIX+'/intake/series':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if set(query)-{'before_id','limit','status'}:raise ApiError(400,'API_REQUEST_INVALID')
                before=page_number(query,'before_id',(1<<63)-1,1,(1<<63)-1)
                limit=page_number(query,'limit',30,1,100)
                with JobQueue(configuration.value('storage.database_path')).connection() as db:
                    db.execute('BEGIN')
                    return 200,series_items(db,before,limit,language=configuration.value('anime.title_language'),
                                            status=intake_status(query)),[]
            series_match=re.fullmatch(PREFIX+r'/intake/series/([1-9][0-9]{0,18})(?:/(keep|reject))?',path)
            if series_match:
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                aid=int(series_match[1])
                if aid>(1<<63)-1:raise ApiError(400,'API_REQUEST_INVALID')
                if series_match[2] is None and method=='GET':
                    with JobQueue(configuration.value('storage.database_path')).connection() as db:
                        db.execute('BEGIN')
                        item=series_detail(db,aid,language=configuration.value('anime.title_language'))
                    if item is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                    return 200,item,[]
                if series_match[2]=='keep' and method=='POST':
                    exact(body,('operation_ids',))
                    with JobQueue(configuration.value('storage.database_path')).connection() as db:
                        db.execute('BEGIN')
                        targets=series_keep_targets(db,aid,body['operation_ids'])
                    actions=ManagementActions(configuration.value('storage.database_path'))
                    submitted=[];failed=[]
                    # Each existing Keep is durable; report partial submissions.
                    for job in targets:
                        try:
                            _,receipt,_=actions.submit(job,'keep')
                            submitted.append(receipt['job_id'])
                        except ActionUnavailable:
                            failed.append(job)
                    return 202,{'submitted':submitted,'failed':failed,
                                'message':f'Keep requested for {len(submitted)} staged acquisitions.'},[]
                if series_match[2]=='reject' and method=='POST':
                    exact(body,('operation_ids','reason','note'))
                    result=reject_intake_series(
                        configuration.value('storage.database_path'),aid,
                        body['operation_ids'],body['reason'],body['note'])
                    return 202,{**result,'message':'Series rejection recorded; cleanup queued.',
                                'status_url':f'/api/v1/jobs/{result["job_id"]}',
                                'history_url':f'/api/v1/jobs/{result["job_id"]}/history'},[]
                raise ApiError(405,'API_METHOD_INVALID')
            manual_match=re.fullmatch(
                PREFIX+r'/intake/series/([1-9][0-9]{0,18})/episodes/([0-9]{1,4})/([1-9][0-9]{0,3})/submit',path)
            if manual_match:
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
                if set(body) not in (set(('release_name','magnet_uri')),
                                    set(('release_name','torrent_b64'))):
                    raise ApiError(400,'API_REQUEST_INVALID')
                from src.nextgen.storage.manual_release import (
                    submit as manual_submit, ManualSubmissionInvalid, ManualSubmissionUnavailable)
                try:
                    result=manual_submit(
                        configuration.value('storage.database_path'),
                        *(int(value) for value in manual_match.groups()),
                        release_name=body['release_name'],
                        magnet_uri=body.get('magnet_uri'),
                        torrent_b64=body.get('torrent_b64'))
                except ManualSubmissionInvalid as exc:
                    raise ApiError(400,exc.reason_code) from None
                except ManualSubmissionUnavailable:
                    raise ApiError(409,'MANAGEMENT_ACTION_UNAVAILABLE') from None
                return (200 if result['reused'] else 202),{
                    **result,'message':'Manual release candidate ' +
                    ('already submitted.' if result['reused'] else 'submitted.'),
                    'status_url':f'/api/v1/jobs/{result["job_id"]}'},[]
            # Missing episodes are addressed by AniDB episode number.
            retry_match=re.fullmatch(PREFIX+r'/intake/series/([1-9][0-9]{0,18})/backfill/([1-9][0-9]{0,8})/retry',path)
            if retry_match:
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
                if set(body) not in (set(), {'delete_cache'}) or (
                        'delete_cache' in body and type(body['delete_cache']) is not bool):
                    raise ApiError(400,'API_REQUEST_INVALID')
                aid,episode=map(int,retry_match.groups())
                return 202,retry_missing_episode(
                    configuration.value('storage.database_path'),aid,episode,
                    delete_cache=body.get('delete_cache',False)),[]
            if path==PREFIX+'/intake':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                return 200,self._intake(configuration,query),[]
            # Series-scoped batch controller diagnostics & views (WI-09)
            size_retry_match=re.fullmatch(
                PREFIX+r'/intake/series/([1-9][0-9]{0,18})/backfill/([1-9][0-9]{0,8})/acquire',path)
            if size_retry_match:
                if query or body: raise ApiError(400,'API_REQUEST_INVALID')
                if method!='POST': raise ApiError(405,'API_METHOD_INVALID')
                from src.nextgen.storage.episode_reset import schedule,EpisodeResetUnavailable
                aid,episode=map(int,size_retry_match.groups())
                try:
                    result=schedule(configuration.value('storage.database_path'),
                                    aid,episode,mode='replay')
                except EpisodeResetUnavailable:
                    raise ApiError(409,'MANAGEMENT_ACTION_UNAVAILABLE') from None
                return 202,result,[]
            batch_scope_match = re.fullmatch(PREFIX + r'/intake/series/([1-9][0-9]{0,18})/batch/([1-9][0-9]{0,3})', path)
            if batch_scope_match:
                if method != 'GET': raise ApiError(405, 'API_METHOD_INVALID')
                if query: raise ApiError(400, 'API_REQUEST_INVALID')
                aid, season = map(int, batch_scope_match.groups())
                from src.nextgen.storage.batch_candidates import get_batch_scope_operation_view
                with JobQueue(configuration.value('storage.database_path')).connection() as db:
                    db.execute('BEGIN')
                    view = get_batch_scope_operation_view(db, aid, season)
                if view is None: raise ApiError(404, 'API_ROUTE_NOT_FOUND')
                return 200, view, []
            if path == PREFIX + '/batch/scopes':
                if method != 'GET': raise ApiError(405, 'API_METHOD_INVALID')
                if set(query) - {'limit'}: raise ApiError(400, 'API_REQUEST_INVALID')
                limit = page_number(query, 'limit', 50, 1, 100)
                from src.nextgen.storage.batch_candidates import list_batch_scopes
                with JobQueue(configuration.value('storage.database_path')).connection() as db:
                    db.execute('BEGIN')
                    return 200, {'scopes': list_batch_scopes(db, limit=limit)}, []
            if path==PREFIX+'/health' and method=='GET':
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                with JobQueue(configuration.value('storage.database_path')).connection():pass
                return 200,{'ready':True,'database_schema_version':LATEST_VERSION},[]
            if path==PREFIX+'/settings':
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                if method=='GET':return 200,{'desired':desired.to_document(),
                    'effective':ConfigurationSnapshot(configuration.desired_revision,configuration.values).to_document(),
                    'pending_restart_fields':configuration.pending_restart_fields,'fields':field_definitions()},[]
                if method=='PATCH':
                    exact(body,('expected_revision','settings'))
                    try:result=self.store.update(body['settings'],expected_revision=body['expected_revision'])
                    except ConfigurationConflict:raise
                    except ConfigurationError as exc:raise ApiError(400,exc.reason_code) from None
                    return 200,{'configuration':result.snapshot.to_document(),'saved':result.saved,
                        'durability_confirmed':result.durability_confirmed,'reason_code':result.reason_code,
                        'reason':None if result.reason_code is None else CATALOG.reason(result.reason_code),
                        'restart_fields':result.restart_fields,'restart_components':result.restart_components},[]
                raise ApiError(405,'API_METHOD_INVALID')
            if path==PREFIX+'/credentials' and method=='GET':
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                return 200,asdict(self.secrets.status()),[]
            reveal_match=re.fullmatch(PREFIX+r'/credentials/(tvdb|prowlarr|sonarr|jellyfin|shoko|qbittorrent|access)/reveal',path)
            if reveal_match:
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
                exact(body,())
                # POST requires an authenticated session, same-origin and CSRF above.
                # Values are never logged or included in a general settings read.
                return 200,self.secrets.reveal_integration(reveal_match[1]),[]
            secret_match=re.fullmatch(PREFIX+r'/credentials/(tvdb|prowlarr|sonarr|jellyfin|shoko|qbittorrent|access)',path)
            if secret_match:
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                if method!='PATCH':raise ApiError(405,'API_METHOD_INVALID')
                kind=secret_match[1]
                fields={'prowlarr':('api_key',),'shoko':('api_key',),'jellyfin':('api_key',),'sonarr':('api_key',),'tvdb':('api_key',),'qbittorrent':('username','password'),'access':('webhook_token',)}[kind]
                exact(body,('expected_revision',*fields))
                validate_secret_fields(kind,body,fields)
                callback={'prowlarr':self.secrets.update_prowlarr_api_key,'shoko':self.secrets.update_shoko_api_key,'jellyfin':self.secrets.update_jellyfin_api_key,'sonarr':self.secrets.update_sonarr_api_key,'tvdb':self.secrets.update_tvdb_api_key,'qbittorrent':self.secrets.update_qbittorrent_credentials,'access':self.secrets.update_webhook_token}[kind]
                result=callback(*(body[k] for k in fields),expected_revision=body['expected_revision'])
                if result.saved:event(self.logger,logging.INFO,'API_CREDENTIALS_UPDATED',component='api')
                return 200,{'status':asdict(result.status),'saved':result.saved,'durability_confirmed':result.durability_confirmed,'reason_code':result.reason_code,
                            'reason':None if result.reason_code is None else CATALOG.reason(result.reason_code)},[]
            if method=='GET' and (path==PREFIX+'/jobs' or re.fullmatch(PREFIX+r'/jobs/[1-9][0-9]{0,18}/history',path)):
                return 200,self._jobs(configuration,path,query),[]
        if path in (PREFIX+'/',PREFIX+'/health',PREFIX+'/credentials',PREFIX+'/jobs',PREFIX+'/batch/scopes') or re.fullmatch(PREFIX+r'/jobs/[1-9][0-9]{0,18}/(history|matching)',path):raise ApiError(405,'API_METHOD_INVALID')
        raise ApiError(404,'API_ROUTE_NOT_FOUND')

    def _cookie(self,token,*,clear=False,secure=False):
        return COOKIE+'='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+('0' if clear else str(self.sessions.ttl))+('; Secure' if secure else '')

    def _jobs(self,configuration,path,query,match=None):
        episode_keys={'anidb_id','season','episode'}
        selected=set(query)&episode_keys
        if selected and selected!=episode_keys:raise ApiError(400,'API_REQUEST_INVALID')
        if set(query)-{'after_id','before_id','limit','state','show_completed','show_canceled'}-episode_keys or 'after_id' in query and 'before_id' in query:raise ApiError(400,'API_REQUEST_INVALID')
        episode_filter=(page_number(query,'anidb_id',0,1,(1<<63)-1),
                        page_number(query,'season',0,0,1000),
                        page_number(query,'episode',0,1,1000)) if selected else None
        after=page_number(query,'after_id',0,0,(1<<63)-1);limit=page_number(query,'limit',50,1,100)
        history=path.endswith('/history')
        if history and (set(query)&{'state','show_completed','show_canceled'} or episode_filter):raise ApiError(400,'API_REQUEST_INVALID')
        state_filter=query.get('state',[])
        if len(state_filter)>1 or state_filter and state_filter[0] not in ('PENDING','RUNNING','RETRY_WAIT','ON_HOLD','NEEDS_REVIEW','BLOCKED','DONE','CANCELED'):
            raise ApiError(400,'API_REQUEST_INVALID')
        show_completed=page_number(query,'show_completed',1,0,1)
        show_canceled=page_number(query,'show_canceled',1,0,1)
        queue=JobQueue(configuration.value('storage.database_path'))
        with queue.connection() as db:
            db.execute('BEGIN')
            if history:
                if 'before_id' in query:raise ApiError(400,'API_REQUEST_INVALID')
                job=int(path.split('/')[-2])
                if job>(1<<63)-1:raise ApiError(400,'API_REQUEST_INVALID')
                kind=db.execute('SELECT kind FROM jobs WHERE job_id=?',(job,)).fetchone()
                if kind is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                # Name the job that did the work: its kind, label and src/nextgen/jobs module.
                module=kind[0].lower()+'_job'
                from importlib.util import find_spec
                about={'job_id':job,'kind':kind[0],'label':CATALOG.handler(kind[0]),
                       'module':module if find_spec('src.nextgen.jobs.'+module) else None}
                # A deferred observation is not another failed processing attempt.
                # Keep the historical claim count for leases/CAS; expose the
                # successfully deferred checks separately in History.
                counts=db.execute("SELECT j.attempt_count, "
                    "(SELECT count(*) FROM job_history h WHERE h.job_id=j.job_id AND h.event_code='DEFERRED') "
                    "FROM jobs j WHERE j.job_id=?",(job,)).fetchone()
                about['scheduler_claim_count']=counts[0]
                about['poll_check_count']=counts[1]
                about['work_attempt_count']=counts[0]-counts[1]
                if kind[0]=='CLEANUP_DECIDED_DOWNLOAD':
                    about['cleanup_progress']=self._cleanup_progress(db,job)
                target=db.execute("SELECT target_job_id FROM management_actions WHERE job_id=?",(job,)).fetchone()
                if target:about['target_job_id']=target[0]
                if kind[0]=='REJECT_SERIES':
                    rejection=db.execute(
                        'SELECT reason_text,cleanup_state FROM anime_review_decisions '
                        'WHERE job_id=?',(job,)).fetchone()
                    if rejection:
                        about['rejection_reason']=rejection[0]
                        about['cleanup_state']=rejection[1]
                rows=db.execute('SELECT history_id,occurred_at_ms,event_code,reason_code,component,details_json,handler_code,handler_label,invocation_id '
                    'FROM job_history WHERE job_id=? AND history_id>? ORDER BY history_id LIMIT ?',(job,after,limit+1)).fetchall()
                items=[]
                for row in rows[:limit]:
                    try:details=json.loads(encode_details(json.loads(row[5] or '{}')))
                    except (ValueError,TypeError):details={}
                    items.append({'history_id':row[0],'occurred_at_ms':row[1],'event':CATALOG.event(row[2]),
                        'reason':None if row[3] is None else CATALOG.reason(row[3]),'component':row[4],'details':details,
                        'handler':None if row[6] is None else CATALOG.handler(row[6],recorded_label=row[7]),'invocation_id':row[8],
                        'detail':size_detail(details) or failed_detail(details)})
                # Every release tried for this backfill episode, with why it stopped.
                attempts=None
                if after==0:
                    from src.nextgen.storage.backfill_attempts import backfill_attempts
                    found=backfill_attempts(db,job)
                    if found is not None:
                        attempts={'episode':found['episode'],'attempts':[
                            {**item,'reason':None if item['reason_code'] is None else CATALOG.reason(item['reason_code']),
                             'detail':size_detail(item['details'])} for item in found['attempts']]}
            else:
                newest='before_id' in query
                cursor=page_number(query,'before_id',(1<<63)-1,1,(1<<63)-1) if newest else after
                criteria=['job_id<?' if newest else 'job_id>?']
                values=[cursor]
                if state_filter:
                    criteria.append('state=?')
                    values.append(state_filter[0])
                if match:
                    criteria.append('('+match[0]+')')
                    values.extend(match[1])
                if episode_filter:
                    # Include all operations for exact verified episode reservations;
                    # never infer an episode from a torrent title. Released
                    # reservations remain visible for diagnosis.
                    criteria.append("""(
                        EXISTS (SELECT 1 FROM episode_reservations r
                            JOIN job_operation_members owner ON owner.job_id=r.owner_job_id
                            JOIN job_operation_members member ON member.operation_id=owner.operation_id
                            WHERE member.job_id=jobs.job_id AND r.anidb_id=?
                            AND r.tvdb_season=? AND r.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM batch_episode_reservations r
                            JOIN job_operation_members owner ON owner.job_id=r.owner_job_id
                            JOIN job_operation_members member ON member.operation_id=owner.operation_id
                            WHERE member.job_id=jobs.job_id AND r.anidb_id=?
                            AND r.tvdb_season=? AND r.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM management_actions a
                            JOIN job_operation_members member ON member.operation_id=a.operation_id
                            JOIN episode_reservations r ON r.owner_job_id=member.job_id
                            WHERE a.job_id=jobs.job_id AND r.anidb_id=?
                            AND r.tvdb_season=? AND r.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM management_actions a
                            JOIN job_operation_members member ON member.operation_id=a.operation_id
                            JOIN batch_episode_reservations r ON r.owner_job_id=member.job_id
                            WHERE a.job_id=jobs.job_id AND r.anidb_id=?
                            AND r.tvdb_season=? AND r.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM manual_release_submissions submission
                            WHERE submission.event_id=jobs.event_id
                            AND submission.anidb_id=? AND submission.tvdb_season=?
                            AND submission.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM manual_release_archive archived
                            WHERE archived.event_id=jobs.event_id
                            AND archived.anidb_id=? AND archived.tvdb_season=?
                            AND archived.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM sonarr_backfill_results b
                            JOIN trial_plans p USING(plan_id)
                            JOIN (SELECT anidb_id,anidb_episode_number,tvdb_season,tvdb_episode
                                  FROM recognized_episodes UNION
                                  SELECT p2.anidb_id,s2.anidb_episode_number,s2.tvdb_season,s2.tvdb_episode
                                  FROM sonarr_episode_selections s2 JOIN trial_plans p2 USING(plan_id)) x
                            ON x.anidb_id=p.anidb_id AND x.anidb_episode_number=b.anidb_episode_number
                            WHERE b.job_id=jobs.job_id AND p.anidb_id=?
                            AND x.tvdb_season=? AND x.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM trial_plans p
                            JOIN (SELECT anidb_id,anidb_episode_number,tvdb_season,tvdb_episode
                                  FROM recognized_episodes UNION
                                  SELECT p2.anidb_id,s2.anidb_episode_number,s2.tvdb_season,s2.tvdb_episode
                                  FROM sonarr_episode_selections s2 JOIN trial_plans p2 USING(plan_id)) x
                            ON x.anidb_id=p.anidb_id
                            AND x.anidb_episode_number=json_extract(jobs.payload_json,'$.exhausted_episode')
                            WHERE jobs.kind='DISCOVER_SONARR_BACKFILL'
                            AND p.plan_id=json_extract(jobs.payload_json,'$.plan_id')
                            AND p.anidb_id=? AND x.tvdb_season=? AND x.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM trial_plans p JOIN (
                            SELECT anidb_id,anidb_episode_number,tvdb_season,tvdb_episode FROM recognized_episodes
                            UNION SELECT p2.anidb_id,s2.anidb_episode_number,s2.tvdb_season,s2.tvdb_episode
                            FROM sonarr_episode_selections s2 JOIN trial_plans p2 USING(plan_id)
                            UNION SELECT anidb_id,anidb_episode_number,tvdb_season,tvdb_episode
                            FROM release_verifications WHERE tvdb_order='official'
                            ) x ON x.anidb_id=p.anidb_id
                            AND x.anidb_episode_number=json_extract(jobs.payload_json,'$.episode')
                            WHERE jobs.kind='RESET_EPISODE_SEARCH'
                            AND p.plan_id=json_extract(jobs.payload_json,'$.plan_id')
                            AND p.anidb_id=? AND x.tvdb_season=? AND x.tvdb_episode=?)
                        OR EXISTS (SELECT 1 FROM prowlarr_search_snapshots s
                            JOIN trial_plans p ON p.plan_id=s.plan_id
                            WHERE s.job_id=jobs.job_id AND p.anidb_id=?
                            AND s.season=? AND s.episode=?)
                        )""")
                    values.extend(episode_filter*10)
                if not show_completed:criteria.append("state<>'DONE'")
                if not show_canceled:
                    criteria.append("state<>'CANCELED'")
                    criteria.append("NOT EXISTS (SELECT 1 FROM job_operation_members m JOIN job_operations o USING(operation_id) "
                                    "WHERE m.job_id=jobs.job_id AND o.state='CANCELED')")
                ordering=' ORDER BY job_id DESC' if newest else ' ORDER BY job_id'
                rows=db.execute('SELECT job_id,event_id,created_by_job_id,kind,state,activity_code,attempt_count,last_reason_code '
                                'FROM jobs WHERE '+' AND '.join(criteria)+ordering+' LIMIT ?',(*values,limit+1)).fetchall()
                items=[self._job(queue,db,r,language=configuration.value('anime.title_language')) for r in rows[:limit]]
                if newest:return {'items':items,'next_before_id':rows[limit-1][0] if len(rows)>limit else None}
        page={'items':items,'next_after_id':rows[limit-1][0] if len(rows)>limit else None}
        return {**page,'job':about,'backfill_attempts':attempts} if history else page

    def _recognized(self,db,language,match=None):
        where,params=match if match else ('1',[])
        rows=db.execute(
            "SELECT s.tvdb_series_id,s.tvdb_season,s.anidb_id,s.display_title,"
            "t.english_title,t.romaji_title,"
            "count(DISTINCT a.alias_key),count(DISTINCT v.anidb_episode_id) "
            "FROM recognized_seasons s "
            "LEFT JOIN anime_title_metadata t ON t.anidb_id=s.anidb_id "
            "LEFT JOIN recognized_aliases a "
            "ON a.tvdb_series_id=s.tvdb_series_id AND a.tvdb_season=s.tvdb_season AND a.anidb_id=s.anidb_id "
            "LEFT JOIN recognized_episodes v "
            "ON v.tvdb_series_id=s.tvdb_series_id AND v.tvdb_season=s.tvdb_season "
            "AND v.anidb_id=s.anidb_id "
            "WHERE ("+where+") "
            "GROUP BY s.tvdb_series_id,s.tvdb_season,s.anidb_id,s.display_title "
            "ORDER BY s.display_title,s.tvdb_season LIMIT 501",params).fetchall()
        if len(rows)>500:raise ApiError(503,'API_UNAVAILABLE')
        return {'items':[{'tvdb_series_id':tid,'season':season,
            'anidb_id':aid,'title':preferred_title(title,english,romaji,language),
            'alias_count':alias_count,'verified_episode_count':ep_count}
            for tid,season,aid,title,english,romaji,alias_count,ep_count in rows]}

    def _search(self,configuration,query):
        # One route for every page; the scope decides which page's list is filtered.
        scope=query.get('scope',[]);text=query.get('q',[])
        if len(scope)!=1 or len(text)!=1:raise ApiError(400,'API_REQUEST_INVALID')
        try:found=search_match(scope[0],search_words(text[0]))
        except SearchInvalid:raise ApiError(400,'API_REQUEST_INVALID') from None
        rest={key:value for key,value in query.items() if key not in ('scope','q')}
        language=configuration.value('anime.title_language')
        if scope[0]=='jobs':return self._jobs(configuration,PREFIX+'/jobs',rest,match=found)
        with JobQueue(configuration.value('storage.database_path')).connection() as db:
            db.execute('BEGIN')
            if scope[0]=='recognized':
                if rest:raise ApiError(400,'API_REQUEST_INVALID')
                return self._recognized(db,language,found)
            if set(rest)-{'before_id','limit','status'}:raise ApiError(400,'API_REQUEST_INVALID')
            before=page_number(rest,'before_id',(1<<63)-1,1,(1<<63)-1)
            limit=page_number(rest,'limit',30,1,100)
            return series_items(db,before,limit,language=language,match=found,status=intake_status(rest))

    def _intake(self,configuration,query):
        if set(query)-{'before_id','limit'}:raise ApiError(400,'API_REQUEST_INVALID')
        before=page_number(query,'before_id',(1<<63)-1,1,(1<<63)-1)
        limit=page_number(query,'limit',50,1,100)
        queue=JobQueue(configuration.value('storage.database_path'))
        with queue.connection() as db:
            db.execute('BEGIN')
            rows=db.execute('SELECT o.operation_id,j.job_id,j.event_id,j.created_by_job_id,j.kind,j.state,'
                'j.activity_code,j.attempt_count,j.last_reason_code FROM job_operations o '
                'JOIN jobs j ON j.job_id=(SELECT MAX(m.job_id) FROM job_operation_members m WHERE m.operation_id=o.operation_id) '
                'WHERE o.operation_id<? AND NOT EXISTS (SELECT 1 FROM management_actions a WHERE a.job_id=j.job_id) '
                'ORDER BY o.operation_id DESC LIMIT ?',(before,limit+1)).fetchall()
            items=[self._job(queue,db,row[1:],language=configuration.value('anime.title_language')) for row in rows[:limit]]
        return {'items':items,'next_before_id':rows[limit-1][0] if len(rows)>limit else None}

    @staticmethod
    def _cleanup_progress(db,job_id):
        """Current recorded checkpoint, not an instruction to repeat deletion."""
        source=db.execute(
            "SELECT json_extract(payload_json,'$.staging_job_id'), "
            "json_extract(payload_json,'$.decision_job_id') "
            "FROM jobs WHERE job_id=? AND kind='CLEANUP_DECIDED_DOWNLOAD'",(job_id,)).fetchone()
        if not source:return None
        if source[0] is not None:
            row=db.execute("SELECT c.phase,c.updated_at_ms FROM staging_publications s "
                "JOIN cancellation_cleanup c ON c.plan_id=s.plan_id "
                "WHERE s.job_id=?",(source[0],)).fetchone()
        elif source[1] is not None:
            row=db.execute("SELECT c.phase,c.updated_at_ms FROM management_actions a "
                "JOIN job_operation_members m ON m.operation_id=a.operation_id "
                "JOIN staging_publications s ON s.job_id=m.job_id "
                "JOIN cancellation_cleanup c ON c.plan_id=s.plan_id "
                "WHERE a.job_id=?",(source[1],)).fetchone()
        else:return None
        return None if row is None else {'phase':row[0],'since_ms':row[1]}

    @staticmethod
    def _episode_targets(db,job_id,kind,event_id):
        # Prefer verified reservations within this operation over release labels,
        # whose numbering may not match TVDB placements.
        records=db.execute("""
            SELECT r.tvdb_season,r.tvdb_episode FROM episode_reservations r
            JOIN job_operation_members owner ON owner.job_id=r.owner_job_id
            JOIN job_operation_members member ON member.operation_id=owner.operation_id
            WHERE member.job_id=?
            UNION
            SELECT r.tvdb_season,r.tvdb_episode FROM batch_episode_reservations r
            JOIN job_operation_members owner ON owner.job_id=r.owner_job_id
            JOIN job_operation_members member ON member.operation_id=owner.operation_id
            WHERE member.job_id=?
            ORDER BY 1,2 LIMIT 101
        """,(job_id,job_id)).fetchall()
        if not records:
            records=db.execute("""
                SELECT r.tvdb_season,r.tvdb_episode FROM management_actions a
                JOIN job_operation_members member ON member.operation_id=a.operation_id
                JOIN episode_reservations r ON r.owner_job_id=member.job_id
                WHERE a.job_id=?
                UNION
                SELECT r.tvdb_season,r.tvdb_episode FROM management_actions a
                JOIN job_operation_members member ON member.operation_id=a.operation_id
                JOIN batch_episode_reservations r ON r.owner_job_id=member.job_id
                WHERE a.job_id=? ORDER BY 1,2 LIMIT 101
            """,(job_id,job_id)).fetchall()
        if not records and event_id is not None:
            records=db.execute(
                "SELECT tvdb_season,tvdb_episode FROM manual_release_submissions "
                "WHERE event_id=? LIMIT 1",(event_id,)).fetchall()
        if not records and kind=='DISCOVER_SONARR_BACKFILL':
            # Discovery keys are AniDB numbers; show their verified TVDB placement.
            placed="""(SELECT anidb_id,anidb_episode_number,tvdb_season,tvdb_episode
                FROM recognized_episodes UNION
                SELECT p2.anidb_id,s2.anidb_episode_number,s2.tvdb_season,s2.tvdb_episode
                FROM sonarr_episode_selections s2 JOIN trial_plans p2 USING(plan_id) UNION
                SELECT anidb_id,anidb_episode_number,tvdb_season,tvdb_episode
                FROM release_verifications WHERE tvdb_order='official') x"""
            records=db.execute(f"""
                SELECT x.tvdb_season,x.tvdb_episode FROM sonarr_backfill_results b
                JOIN trial_plans p USING(plan_id) JOIN {placed}
                ON x.anidb_id=p.anidb_id AND x.anidb_episode_number=b.anidb_episode_number
                WHERE b.job_id=?
                UNION
                SELECT x.tvdb_season,x.tvdb_episode
                FROM jobs j JOIN trial_plans p
                ON p.plan_id=json_extract(j.payload_json,'$.plan_id') JOIN {placed}
                ON x.anidb_id=p.anidb_id
                AND x.anidb_episode_number=COALESCE(
                    json_extract(j.payload_json,'$.exhausted_episode'),
                    json_extract(j.payload_json,'$.fallback_episode'),
                    json_extract(j.payload_json,'$.retry_episode')
                )
                WHERE j.job_id=? AND (
                    json_type(j.payload_json,'$.exhausted_episode')='integer' OR
                    json_type(j.payload_json,'$.fallback_episode')='integer' OR
                    json_type(j.payload_json,'$.retry_episode')='integer'
                )
                ORDER BY 1,2 LIMIT 101
            """,(job_id,job_id)).fetchall()
        if not records and kind in ('VERIFY_RELEASE','EVALUATE_TRIAL','IDENTIFY_ANIME') and event_id is not None:
            records=db.execute("""
                SELECT DISTINCT tvdb_season,tvdb_episode FROM release_verifications
                WHERE job_id=? OR job_id IN
                  (SELECT job_id FROM jobs WHERE event_id=? AND kind='VERIFY_RELEASE')
                ORDER BY tvdb_season,tvdb_episode LIMIT 101
            """,(job_id,event_id)).fetchall()
        return [{'season':season,'episode':episode} for season,episode in records[:100]
                if type(season) is int and type(episode) is int]

    def _job(self,queue,db,row,language='english'):
        op=db.execute('SELECT o.operation_id,o.state,o.cleanup_state,o.reason_code FROM job_operations o JOIN job_operation_members m USING(operation_id) WHERE m.job_id=?',(row[0],)).fetchone()
        actions=ManagementActions(queue.database).capabilities(db,row[0])
        actions['retry']=retry_capability(db,row[0])
        context=db.execute('SELECT j.created_at_ms,e.received_at_ms,CASE WHEN length(e.raw_body)<=65536 THEN e.raw_body ELSE NULL END '
                           'FROM jobs j LEFT JOIN incoming_events e ON e.event_id=j.event_id WHERE j.job_id=?',(row[0],)).fetchone()
        release=None
        try:
            declaration=json.loads(context[2] or b'{}')
            value=declaration.get('release_name') if type(declaration) is dict else None
            if type(value) is str and len(value.encode('utf-8'))<=1024:release=value
        except (ValueError,UnicodeError,TypeError,RecursionError):pass
        sized=db.execute("SELECT details_json FROM job_history WHERE job_id=? AND reason_code IN "
                         "('ACQUISITION_SIZE_LIMIT','ACQUISITION_PLAN_SIZE_LIMIT') ORDER BY history_id DESC LIMIT 1",(row[0],)).fetchone()
        try:sized=size_detail(json.loads(sized[0])) if sized else None
        except (ValueError,TypeError):sized=None
        stopped=None
        if row[4] in ('NEEDS_REVIEW','BLOCKED','ON_HOLD'):
            found=db.execute("SELECT details_json FROM job_history WHERE job_id=? AND ("
                             "json_extract(details_json,'$.failed_check') IS NOT NULL OR "
                             "json_extract(details_json,'$.cause_reason_code') IS NOT NULL OR "
                             "json_extract(details_json,'$.blocking_job_id') IS NOT NULL) "
                             "ORDER BY history_id DESC LIMIT 1",(row[0],)).fetchone()
            try:stopped=failed_detail(json.loads(found[0])) if found else None
            except (ValueError,TypeError):stopped=None
        titles=db.execute('SELECT a.title,t.english_title,t.romaji_title '
                          'FROM identity_results r JOIN identified_anime a USING(anidb_id) '
                          'LEFT JOIN anime_title_metadata t USING(anidb_id) '
                          'WHERE r.event_id=? LIMIT 2',(row[1],)).fetchall()
        anime_title=(preferred_title(*titles[0],language) if len(titles)==1
                     and len(titles[0][0].encode('utf-8'))<=1024 else None)
        targets=self._episode_targets(db,row[0],row[3],row[1])
        return {'job_id':row[0],'event_id':row[1],'created_by_job_id':row[2],'kind':row[3],'state':row[4],
                'is_management_action':db.execute('SELECT 1 FROM management_actions WHERE job_id=?',(row[0],)).fetchone() is not None,
                'label':CATALOG.handler(row[3]),'created_at_ms':context[0],'received_at_ms':context[1],
                'release_name':release,'anime_title':anime_title,'episode_targets':targets,
                'acquisition_status':CATALOG.state('acquisition',op[1]),
                'status':CATALOG.job_status(row[4],row[5],kind=row[3]),'attempt_count':row[6],
                'cleanup_progress':self._cleanup_progress(db,row[0]) if row[3]=='CLEANUP_DECIDED_DOWNLOAD' else None,
                'reason':None if row[7] is None else CATALOG.reason(row[7]),'size_detail':sized,'failed_detail':stopped,'actions':actions,
                'cancellation':{'operation_id':op[0],'state':op[1],'cleanup_state':op[2],'reason_code':op[3],
                                 'reason':None if op[3] is None else CATALOG.reason(op[3]),
                                'can_cancel':actions['cancel']['enabled'],'can_cleanup':actions['cleanup']['enabled']}}
