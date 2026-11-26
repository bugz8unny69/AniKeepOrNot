"""Small versioned ASGI application. No worker execution or provider logic."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import logging
import os
import re
import time
import traceback
import uuid
from urllib.parse import parse_qs, urlsplit

from src.anime.async_io import blocking
from src.anime.systemd_notify import notify, watchdog_interval
from src.anime.api.password_store import PasswordStore, PasswordConflict, PasswordStorageError
from src.anime.api.passwords import verify_password
from src.anime.api.admin_socket import start_admin
from src.anime.api.auth import (ApiError, COOKIE, Sessions, public_origin, check_origin, valid_host, webhook_key)
from src.anime.configuration import ConfigurationStore
from src.anime.configuration_schema import ConfigurationError, ConfigurationConflict, ConfigurationSnapshot, for_operation, field_definitions
from src.anime.diagnostics import configure_logging, event, log_history
from src.anime.maintenance import MaintenanceGate, MaintenanceError, MaintenancePending, MaintenanceBusy
from src.anime.messages import MessageCatalog
from src.anime.dashboard.assets import load_assets, CSP as DASHBOARD_CSP
from src.anime.search import SearchInvalid, match as search_match, words as search_words
from src.anime.secrets import SecretStore
import sqlite3
from src.anime.storage.adapter import DatabaseVersionMismatch
from src.anime.storage import notifications as inbox
from src.anime.decided_series import (webhook_decision, notify_refused, release_name, REASONS as DECISION_REASONS,WORDS as DECISION_WORDS)
from src.anime.storage.migrations import LATEST_VERSION
from src.anime.anime_titles import preferred_title
from src.anime.storage.connection import database
from src.anime.storage.connection import database as connection
from src.anime.series_job.views import running, series_list, series_view

PREFIX='/api/v1'
MAX_BODY=65536
MAX_TORRENT_BODY=6*1024*1024  # base64 of a bounded 4 MiB BEP3 file
CATALOG=MessageCatalog()


def where(exc):
    """file:line in function where the error was raised in our code (the last src/ frame)."""
    frames=traceback.extract_tb(exc.__traceback__)
    ours=[f for f in frames if '/src/' in f.filename] or frames
    if not ours:return type(exc).__name__
    f=ours[-1]
    return f"{f.filename.rsplit('/src/',1)[-1]}:{f.lineno} in {f.name} ({type(exc).__name__})"


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
            from src.anime.secrets import _access_values
            _access_values(body['webhook_token'],'')
        elif kind=='qbittorrent':
            from src.anime.acquisition.settings import DownloadCredentials
            if body['username'] or body['password']:DownloadCredentials(body['username'],body['password'])
        elif body['api_key']:
            from src.anime.providers.http import Credentials
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
    from src.anime.acquisition.metadata import hash_magnet
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


def service_line(name,status=None):
    """'Prowlarr rejected the login or API key.' for a service named in job details."""
    from src.anime.services import default_registry
    from src.anime.services.base import SETUP_STATUSES, _SUMMARY, valid_name
    if not valid_name(name):return None
    try:label=default_registry().get(name).label
    except (KeyError,LookupError,ValueError):label=name
    if status in SETUP_STATUSES:return _SUMMARY[status].format(label=label)
    return f'Service: {label}.'


def failed_detail(details):
    """Which check stopped the work and the file it was looking at."""
    check,path=details.get('failed_check'),details.get('failed_path')
    cause=details.get('cause_reason_code')
    blocker=details.get('blocking_job_id')
    op=details.get('operation_id')
    service=details.get('service_detail')
    if type(service) is str:
        # A service outage: say which service and what is wrong.
        return service + (f' (cause: {cause})' if type(cause) is str else '')
    named=service_line(details.get('service'),details.get('service_status'))
    if named is not None:
        # A generic SERVICE_* code: its message does not name the service, so say it here.
        return named + (f' · stopped at {check}' if type(check) is str else '')
    if blocker is not None and type(check) is not str:
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
    if blocker is not None:
        # A stop caused by another job (e.g. a reset whose owner's cleanup stopped).
        res += f' · see Job #{blocker}' + (f' (operation #{op})' if op is not None else '')
    return res


def service_statuses(database, *, now_ms=None, configuration=None):
    """Every service's last known status as written by the worker (status bar)."""
    import time
    from src.anime.services import default_registry
    from src.anime.services.status_store import load
    now=time.time_ns()//1000000 if now_ms is None else now_ms
    stored=load(database)
    items=[]
    for adapter in default_registry():
        if adapter.name=='transcode' and configuration is not None and configuration.value('transcode.mode')!='remote': continue
        row=stored.get(adapter.name)
        items.append({'service':adapter.name,'label':adapter.label,'required':adapter.required,
            'optional':adapter.optional,
            'status':row.status if row else 'UNKNOWN',
            'message':row.message() if row else f'The {adapter.label} status is unknown: the worker has not checked it yet.',
            'checked_at_ms':row.checked_at_ms if row else None,
            'since_ms':row.since_ms if row else None,
            'stale':row.stale(now) if row else False})
    return {'services':items}


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
        self.admin_path=os.environ.get('AKON_ADMIN_SOCKET') or os.environ.get('ANIME_DISCOVERY_ADMIN_SOCKET')
        self.assets=load_assets()
        self.pool=ThreadPoolExecutor(max_workers=8,thread_name_prefix='nextgen-api')
        self.slots=asyncio.Semaphore(8)
        self.logger=configure_logging(self.startup.logging_settings(),'api',logger=logging.Logger('api.request'))
        self.closed=False
        # The API identifies releases (in its identity process) and gives the worker a job.
        from src.anime.identity_bridge import IdentityBridge
        self.bridge=IdentityBridge(directory,self.startup.value('storage.database_path'),self.logger,
                                   executor=self.pool)
        self.bridge_task=None

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
                    except Exception:  # Boundary: startup must report failure; full trace in the log.
                        self.logger.error('The admin socket could not be started',exc_info=True)
                        await send({'type':'lifespan.startup.failed','message':'Admin socket could not be started'})
                        return
                    await send({'type':'lifespan.startup.complete'})
                    self.bridge_task=asyncio.create_task(self.bridge.run())
                    interval = watchdog_interval()
                    if interval is not None:
                        watchdog_task = asyncio.create_task(self._watchdog(interval))
                elif message['type']=='lifespan.shutdown':
                    notify('STOPPING=1')
                    # The identity process must stop with the API (KillMode=mixed, no stop timeout).
                    await self.bridge.close()
                    if self.bridge_task is not None:
                        await asyncio.gather(self.bridge_task, return_exceptions=True)
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
        method=scope.get('method','');path=scope.get('path','');started=time.monotonic()
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
            if len(raw_query)>2048 or len(path)>512 or '%' in path:raise ApiError(404,'API_ROUTE_NOT_FOUND')
            if scope.get('raw_path',path.encode('utf-8'))!=path.encode('utf-8'):raise ApiError(400,'API_REQUEST_INVALID')
            for mount in ('/akon','/anime-discovery'):
                if path==mount:
                    path='/';break
                if path.startswith(mount+'/'):
                    path=path[len(mount):];break
            if not (path.startswith(PREFIX+'/') or path in self.assets):raise ApiError(404,'API_ROUTE_NOT_FOUND')
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
            error=CATALOG.reason(exc.reason_code)
            if exc.detail:error={**error,'message':exc.detail}
            status=exc.status;result={'api_version':'v1','request_id':request_id,'error':error}
            event(self.logger,logging.WARNING,'API_REQUEST_REJECTED',component='api',reason_code=exc.reason_code)
        except (UnicodeError,ValueError) as exc:
            # Bad input from the caller; where it was refused, so the code says which check.
            status=400;result={'api_version':'v1','request_id':request_id,'error':CATALOG.reason('API_REQUEST_INVALID')}
            self.logger.warning('%s %s: refused as invalid at %s',method,path,where(exc))
        except Exception as exc:
            # The API's one boundary: anything else is a program bug. The full stack
            # trace goes to the log; the caller is told where, so the code can be read.
            at=where(exc)
            self.logger.error('%s %s failed with a program error at %s',method,path,at,exc_info=True)
            status=500;result={'api_version':'v1','request_id':request_id,'error':{**CATALOG.reason('API_PROGRAM_ERROR'),
                'message':f'Program error at {at}. The full error is in the API log.'}}
        encoded=json.dumps(result,allow_nan=False,separators=(',',':')).encode()
        response_headers=[(b'content-type',b'application/json; charset=utf-8'),(b'cache-control',b'no-store'),
            (b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer'),
            (b'content-security-policy',b"default-src 'none'; frame-ancestors 'none'"),
            (b'x-request-id',request_id.encode()),(b'content-length',str(len(encoded)).encode()),*extra]
        if status in (429,503) and not any(k==b'retry-after' for k,_ in extra):response_headers.append((b'retry-after',b'60'))
        await send({'type':'http.response.start','status':status,'headers':response_headers})
        await send({'type':'http.response.body','body':encoded})
        # Changes are INFO; the dashboard's reads are DEBUG, so polling doesn't fill the log.
        self.logger.log(logging.DEBUG if method=='GET' else logging.INFO,'%s %s answered %s in %d ms',
                        method,path,status,(time.monotonic()-started)*1000)

    def _dispatch(self,method,path,headers,query,body,peer):
        try:return self._route(method,path,headers,query,body,peer)
        except ApiError:raise
        except ConfigurationConflict as exc:raise ApiError(409,exc.reason_code) from None
        except ConfigurationError as exc:raise ApiError(503,exc.reason_code) from None
        except (MaintenancePending,MaintenanceBusy,MaintenanceError) as exc:raise ApiError(503,exc.reason_code) from None
        except DatabaseVersionMismatch as exc:raise ApiError(503,exc.reason_code) from None
        except sqlite3.Error:raise ApiError(503,'DATABASE_ERROR') from None

    def _release(self,check,normalized):
        """Autobrr's two calls for one release, answered alike from one queue row per info hash.

        /check is Autobrr's External filter and works like SMTP greylisting:
        200 accepted, 422 rejected, 503 "ask again later" while the identity
        process gets the real data from AniDB (Autobrr retries on 503).
        /webhook is Autobrr's action; it gets 202 while the release waits.
        """
        from src.anime.identity_bridge import take_release
        desired=self.store.read();configuration=for_operation(desired,self.startup,component='api')
        configure_logging(configuration.logging_settings(),'api',logger=self.logger)
        database=configuration.value('storage.database_path')
        with MaintenanceGate(database).operation():
            name=release_name(normalized)
            # A decided series is refused here from local data only.
            decided=webhook_decision(configuration,normalized)
            if decided is not None:
                with connection(database) as db:
                    from src.anime.storage.sqlite import transaction
                    with transaction(db):notify_refused(db,*decided,normalized)
                self.logger.info('%s: refused, AniDB %s is already %s; in API Reject',
                                 name,decided[0],DECISION_WORDS[decided[1]])
                # Final for Autobrr: no event and no job.
                return (422 if check else 200),{'accepted':False,'reason_code':DECISION_REASONS[decided[1]]},[]
            request_id,outcome,reason,new=take_release(database,normalized,self.logger)
        if outcome is None:
            self.logger.log(logging.INFO if new else logging.DEBUG,'%s: %s; waiting for AniDB (release %s)%s',
                            name,'new release, asked the identity process' if new else 'still identifying',
                            request_id,', Autobrr asks again' if check else '')
            self.bridge.wake()
            if check:
                return 503,{'accepted':None,'request_id':request_id,'reason_code':'IDENTITY_PENDING'},[(b'retry-after',b'15')]
            return 202,{'request_id':request_id},[]
        self.logger.info('%s: %s (release %s)',name,'accepted' if outcome=='accepted' else 'rejected, '+str(reason),request_id)
        if outcome=='accepted':
            return 200,{'accepted':True,'request_id':request_id},[]
        return (422 if check else 200),{'accepted':False,'request_id':request_id,'reason_code':reason},[]

    def _route(self,method,path,headers,query,body,peer):
        access=self.secrets.access_credentials()  # Fresh, fail closed. No fallback.
        if path in (PREFIX+'/webhook',PREFIX+'/check'):
            if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
            if set(query)-{'token'}:raise ApiError(400,'API_REQUEST_INVALID')
            webhook_key(headers,query,body,access.webhook_token)
            normalized=normalize_webhook(body)
            if any(key and key.encode() in normalized for key in (access.webhook_token,)):raise ApiError(400,'API_WEBHOOK_INVALID')
            return self._release(path==PREFIX+'/check',normalized)
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
            db_path=configuration.value('storage.database_path')
            language=configuration.value('anime.title_language')
            if path==PREFIX+'/running':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                with database(db_path) as db:
                    return 200,running(db),[]
            if path==PREFIX+'/services':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                data=service_statuses(db_path,configuration=configuration)
                # The bell's unread count rides on this poll, which runs on every page.
                with database(db_path) as db:
                    data['notifications_unread']=inbox.unread(db)
                return 200,data,[]
            if path==PREFIX+'/notifications' or path.startswith(PREFIX+'/notifications/'):
                return self._notifications(configuration,method,path,query,body)
            if path==PREFIX+'/search':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                return 200,self._search(configuration,query),[]
            if path==PREFIX+'/' and method=='GET':
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                return 200,{'version':'v1','resources':['health','settings','credentials','intake','series','running',
                    'auth/session','auth/password','recognized','search','webhook','check']},[]
            series_match=re.fullmatch(PREFIX+r'/recognized/([1-9][0-9]{0,17})/episodes',path)
            if series_match:
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                aid=int(series_match.group(1))
                with database(db_path) as db:
                    db.execute('BEGIN')
                    parent=db.execute("SELECT display_title,tvdb_series_id FROM recognized_series WHERE anidb_id=?",
                                      (aid,)).fetchone()
                    if parent is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                    translated=db.execute(
                        "SELECT english_title,romaji_title FROM anime_title_metadata WHERE anidb_id=?",
                        (aid,)).fetchone()
                    recognized_title=preferred_title(parent[0],
                        translated[0] if translated else None,
                        translated[1] if translated else None,language)
                    records=db.execute(
                        "SELECT anidb_episode_number,anidb_episode_id,tvdb_season,tvdb_episode,tvdb_episode_id "
                        "FROM recognized_episodes WHERE anidb_id=? ORDER BY anidb_episode_number LIMIT 501",
                        (aid,)).fetchall()
                if len(records)>500:raise ApiError(503,'API_UNAVAILABLE')
                return 200,{'anidb_id':aid,'tvdb_series_id':parent[1],'title':recognized_title,
                    'items':[{'anidb_episode_number':an,'anidb_episode_id':ae,'tvdb_season':ts,
                              'tvdb_episode':te,'tvdb_episode_id':tid_episode}
                             for an,ae,ts,te,tid_episode in records]},[]
            if path==PREFIX+'/recognized':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                with database(db_path) as db:
                    db.execute('BEGIN')
                    return 200,self._recognized(db,language),[]
            # Intake: one row per series (the series job). /intake/series/... and /series/... are the same.
            if path in (PREFIX+'/intake',PREFIX+'/intake/series'):
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                return 200,self._intake(configuration,query),[]
            series_act=re.fullmatch(
                PREFIX+r'/(?:intake/)?series/([1-9][0-9]{0,18})(?:/(keep|reject|search|retry|cancel|release))?',path)
            if series_act:
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                aid=int(series_act[1])
                if aid>(1<<63)-1:raise ApiError(400,'API_REQUEST_INVALID')
                action=series_act[2]
                if action is None:
                    if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                    with database(db_path) as db:
                        item=series_view(db,aid,language)
                    if item is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
                    return 200,item,[]
                if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
                return self._series_action(db_path,aid,action,body)

            if path==PREFIX+'/health' and method=='GET':
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                with database(db_path):pass
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
            if path==PREFIX+'/settings/sonarr/quality-profiles':
                if method!='GET':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                from src.anime.sonarr import KeepConfiguration,KeepUnavailable,quality_profiles
                try:profiles=quality_profiles(configuration,self.secrets.sonarr_api_key())
                except KeepConfiguration as exc:
                    raise ApiError(409,exc.reason_code,detail='Set the Sonarr address and API key first.') from None
                except KeepUnavailable as exc:
                    raise ApiError(503,exc.reason_code,detail='Sonarr did not answer.') from None
                return 200,{'profiles':profiles},[]
            if path==PREFIX+'/credentials/transcode':
                if method!='PATCH':raise ApiError(405,'API_METHOD_INVALID')
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                exact(body,('expected_revision','agent','token'))
                validate_secret_fields('transcode',{'api_key':body['token']},('api_key',))
                from src.anime.services.transcode import agent_url
                try:agent=agent_url(body['agent'])
                except (ValueError,TypeError):raise ApiError(400,'API_REQUEST_INVALID') from None
                result=self.secrets.update_transcode_token(agent,body['token'],expected_revision=body['expected_revision'])
                return 200,{'status':asdict(result.status),'saved':result.saved},[]
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
        if path in (PREFIX+'/',PREFIX+'/health',PREFIX+'/credentials'):raise ApiError(405,'API_METHOD_INVALID')
        raise ApiError(404,'API_ROUTE_NOT_FOUND')

    def _cookie(self,token,*,clear=False,secure=False):
        return COOKIE+'='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+('0' if clear else str(self.sessions.ttl))+('; Secure' if secure else '')

    def _notifications(self,configuration,method,path,query,body):
        """The bell: list, mark read (delete), mark all read, and item actions."""
        from src.anime.notifications import EVENT_TYPES
        from src.anime.storage.sqlite import transaction
        db_path=configuration.value('storage.database_path')
        if path==PREFIX+'/notifications':
            if method=='GET':
                if set(query)-{'before_id','limit'}:raise ApiError(400,'API_REQUEST_INVALID')
                before=page_number(query,'before_id',(1<<63)-1,1,(1<<63)-1)
                limit=page_number(query,'limit',20,1,100)
                with database(db_path) as db:
                    db.execute('BEGIN')
                    rows=inbox.items(db,before_id=before,limit=limit+1);unread=inbox.unread(db)
                items=[{**row,'actions':list(EVENT_TYPES[row['type']])} for row in rows[:limit]]
                return 200,{'items':items,'unread':unread,
                            'next_before_id':items[-1]['notification_id'] if len(rows)>limit else None},[]
            if method=='DELETE':
                if query:raise ApiError(400,'API_REQUEST_INVALID')
                with database(db_path) as db,transaction(db):deleted=inbox.delete_all(db)
                return 200,{'deleted':deleted,'unread':0},[]
            raise ApiError(405,'API_METHOD_INVALID')
        match=re.fullmatch(PREFIX+r'/notifications/([1-9][0-9]{0,18})(?:/actions/([a-z_]{1,32}))?',path)
        if not match or query:raise ApiError(404 if not match else 400,'API_ROUTE_NOT_FOUND' if not match else 'API_REQUEST_INVALID')
        number=int(match[1])
        if number>(1<<63)-1:raise ApiError(400,'API_REQUEST_INVALID')
        if match[2] is None:
            if method!='DELETE':raise ApiError(405,'API_METHOD_INVALID')
            with database(db_path) as db,transaction(db):
                if not inbox.delete(db,number):raise ApiError(404,'API_ROUTE_NOT_FOUND')
                unread=inbox.unread(db)
            return 200,{'deleted':1,'unread':unread},[]
        if method!='POST':raise ApiError(405,'API_METHOD_INVALID')
        exact(body,('anidb_id','tvdb_series_id','title','season') if match[2]=='match_by_hand' else ())
        with database(db_path) as db:
            db.execute('BEGIN')
            item=inbox.get(db,number)
        if item is None:raise ApiError(404,'API_ROUTE_NOT_FOUND')
        action=match[2]
        if action not in EVENT_TYPES[item['type']]:raise ApiError(400,'API_REQUEST_INVALID')
        return self._notification_action(configuration,db_path,item,action,body)

    def _notification_action(self,configuration,db_path,item,action,body=None):
        """Each action runs existing code; open_* are links the dashboard follows."""
        if action=='open_series':
            if item['anidb_id'] is None:raise ApiError(409,'MANAGEMENT_ACTION_UNAVAILABLE')
            return 200,{'action':action,'anidb_id':item['anidb_id']},[]
        if action=='add_release':
            # The existing manual release form, for this series (and episode when known).
            return 200,{'action':action,'anidb_id':item['anidb_id'],'release_name':item['release_name']},[]
        if action=='match_by_hand':
            # Save the hand match for this release name and season, then identify it again.
            from src.anime.storage.manual_identity import save, ManualMatchError
            if item['raw_body'] is None:raise ApiError(409,'MANAGEMENT_ACTION_UNAVAILABLE')
            try:
                save(configuration.value('storage.database_path'),bytes(item['raw_body']),
                     anidb_id=body['anidb_id'],tvdb_series_id=body['tvdb_series_id'],
                     title=body['title'],season=body['season'])
            except ManualMatchError:
                raise ApiError(409,'MANAGEMENT_ACTION_UNAVAILABLE') from None
        # process_anyway: the delivery exactly as the webhook would have queued it,
        # skipping the decision check for this one delivery (work item P4 4.5).
        if item['raw_body'] is None:raise ApiError(409,'MANAGEMENT_ACTION_UNAVAILABLE')
        from src.anime.identity_bridge import queue_release
        with database(db_path) as db:
            request_id=queue_release(db,bytes(item['raw_body']),process_anyway=True)
        from src.anime.storage.sqlite import transaction
        with database(db_path) as db,transaction(db):
            inbox.delete(db,item['notification_id'])
        self.bridge.wake()
        return 202,{'action':action,'request_id':request_id},[]

    def _recognized(self,db,language,match=None):
        where,params=match if match else ('1',[])
        rows=db.execute(
            "SELECT s.anidb_id,s.tvdb_series_id,s.display_title,s.anime_type,t.english_title,t.romaji_title,"
            "(SELECT count(*) FROM recognized_aliases a WHERE a.anidb_id=s.anidb_id),"
            "(SELECT count(*) FROM recognized_episodes v WHERE v.anidb_id=s.anidb_id),"
            "(SELECT count(*) FROM recognized_episodes v WHERE v.anidb_id=s.anidb_id AND v.tvdb_episode_id IS NOT NULL),"
            "(SELECT group_concat(tvdb_season) FROM (SELECT tvdb_season FROM recognized_seasons r "
            " WHERE r.anidb_id=s.anidb_id ORDER BY tvdb_season)) "
            "FROM recognized_series s LEFT JOIN anime_title_metadata t ON t.anidb_id=s.anidb_id "
            "WHERE ("+where+") ORDER BY s.display_title,s.anidb_id LIMIT 501",params).fetchall()
        if len(rows)>500:raise ApiError(503,'API_UNAVAILABLE')
        return {'items':[{'anidb_id':aid,'tvdb_series_id':tid,
            'title':preferred_title(title,english,romaji,language),'anime_type':kind,
            'seasons':[int(x) for x in seasons.split(',')] if seasons else [],
            'alias_count':alias_count,'verified_episode_count':ep_count,'placed_episode_count':placed}
            for aid,tid,title,kind,english,romaji,alias_count,ep_count,placed,seasons in rows]}

    def _search(self,configuration,query):
        # One route for every page; the scope decides which page's list is filtered.
        scope=query.get('scope',[]);text=query.get('q',[])
        if len(scope)!=1 or len(text)!=1 or scope[0] not in ('intake','recognized'):raise ApiError(400,'API_REQUEST_INVALID')
        try:found=search_match(scope[0],search_words(text[0]))
        except SearchInvalid:raise ApiError(400,'API_REQUEST_INVALID') from None
        rest={key:value for key,value in query.items() if key not in ('scope','q')}
        language=configuration.value('anime.title_language')
        with database(configuration.value('storage.database_path')) as db:
            db.execute('BEGIN')
            if scope[0]=='recognized':
                if rest:raise ApiError(400,'API_REQUEST_INVALID')
                return self._recognized(db,language,found)
        return self._intake(configuration,rest,match=found)

    def _intake(self,configuration,query,*,match=None):
        """Series, newest change first: one row each (the series job)."""
        if set(query)-{'before_id','limit','status'}:raise ApiError(400,'API_REQUEST_INVALID')
        limit=page_number(query,'limit',30,1,100)
        status=query.get('status',[None])[0]
        if status not in (None,'working','asleep','kept','rejected'):raise ApiError(400,'API_REQUEST_INVALID')
        with database(configuration.value('storage.database_path')) as db:
            return series_list(db,configuration.value('anime.title_language'),limit=limit,
                               before=page_number(query,'before_id',(1<<63)-1,1,(1<<63)-1),
                               state=None if status is None else status.upper(),match=match)

    def _series_action(self,db_path,aid,action,body):
        from src.anime.series_job import actions
        try:
            with database(db_path) as db:
                if action=='keep':result=actions.keep_series_sync(db,aid)
                elif action=='reject':result=actions.reject_series_sync(
                    db,aid,reason=body.get('reason','REJECTED_BY_USER'),note_text=body.get('note',''))
                elif action=='search':result=actions.search_series_sync(db,aid)
                elif action=='retry':result=actions.retry_series_sync(db,aid)
                elif action=='cancel':result=actions.cancel_series_sync(db,aid)
                else:
                    if set(body) not in ({'release_name','magnet_uri'},{'release_name','torrent_b64'}):
                        raise ApiError(400,'API_REQUEST_INVALID')
                    result=actions.add_release(db,aid,body['release_name'],magnet_uri=body.get('magnet_uri'),
                                               torrent_b64=body.get('torrent_b64'))
        except actions.SeriesNotFound:
            raise ApiError(404,'API_ROUTE_NOT_FOUND') from None
        except actions.SeriesDecided as exc:
            raise ApiError(409,exc.reason_code) from None
        except actions.ReleaseInvalid as exc:
            raise ApiError(400,exc.reason_code) from None
        return 200,result,[]


