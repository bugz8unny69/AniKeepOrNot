"""Bounded session-local qBittorrent 4.5+ and 5.x client. No automatic retries.

Version differences live in qbittorrent_versions.py (picked at login). This
module never touches files; files() and changes() report them uniformly.
"""
from http.cookies import CookieError, SimpleCookie
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from src.anime.acquisition.metadata import TorrentFile, parse_metainfo
from src.anime.acquisition.metadata_service import STOPPED, verify_torrent_ownership
from src.anime.acquisition.settings import DownloadOptions, DownloadCredentials
from src.anime.torrent_client.contract import Changes, ClientFile, TorrentChange
from src.anime.torrent_client.qbittorrent_versions import select_adapter
from src.anime.acquisition.submission import (Observation, ClientUnavailable, ClientInvalid,
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
        if path not in ('auth/login','app/version','app/webapiVersion','torrents/info','torrents/files','torrents/add','torrents/properties','torrents/filePrio','torrents/start','torrents/resume','torrents/stop','torrents/pause','torrents/delete','torrents/fetchMetadata','torrents/saveMetadata','torrents/export','sync/maindata'):raise ValueError('unsupported client operation')
        url=self.options.base_url+'/api/v2/'+path
        if query:url+='?'+urllib.parse.urlencode(query)
        headers={'User-Agent':'akon/1','Referer':self.options.base_url+'/','Accept':'application/json'}
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
            except CookieError:raise ClientInvalid() from None
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
            self.protocol=select_adapter(self.major,self.api_version)
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
        from src.anime.acquisition.prowlarr_search import _magnet_hash
        from src.anime.acquisition.metadata import MAX_METAINFO, infohash, parse_batch_metainfo
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
        from src.anime.acquisition.metadata import MAX_METAINFO, infohash, parse_batch_metainfo
        if self.protocol is None or type(batch) is not bool:
            raise ClientUnsupported()
        expected_hash=infohash(expected_hash)
        raw,_=self._request('torrents/export',query={'hash':expected_hash},limit=MAX_METAINFO)
        return (parse_batch_metainfo if batch else parse_metainfo)(raw,expected_hash),raw

    def inspect(self,expected_hash,*,include_files=True):
        if not self._sid or self.major is None:raise ClientAuthenticationFailed()
        from src.anime.acquisition.metadata import infohash
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

    def _add_file(self,fields,raw):
        boundary='ad'+uuid.uuid4().hex
        while boundary.encode() in raw:boundary='ad'+uuid.uuid4().hex
        chunks=[]
        for key,value in fields.items():
            chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+value+'\r\n').encode())
        chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="torrents"; filename="source.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n').encode()+raw+b'\r\n')
        chunks.append(('--'+boundary+'--\r\n').encode())
        self._request('torrents/add',body=b''.join(chunks),content_type='multipart/form-data; boundary='+boundary,limit=4096)

    # Series torrents (the series job). The client is built with the torrent's own
    # options: download_root is <download root>/ad-<marker>, as for every AKON torrent.
    # Each call reads the torrent back and acts only on one AKON owns (marker tag,
    # category, folder); the version adapter picks the spelling (pause/stop, paused/stopped).

    def _series_owned(self,info_hash,marker):
        from src.anime.acquisition.metadata import infohash
        if (not self._sid or self.protocol is None):raise ClientAuthenticationFailed()
        if (type(marker) is not str or not re.fullmatch('[a-f0-9]{32}',marker)
                or not self.options.download_root.endswith('/ad-'+marker)):raise ClientInvalid()
        observed=self.inspect(infohash(info_hash))
        if observed is not None and not verify_torrent_ownership(
                observed,marker,self.options.download_root,self.options.category):
            raise ClientInvalid('The torrent in qBittorrent is not the one AKON added (tag, category or folder).')
        return observed

    def series_torrent(self,info_hash,marker):
        """The owned torrent (Observation, with files once qBittorrent has the metadata), or None."""
        return self._series_owned(info_hash,marker)

    def add_series_torrent(self,info_hash,marker,*,magnet=None,raw=None):
        """Add stopped, labelled with the marker. The caller records the add before calling,
        so an add whose outcome is unknown is never sent again."""
        from src.anime.acquisition.prowlarr_search import _magnet_hash
        from src.anime.acquisition.metadata import infohash, parse_batch_metainfo
        if self._series_owned(info_hash,marker) is not None:raise ClientInvalid('Already in qBittorrent.')
        fields={'savepath':self.options.download_root,'category':self.options.category,
                **self.protocol.label_fields(marker),**self.protocol.add_fields(True),'skip_checking':'false'}
        if (magnet is None)==(raw is None):raise ClientInvalid()
        if magnet is not None:
            if _magnet_hash(magnet)!=infohash(info_hash):raise ClientInvalid()
            self._request('torrents/add',form={'urls':magnet,**fields},limit=4096)
        else:
            parse_batch_metainfo(raw,infohash(info_hash))      # The bytes are this torrent.
            self._add_file(fields,raw)

    def start_series_torrent(self,info_hash,marker):
        if self._series_owned(info_hash,marker) is None:raise ClientInvalid('Not in qBittorrent.')
        self._request(self.protocol.start_endpoint,form={'hashes':info_hash},limit=4096)

    def stop_series_torrent(self,info_hash,marker):
        if self._series_owned(info_hash,marker) is None:raise ClientInvalid('Not in qBittorrent.')
        self._request(self.protocol.stop_endpoint,form={'hashes':info_hash},limit=4096)

    def select_series_files(self,info_hash,marker,wanted):
        """Download only the wanted file indices; every other file is skipped."""
        observed=self._series_owned(info_hash,marker)
        if observed is None or not observed.files:raise ClientInvalid('No file list yet.')
        indices={f.index for f in observed.files}
        wanted=set(wanted)
        if not wanted or not wanted<=indices:raise ClientInvalid()
        for priority,chosen in ((0,sorted(indices-wanted)),(1,sorted(wanted))):
            if chosen:
                self._request('torrents/filePrio',form={'hash':info_hash,'id':'|'.join(map(str,chosen)),
                                                        'priority':priority},limit=4096)

    def remove_series_torrent(self,info_hash,marker):
        """Delete the owned torrent and its files. Returns delete_owned's answer."""
        if (type(marker) is not str or not re.fullmatch('[a-f0-9]{32}',marker)):raise ClientInvalid()
        return self.delete_owned(info_hash,marker,self.options.download_root)

    def free_space(self):
        """Free bytes on qBittorrent's download disk, as qBittorrent reports it."""
        free=self.changes(0).free_space
        if free is None:raise ClientInvalid()
        return free

    # Uniform reports (JD-1b). The same shape for every supported version, so
    # `resources` can track a torrent's files without knowing the client.

    def files(self,expected_hash):
        """Every file of the torrent as ClientFile (absolute path), or None if absent."""
        if not self._sid or self.major is None:raise ClientAuthenticationFailed()
        from src.anime.acquisition.metadata import infohash
        expected_hash=infohash(expected_hash)
        raw,_=self._request('torrents/info',query={'hashes':expected_hash,'limit':2})
        rows=_json(raw)
        if type(rows) is not list or len(rows)>1:raise ClientInvalid()
        if not rows:return None
        row=rows[0]
        if type(row) is not dict or row.get('hash')!=expected_hash:raise ClientInvalid()
        save_path=_text(row.get('save_path'),4096).rstrip('/')
        if not save_path.startswith('/'):raise ClientInvalid()
        raw,_=self._request('torrents/files',query={'hash':expected_hash})
        items=_json(raw)
        if type(items) is not list or len(items)>10000:raise ClientInvalid()
        result=[]
        for item in items:
            if type(item) is not dict:raise ClientInvalid()
            index,name,size=item.get('index'),item.get('name'),item.get('size')
            progress,priority=item.get('progress'),item.get('priority')
            if (type(index) is not int or index<0 or type(size) is not int or size<0
                    or type(priority) is not int or priority<0
                    or type(progress) not in (int,float) or not 0<=progress<=1):raise ClientInvalid()
            name=_text(name,4096)
            if not name or name.startswith('/') or '\x00' in name or any(part in ('','.','..') for part in name.split('/')):
                raise ClientInvalid()
            result.append(ClientFile(index,save_path+'/'+name,size,float(progress),priority>0))
        result.sort(key=lambda f:f.index)
        if [f.index for f in result]!=list(range(len(result))):raise ClientInvalid()
        return tuple(result)

    _CHANGE_FIELDS={'state':str,'progress':float,'downloaded':int,'size':int,
                    'save_path':str,'category':str,'tags':str,
                    'dlspeed':int,'eta':int,'amount_left':int}

    def changes(self,rid=0):
        """One incremental update for all torrents (sync/maindata): what changed since rid.

        One request covers every torrent, so following many downloads costs one
        call per poll, not one per torrent.
        """
        if not self._sid or self.major is None:raise ClientAuthenticationFailed()
        if type(rid) is not int or rid<0:raise ClientInvalid()
        raw,_=self._request('sync/maindata',query={'rid':rid},limit=33554432)
        data=_json(raw)
        if type(data) is not dict or type(data.get('rid')) is not int or data['rid']<0:raise ClientInvalid()
        full=data.get('full_update',False)
        if type(full) is not bool:raise ClientInvalid()
        torrents={}
        for digest,fields in (data.get('torrents') or {}).items():
            if type(digest) is not str or type(fields) is not dict:raise ClientInvalid()
            if not re.fullmatch('[0-9a-f]{40}',digest):continue     # v2-only hashes are not ours
            seen={}
            for key,kind in self._CHANGE_FIELDS.items():
                if key not in fields:continue
                value=fields[key]
                if kind is float:
                    if type(value) not in (int,float) or not 0<=value<=1:raise ClientInvalid()
                    value=float(value)
                elif kind is int:
                    if type(value) is not int or value<0:raise ClientInvalid()
                else:
                    value=_text(value,4096)
                seen[key]=value
            torrents[digest]=TorrentChange(**seen)
        removed=data.get('torrents_removed') or []
        if type(removed) is not list or any(type(h) is not str for h in removed):raise ClientInvalid()
        server=data.get('server_state') or {}
        if type(server) is not dict:raise ClientInvalid()
        free=server.get('free_space_on_disk')
        if free is not None and (type(free) is not int or free<0):raise ClientInvalid()
        return Changes(data['rid'],full,torrents,tuple(h for h in removed if re.fullmatch('[0-9a-f]{40}',h)),free)
