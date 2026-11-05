"""Bounded session-local qBittorrent 4.5+ and 5.x transport. No automatic retries."""
from http.cookies import SimpleCookie
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from src.nextgen.acquisition.metadata import TorrentFile, parse_metainfo
from src.nextgen.acquisition.settings import DownloadOptions, DownloadCredentials
from src.nextgen.acquisition.submission import (Observation, SendPermit, ClientUnavailable, ClientInvalid,
                                               ClientAuthenticationFailed, ClientUnsupported)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


def _json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ClientInvalid()
            result[key]=value
        return result
    try:return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,
                          parse_constant=lambda value:(_ for _ in ()).throw(ClientInvalid()))
    except (ValueError,UnicodeError,RecursionError):raise ClientInvalid() from None


def _text(value,limit):
    if type(value) is not str or len(value)>limit or any(ord(c)<32 or ord(c)==127 for c in value):raise ClientInvalid()
    return value


class _Protocol:
    """Small version-specific API contract; no transport or acquisition policy."""
    def __init__(self,major,api_version):
        if (major not in (4,5) or type(api_version) is not tuple
                or len(api_version)!=3 or not (2,8,14) <= api_version < (3,0,0)):
            raise ClientUnsupported()
        self.major,self.api_version=major,api_version

    @property
    def metadata_strategy(self):
        if self.major == 5 and self.api_version >= (2,11,9):
            return 'fetch'
        return 'guarded_export'

    @property
    def stopped_parameter(self):
        return 'stopped' if self.api_version >= (2,11,0) else 'paused'

    @property
    def start_endpoint(self):
        return 'torrents/start' if self.api_version >= (2,11,0) else 'torrents/resume'

    @property
    def stop_endpoint(self):
        return 'torrents/stop' if self.api_version >= (2,11,0) else 'torrents/pause'

    def add_fields(self,stopped):
        fields={self.stopped_parameter:'true' if stopped else 'false',
                'autoTMM':'false'}
        fields['contentLayout']='Original'
        fields['useDownloadPath']='false'
        return fields

    def label_fields(self,marker):
        return {'tags':'ad-'+marker}


class Client:
    def __init__(self,options,credentials,*,transport=None):
        if type(options) is not DownloadOptions or type(credentials) is not DownloadCredentials:raise ValueError('validated client context required')
        self.options,self._credentials=options,credentials
        self._sid=None
        self.major=None
        self.api_version=None
        self.protocol=None
        opener=urllib.request.build_opener(_NoRedirect(),urllib.request.ProxyHandler({}))
        self._transport=transport or (lambda request,timeout:opener.open(request,timeout=timeout))

    def _request(self,path,*,query=None,form=None,body=None,content_type=None,limit=3145728):
        if path not in ('auth/login','app/version','app/webapiVersion','torrents/info','torrents/files','torrents/add','torrents/properties','torrents/filePrio','torrents/start','torrents/resume','torrents/stop','torrents/pause','torrents/delete','torrents/fetchMetadata','torrents/saveMetadata','torrents/export'):raise ValueError('unsupported client operation')
        url=self.options.base_url+'/api/v2/'+path
        if query:url+='?'+urllib.parse.urlencode(query)
        headers={'User-Agent':'AnimeDiscoveryNextGen/1','Referer':self.options.base_url+'/','Accept':'application/json'}
        if self._sid:headers['Cookie']='SID='+self._sid
        if form is not None:
            body=urllib.parse.urlencode(form).encode()
            content_type='application/x-www-form-urlencoded'
        if content_type:headers['Content-Type']=content_type
        request=urllib.request.Request(url,data=body,headers=headers,method='POST' if body is not None else 'GET')
        try:
            with self._transport(request,self.options.timeout_seconds) as response:
                if response.getcode() in (401,403):raise ClientAuthenticationFailed()
                code=response.getcode()
                if code not in (200,204) and not (path=='torrents/fetchMetadata' and code==202):
                    raise ClientUnavailable()
                if code==204 and path not in ('torrents/add','torrents/filePrio',
                        'torrents/start','torrents/resume','torrents/stop',
                        'torrents/pause','torrents/delete'):
                    raise ClientInvalid()
                raw=response.read(limit+1)
                if type(raw) is not bytes or len(raw)>limit:raise ClientInvalid()
                if path=='torrents/add' and raw.strip()==b'Fails.':
                    raise ClientInvalid()
                return raw,response.headers
        except urllib.error.HTTPError as exc:
            code=exc.code
            exc.close()
            if code in (401,403):raise ClientAuthenticationFailed() from None
            if code==400:raise ClientInvalid() from None
            if path=='torrents/saveMetadata' and code==409:
                # Metadata is still resolving; the caller retries later.
                return b'',{}
            raise ClientUnavailable() from None
        except (OSError,TimeoutError,urllib.error.URLError):raise ClientUnavailable() from None

    def login(self):
        self._sid=None
        self.major=None
        self.api_version=None
        self.protocol=None
        raw,headers=self._request('auth/login',form={'username':self._credentials.username,'password':self._credentials.password},limit=4096)
        if raw.strip()!=b'Ok.':raise ClientAuthenticationFailed()
        cookies=headers.get_all('Set-Cookie',[])
        if len(cookies)>10 or any(len(v)>4096 for v in cookies):raise ClientInvalid()
        found=[]
        for value in cookies:
            try:
                parsed=SimpleCookie();parsed.load(value)
                if 'SID' in parsed:found.append(parsed['SID'].value)
            except Exception:raise ClientInvalid() from None
        # qBittorrent generates Base64 SIDs, which can include +, / and =.
        if len(found)!=1 or not re.fullmatch('[A-Za-z0-9_+/=-]{8,256}',found[0]):raise ClientAuthenticationFailed()
        self._sid=found[0]
        version,_=self._request('app/version',limit=128)
        api,_=self._request('app/webapiVersion',limit=128)
        try:
            match=re.fullmatch(r'v?(4|5)\.([0-9]+)\.[0-9]+',version.decode().strip())
            api_match=re.fullmatch(r'2\.([0-9]+)(?:\.[0-9]+)?',api.decode().strip())
            if not match or not api_match:raise ClientUnsupported()
            if match[1]=='4' and int(match[2])<5:raise ClientUnsupported()
            self.major=int(match[1])
            api_parts=api.decode().strip().split('.')
            self.api_version=tuple(int(part) for part in api_parts[:3])
            self.api_version+=(0,)*(3-len(self.api_version))
            self.protocol=_Protocol(self.major,self.api_version)
        except UnicodeError:raise ClientUnsupported() from None

    @property
    def metadata_strategy(self):
        if not self._sid or self.protocol is None:
            raise ClientAuthenticationFailed()
        return self.protocol.metadata_strategy

    def fetch_metadata(self,source,expected_hash,*,batch=False):
        """5.2+ metadata-only fetch without torrent submission or selection.

        Batch mode validates a multi-video inventory; single-episode mode
        retains its existing one-video Manifest contract.
        """
        from src.nextgen.acquisition.prowlarr_search import _magnet_hash
        from src.nextgen.acquisition.metadata import MAX_METAINFO, infohash, parse_batch_metainfo
        if type(batch) is not bool:raise ClientInvalid()
        expected_hash=infohash(expected_hash)
        if (self.metadata_strategy!='fetch' or
                source!=expected_hash and _magnet_hash(source)!=expected_hash):
            raise ClientInvalid()
        raw,_=self._request('torrents/fetchMetadata',form={'source':source},limit=MAX_METAINFO)
        data=_json(raw)
        if type(data) is not dict:raise ClientInvalid()
        if not data:return None
        if data.get('infohash_v1') != expected_hash:
            raise ClientInvalid()
        # qBittorrent returns a hash-only description while it is resolving.
        if 'info' not in data:return None
        if type(data['info']) is not dict:raise ClientInvalid()
        raw,_=self._request('torrents/saveMetadata',form={'source':source},limit=MAX_METAINFO)
        if not raw:return None
        return (parse_batch_metainfo if batch else parse_metainfo)(raw,expected_hash),raw

    def export_metainfo(self,expected_hash,*,batch=False):
        """Hash-verified export for a previously admitted stopped torrent.

        Export is read-only. It is not permission to adopt, start or replace a
        torrent, and it never grants a send permit. Batch mode returns a typed
        multi-video inventory instead of a single-episode manifest.
        """
        from src.nextgen.acquisition.metadata import MAX_METAINFO, infohash, parse_batch_metainfo
        if self.protocol is None or type(batch) is not bool:
            raise ClientUnsupported()
        expected_hash=infohash(expected_hash)
        raw,_=self._request('torrents/export',query={'hash':expected_hash},limit=MAX_METAINFO)
        return (parse_batch_metainfo if batch else parse_metainfo)(raw,expected_hash),raw

    def inspect(self,expected_hash,*,include_files=True):
        if not self._sid or self.major is None:raise ClientAuthenticationFailed()
        from src.nextgen.acquisition.metadata import infohash
        expected_hash=infohash(expected_hash)
        raw,_=self._request('torrents/info',query={'hashes':expected_hash,'limit':2})
        rows=_json(raw)
        if type(rows) is not list or len(rows)>1:raise ClientInvalid()
        if not rows:return None
        row=rows[0]
        if type(row) is not dict or row.get('hash')!=expected_hash or type(row.get('auto_tmm')) is not bool:raise ClientInvalid()
        tags=_text(row.get('tags'),8192).split(',') if row.get('tags') else []
        tags=tuple(t.strip() for t in tags if t.strip())
        if len(tags)>128 or any(len(t)>128 for t in tags):raise ClientInvalid()
        state=_text(row.get('state'),64)
        save_path=_text(row.get('save_path'),4096)
        category=_text(row.get('category'),256)
        if type(include_files) is not bool:raise ClientInvalid()
        # Metadata polling normally checks only status; periodically read files
        # while metaDL, and always read files once the torrent changes state.
        if not include_files and state=='metaDL':
            return Observation(expected_hash,state,save_path,category,tags,row['auto_tmm'],())
        raw,_=self._request('torrents/files',query={'hash':expected_hash})
        files=_json(raw)
        if type(files) is not list or len(files)>10000:raise ClientInvalid()
        result=[]
        try:
            for position,item in enumerate(files):
                if type(item) is not dict:raise ClientInvalid()
                result.append(TorrentFile(item.get('index'),item.get('name'),item.get('size')))
            result.sort(key=lambda f:f.index)
            if [f.index for f in result]!=list(range(len(result))):raise ClientInvalid()
        except ValueError:raise ClientInvalid() from None
        return Observation(expected_hash,state,save_path,category,tags,row['auto_tmm'],tuple(result))

    def inspect_legacy_metadata(self,expected_hash,marker,download_root):
        """Read-only pre-manifest observation for a journaled magnet intake.

        Metadata is not a send permit. Empty file lists are pending; snapshots
        are usable only after full ownership readback and a stopped state.
        Never claim that qBittorrent's files alone prove BEP3 info bytes.
        """
        from src.nextgen.acquisition.metadata import infohash, MetadataInvalid
        expected_hash=infohash(expected_hash)
        if (not self._sid or self.protocol is None
                or type(marker) is not str or not re.fullmatch('[a-f0-9]{32}',marker)
                or type(download_root) is not str
                or download_root!=self.options.download_root
                or not download_root.endswith('/ad-'+marker)):
            raise ClientInvalid()
        snapshot=self.inspect(expected_hash)
        if snapshot is None:
            return None
        if (snapshot.save_path.rstrip('/')!=download_root
                or snapshot.category!=self.options.category
                or snapshot.auto_tmm is not False
                or 'ad-'+marker not in snapshot.tags
                or any(t.startswith('ad-') and t!='ad-'+marker for t in snapshot.tags)):
            raise ClientInvalid()
        if not snapshot.files:
            return None
        if snapshot.state not in ('pausedDL','pausedUP','stoppedDL','stoppedUP'):
            return None
        # Read the exact same inventory again, rejecting concurrent changes.
        confirmed=self.inspect(expected_hash)
        if confirmed!=snapshot:
            raise ClientInvalid()
        return snapshot

    def add_legacy_magnet(self,magnet,intent,permit):
        """One bounded pre-manifest admission for the 4.5+ API.

        A durable Sonarr magnet journal must issue the permit before this
        call. Never invoke add again on an uncertain or recovered intent.
        """
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent, MagnetPermit
        from src.nextgen.acquisition.prowlarr_search import _magnet_hash
        if (not self._sid or self.protocol is None):
            raise ClientAuthenticationFailed()
        if (type(intent) is not MagnetIntent or type(permit) is not MagnetPermit
                or intent.options!=self.options or intent.api_major!=self.major
                or intent.state!='UNCERTAIN'
                or self.metadata_strategy!='guarded_export'
                or _magnet_hash(magnet)!=intent.info_hash
                or not self.options.download_root.endswith('/ad-'+intent.marker)):
            raise ClientInvalid()
        fields={'urls':magnet,'savepath':self.options.download_root,
                'category':self.options.category,
                **self.protocol.label_fields(intent.marker),
                **self.protocol.add_fields(True)}
        permit.take(intent)
        # The first add is PAUSED. No discovery window opens before marker
        # readback and a separately bounded resume.
        self._request('torrents/add',form=fields,limit=4096)

    def poll_legacy_metadata(self,intent,cancelled,*,attempts=24,
                             interval=5,sleep=None,trace=None):
        """Bounded 4.5+ metadata discovery window with unconditional stop.

        Never resumes when the file list already exists. A restart may run a
        NEW bounded metadata window, but cannot re-submit the magnet.
        """
        import time
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        from src.nextgen.storage.batch_metadata_admission import EarlyBatchIntent
        if (type(intent) not in (MagnetIntent,EarlyBatchIntent) or intent.state!='UNCERTAIN'
                or intent.options!=self.options
                or self.protocol is None
                or type(attempts) is not int or not 1<=attempts<=24
                or type(interval) not in (int,float) or not 0<=interval<=10):
            raise ClientInvalid()
        sleep=time.sleep if sleep is None else sleep
        if not callable(sleep):raise ClientInvalid()
        before=self.inspect(intent.info_hash)
        if trace is not None:
            trace.update(initial_state=getattr(before,'state',None),
                         last_active_state=getattr(before,'state',None),
                         stopped_state=None,polls=0,
                         max_files=len(getattr(before,'files',())),
                         resumed=False)
        def matches(row):
            return (row is not None and row.info_hash==intent.info_hash
                    and row.category==intent.options.category
                    and row.save_path.rstrip('/')==intent.options.download_root
                    and row.auto_tmm is False
                    and 'ad-'+intent.marker in row.tags
                    and not any(t.startswith('ad-') and t!='ad-'+intent.marker
                                for t in row.tags))
        if not matches(before):
            raise ClientInvalid()
        if before.state not in ('pausedDL','pausedUP','stoppedDL','stoppedUP'):
            # A previous lease may have died during metadata discovery.
            # Stop before considering another start.
            stopped=self.stop_legacy_metadata(intent)
            if trace is not None:
                trace['phase']='recover_stop'
                trace['stopped_state']=getattr(stopped,'state',None)
                trace['max_files']=max(trace['max_files'],len(getattr(stopped,'files',())))
            return stopped
        if before.files:
            return self.inspect_legacy_metadata(intent.info_hash,intent.marker,
                                                intent.options.download_root)
        if cancelled.is_set():return None
        try:
            self._request(self.protocol.start_endpoint,
                          form={'hashes':intent.info_hash},limit=4096)
            if trace is not None:
                trace['resumed']=True
            for index in range(attempts):
                if cancelled.is_set():break
                current=self.inspect(intent.info_hash,include_files=(index%3==0))
                if trace is not None:
                    trace['polls']=index+1
                    trace['last_active_state']=getattr(current,'state',None)
                    trace['max_files']=max(trace['max_files'],len(getattr(current,'files',())))
                if not matches(current):raise ClientInvalid()
                if current.files:break
                if index+1<attempts:sleep(interval)
        finally:
            # Do not leave metadata discovery active on cancellation, timeouts
            # or invalid responses. Network failure remains an uncertain state.
            stopped=self.stop_legacy_metadata(intent)
            if trace is not None:
                trace['stopped_state']=getattr(stopped,'state',None)
                trace['max_files']=max(trace['max_files'],len(getattr(stopped,'files',())))
        return stopped

    def stop_legacy_metadata(self,intent):
        """Stop a journal-owned magnet before any file inventory is accepted."""
        from src.nextgen.storage.sonarr_magnet_intake import MagnetIntent
        from src.nextgen.storage.batch_metadata_admission import EarlyBatchIntent
        if (type(intent) not in (MagnetIntent,EarlyBatchIntent) or intent.options!=self.options
                or intent.state not in ('UNCERTAIN','OWNED_STOPPED')
                or self.protocol is None):
            raise ClientInvalid()
        before=self.inspect(intent.info_hash)
        if (before is None or before.category!=intent.options.category
                or before.save_path.rstrip('/')!=intent.options.download_root
                or before.auto_tmm is not False or 'ad-'+intent.marker not in before.tags
                or any(tag.startswith('ad-') and tag!='ad-'+intent.marker
                       for tag in before.tags)):
            raise ClientInvalid()
        if before.state not in ('pausedDL','pausedUP','stoppedDL','stoppedUP'):
            self._request(self.protocol.stop_endpoint,
                          form={'hashes':intent.info_hash},limit=4096)
        after=self.inspect(intent.info_hash)
        if after is None or after.state not in ('pausedDL','pausedUP','stoppedDL','stoppedUP'):
            return None
        if not after.files:
            return None
        return self.inspect_legacy_metadata(intent.info_hash,intent.marker,
                                            intent.options.download_root)

    def delete_owned(self,info_hash,marker,save_path):
        """Cancel cleanup. Returns what it found:
        'absent'  - no such torrent;
        'foreign' - no ad-<marker> tag: not ours, untouched;
        'moved'   - ours, but stored outside its recorded folder: untouched;
        'deleted' - ours and where we put it: deleted with its files.
        """
        if self.protocol is None:raise ClientAuthenticationFailed()
        observed=self.inspect(info_hash,include_files=False)
        if observed is None:return 'absent'
        if 'ad-'+marker not in observed.tags:return 'foreign'
        if observed.save_path.rstrip('/')!=save_path.rstrip('/'):return 'moved'
        self._request('torrents/delete',form={'hashes':observed.info_hash,'deleteFiles':'true'},limit=4096)
        return 'deleted'

    def add_stopped(self,raw,intent,permit):
        """Single journaled submission for verified .torrent bytes or a magnet."""
        if not self._sid or self.major is None:raise ClientAuthenticationFailed()
        if type(permit) is not SendPermit or intent.options!=self.options or intent.api_major!=self.major:raise ClientInvalid()
        from src.nextgen.acquisition.metadata import ObservedInventory
        if type(intent.plan.manifest) is ObservedInventory:raise ClientInvalid()
        if type(raw) is str:
            # Exactly the same journal permit protects both source formats.
            from src.nextgen.acquisition.prowlarr_search import _magnet_hash
            if _magnet_hash(raw)!=intent.plan.manifest.info_hash:
                raise ClientInvalid()
            fields={'urls':raw,'savepath':self.options.download_root,
                    'category':self.options.category,**self.protocol.label_fields(intent.plan.marker),
                    **self.protocol.add_fields(True),'skip_checking':'false'}
            permit.take(intent)
            self._request('torrents/add',form=fields,limit=4096)
            return
        manifest=parse_metainfo(raw,intent.plan.manifest.info_hash)
        if manifest!=intent.plan.manifest:raise ClientInvalid()
        boundary='ad'+uuid.uuid4().hex
        while boundary.encode() in raw:boundary='ad'+uuid.uuid4().hex
        fields={'savepath':self.options.download_root,'category':self.options.category,
                **self.protocol.label_fields(intent.plan.marker),
                **self.protocol.add_fields(True),'skip_checking':'false'}
        chunks=[]
        for key,value in fields.items():
            chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+value+'\r\n').encode())
        chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="torrents"; filename="source.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n').encode()+raw+b'\r\n')
        chunks.append(('--'+boundary+'--\r\n').encode())
        # Consume before the single transport call. Never retry, even on HTTP error.
        permit.take(intent)
        self._request('torrents/add',body=b''.join(chunks),content_type='multipart/form-data; boundary='+boundary,limit=4096)

    def inspect_transfer(self,expected_hash):
        from src.nextgen.acquisition.transfers import TransferSnapshot, SnapshotChanged
        before=self.inspect(expected_hash)
        if before is None:return None
        raw,_=self._request('torrents/files',query={'hash':expected_hash})
        files=_json(raw)
        if type(files) is not list or len(files)!=len(before.files):raise ClientInvalid()
        rows=[]
        try:
            for position,row in enumerate(files):
                if type(row) is not dict:raise ClientInvalid()
                item=TorrentFile(row.get('index'),row.get('name'),row.get('size'))
                ranges=row.get('piece_range')
                if type(ranges) is not list or len(ranges)!=2:raise ClientInvalid()
                rows.append((item,row.get('priority'),row.get('progress'),tuple(ranges)))
            rows.sort(key=lambda r:r[0].index)
        except ValueError:raise ClientInvalid() from None
        if tuple(r[0] for r in rows)!=before.files:raise SnapshotChanged()
        raw,_=self._request('torrents/properties',query={'hash':expected_hash})
        properties=_json(raw)
        if type(properties) is not dict:raise ClientInvalid()
        after=self.inspect(expected_hash)
        if before!=after:raise SnapshotChanged()
        return TransferSnapshot(after,tuple(r[1] for r in rows),tuple(r[2] for r in rows),
                                properties.get('piece_size'),tuple(r[3] for r in rows))

    def apply_selection(self,intent,fingerprint,permit,action):
        if not self._sid or self.major not in (4,5):raise ClientAuthenticationFailed()
        from src.nextgen.acquisition.transfers import (TransferPermit, TransferConflict, STOPPED, verify_snapshot, selection_fingerprint)
        if type(permit) is not TransferPermit or self.options!=intent.options:raise ClientInvalid()
        snapshot=self.inspect_transfer(intent.plan.manifest.info_hash)
        if snapshot is None:raise TransferConflict()
        verify_snapshot(intent,snapshot)
        if selection_fingerprint(intent,snapshot.piece_size)!=fingerprint:raise TransferConflict()
        if snapshot.observation.state not in STOPPED:raise TransferConflict()
        selected=intent.plan.manifest.selected_index
        if action=='skip':indices=[i for i in range(len(snapshot.priorities)) if i!=selected];priority=0
        elif action=='wanted':indices=[selected];priority=1
        else:raise TransferConflict()
        permit.take(fingerprint,action)
        if indices:self._request('torrents/filePrio',form={'hash':intent.plan.manifest.info_hash,
            'id':'|'.join(map(str,indices)),'priority':priority},limit=4096)

    def start_selected(self,intent,fingerprint,permit):
        if not self._sid or self.major not in (4,5):raise ClientAuthenticationFailed()
        from src.nextgen.acquisition.transfers import (TransferPermit, TransferConflict, STOPPED, verify_snapshot, exact_selection, selection_fingerprint)
        if type(permit) is not TransferPermit or self.options!=intent.options:raise ClientInvalid()
        snapshot=self.inspect_transfer(intent.plan.manifest.info_hash)
        if snapshot is None:raise TransferConflict()
        verify_snapshot(intent,snapshot)
        if selection_fingerprint(intent,snapshot.piece_size)!=fingerprint:raise TransferConflict()
        if snapshot.observation.state not in STOPPED or not exact_selection(intent,snapshot):raise TransferConflict()
        permit.take(fingerprint,'start')
        self._request(self.protocol.start_endpoint,
                      form={'hashes':intent.plan.manifest.info_hash},limit=4096)

    def cleanup_control(self,intent,fingerprint,permit,action):
        from src.nextgen.acquisition.cleanup import CleanupPermit, owned
        from src.nextgen.acquisition.transfers import STOPPED
        if not self._sid or self.major not in (4,5):raise ClientAuthenticationFailed()
        if type(permit) is not CleanupPermit or self.options!=intent.options or fingerprint!=intent.fingerprint:raise ClientInvalid()
        current=owned(intent,self.inspect(intent.plan.manifest.info_hash))
        if action=='remove' and current.state not in STOPPED:raise ClientInvalid()
        if action not in ('stop','remove'):raise ClientInvalid()
        permit.take(fingerprint,action)
        if action=='stop':
            self._request(self.protocol.stop_endpoint,
                          form={'hashes':intent.plan.manifest.info_hash},limit=4096)
        else:
            # Staging is already committed; qBittorrent deletes its original
            # payload. The owned ad-* folder is removed separately if needed.
            self._request('torrents/delete',form={'hashes':intent.plan.manifest.info_hash,'deleteFiles':'true'},limit=4096)
