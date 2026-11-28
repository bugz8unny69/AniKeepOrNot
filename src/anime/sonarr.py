"""Bounded Sonarr v4 API v3 operations for one explicitly selected staged file."""
from dataclasses import dataclass, field, asdict
from pathlib import PurePosixPath
import json
import http.client
import os
import stat
import re
import urllib.error
import urllib.parse
import urllib.request

from src.anime.acquisition.settings import endpoint
from src.anime.acquisition.metadata import safe_path
from src.anime.torrent_client.qbittorrent import _NoRedirect, _json
from src.anime.acquisition.cleanup import directory, parent


class KeepReview(ValueError):
    reason_code='KEEP_IMPORT_REVIEW'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'The library import needs review.')
        self.failed_check = failed_check
        self.failed_path = failed_path


# One sentence for the Keep button and the job: Keep adds new series with this profile.
PROFILE_MISSING='choose the Sonarr quality profile in Settings; Keep adds new series with it'


class KeepConfiguration(KeepReview):
    reason_code='KEEP_CONFIGURATION_INVALID'
    def __init__(self,message=None):
        ValueError.__init__(self,message or 'Configure Sonarr and the permanent library folder before Keep.')
        self.failed_check=self.failed_path=None


class KeepUnavailable(RuntimeError):
    reason_code = 'SERVICE_UNAVAILABLE'
    service = 'sonarr'
    def __init__(self):super().__init__('Sonarr is temporarily unavailable.')


@dataclass(frozen=True)
class Options:
    base_url: str = field(repr=False)
    keep_root: str = field(repr=False)
    staging_root: str = field(repr=False)
    library_root: str = field(repr=False)
    quality_profile_id: int
    timeout_seconds: int
    # Defaults keep journals recorded before these settings readable.
    long_running_threshold: int = 100
    backfill_long_running: bool = False
    def __post_init__(self):
        try:object.__setattr__(self,'base_url',endpoint(self.base_url))
        except ValueError:raise KeepConfiguration() from None
        for value in (self.keep_root,self.staging_root,self.library_root):
            if (type(value) is not str or not value.startswith('/') or value=='/' or str(PurePosixPath(value))!=value
                    or any(x in ('.','..') for x in value.split('/')) or any(ord(c)<32 for c in value)):raise KeepConfiguration()
        if type(self.quality_profile_id) is not int or self.quality_profile_id<0:raise KeepConfiguration()
        if type(self.timeout_seconds) is not int or not 1<=self.timeout_seconds<=60:raise KeepConfiguration()
        if type(self.long_running_threshold) is not int or self.long_running_threshold<0:raise KeepConfiguration()
        if type(self.backfill_long_running) is not bool:raise KeepConfiguration()

    @classmethod
    def capture(cls,configuration,selection):
        return cls(configuration.value('sonarr.base_url'),configuration.value('storage.keep_root'),
            configuration.value('sonarr.staging_root') or selection.staging_root,
            configuration.value('sonarr.library_root') or configuration.value('storage.keep_root'),
            configuration.value('sonarr.quality_profile_id'),configuration.value('sonarr.timeout_seconds'),
            configuration.value('sonarr.long_running_threshold'),configuration.value('sonarr.backfill_long_running'))

    def data(self):return asdict(self)


def api_client(configuration,key):
    """A Sonarr client for calls that touch no folders (profiles, episodes, queue, release push)."""
    unused='/unused'   # These calls read no folders; Options requires the fields.
    options=Options(configuration.value('sonarr.base_url'),unused,unused,unused,0,
                    configuration.value('sonarr.timeout_seconds'))
    return Client(options,key)


def quality_profiles(configuration,key):
    """Sonarr's quality profiles as [{id, name}], for the Settings drop-down.

    Sonarr shows profile names but not their IDs; the setting stores the ID.
    """
    return [{'id':row['id'],'name':row['name']} for row in api_client(configuration,key).request('qualityprofile')
            if type(row.get('id')) is int and type(row.get('name')) is str]


def remote_source(options,selection):return str(PurePosixPath(options.staging_root)/selection.media_relative_path)


def existing_file(options,series,episode,record,expected_size):
    """Confirm Sonarr's binding and the local regular file, without hashing."""
    if (type(record) is not dict or type(record.get('id')) is not int or record['id']<=0
            or record.get('id')!=episode.get('episodeFileId') or record.get('seriesId')!=series['id']
            or record.get('size')!=expected_size):raise KeepReview()
    try:
        remote=PurePosixPath(record['path']);relative=str(remote.relative_to(options.library_root))
        safe_path(relative)
        if not remote.is_relative_to(PurePosixPath(series['path'])):raise KeepReview()
        with directory(options.keep_root) as root, parent(root,relative) as (folder,name):
            info=os.stat(name,dir_fd=folder,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size!=expected_size:raise KeepReview()
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=folder)
            try:
                fresh=os.fstat(fd)
                if (fresh.st_dev,fresh.st_ino,fresh.st_size)!=(info.st_dev,info.st_ino,expected_size):raise KeepReview()
            finally:os.close(fd)
        return {'episode_file_id':record['id'],'relative_path':relative,'size':expected_size}
    except (ValueError,KeyError,TypeError):raise KeepReview() from None


class Client:
    def __init__(self,options,key,*,transport=None):
        if type(options) is not Options or type(key) is not str or not key:raise KeepConfiguration()
        self.options,self._key=options,key
        opener=urllib.request.build_opener(_NoRedirect(),urllib.request.ProxyHandler({}))
        self.transport=transport or (lambda request,timeout:opener.open(request,timeout=timeout))

    def request(self,path,*,query=None,body=None):
        # POST adds a series, previews a manual import, or sends a command.
        if body is not None and path not in ('series','manualimport','command','release/push'):raise KeepReview()
        if path not in ('system/status','series','series/lookup','rootfolder','qualityprofile','episode','manualimport','command','queue','release/push') and not re.fullmatch(r'(?:command|episodefile)/[1-9][0-9]*',path):raise KeepReview()
        url=self.options.base_url+'/api/v3/'+path
        if query:url+='?'+urllib.parse.urlencode(query)
        headers={'X-Api-Key':self._key,'Accept':'application/json','Content-Type':'application/json'}
        request=urllib.request.Request(url,headers=headers,method='GET' if body is None else 'POST',
            data=None if body is None else json.dumps(body,allow_nan=False).encode())
        try:
            with self.transport(request,self.options.timeout_seconds) as response:
                code=response.getcode()
                if code in (401,403):raise KeepConfiguration()
                if code>=500 or code==429:raise KeepUnavailable()
                if code not in (200,201,202):raise KeepReview()
                raw=response.read(4194305)
                if len(raw)>4194304:raise KeepReview()
                try:
                    result=_json(raw)
                    is_list=path in ('series','series/lookup','rootfolder','qualityprofile','episode','manualimport') and (body is None or path=='manualimport') or path=='command' and body is None or path=='release/push'
                    if is_list:
                        if type(result) is not list or len(result)>10000 or any(type(row) is not dict for row in result):raise KeepReview()
                    elif type(result) is not dict:raise KeepReview()
                    return result
                except ValueError:raise KeepReview() from None
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close()
            if code in (401,403):raise KeepConfiguration() from None
            if code>=500 or code==429:raise KeepUnavailable() from None
            raise KeepReview() from None
        except (OSError,TimeoutError,urllib.error.URLError,http.client.HTTPException):raise KeepUnavailable() from None

    def series(self,tvdb):
        rows=self.request('series',query={'tvdbId':tvdb})
        if type(rows) is not list:raise KeepReview()
        rows=[r for r in rows if type(r) is dict and r.get('tvdbId')==tvdb]
        if len(rows)>1:raise KeepReview()
        return rows[0] if rows else None

    def add_body(self,tvdb,monitor):
        """The POST series body: Sonarr's own lookup record plus our settings.

        Checks the lookup, root folder and quality profile first; sends nothing.
        """
        from src.anime.checks import here
        rows=[r for r in self.request('series/lookup',query={'term':f'tvdb:{tvdb}'}) if r.get('tvdbId')==tvdb]
        if len(rows)!=1:raise KeepReview(failed_check=here('SERIES_LOOKUP_FAILED'))
        roots={r.get('path','').rstrip('/') for r in self.request('rootfolder')}
        if self.options.library_root.rstrip('/') not in roots:
            raise KeepReview(failed_check=here('ROOT_FOLDER_NOT_IN_SONARR'),failed_path=self.options.library_root)
        profile=self.options.quality_profile_id
        if profile<=0 or profile not in {r.get('id') for r in self.request('qualityprofile')}:
            raise KeepConfiguration('Sonarr is not set up: '+PROFILE_MISSING+'.')
        body={k:v for k,v in rows[0].items() if k not in ('id','path','folder','rootFolderPath','addOptions')}
        # Search waits until every staged import is done (keep_service.request_search),
        # or Sonarr would download the staged episodes again.
        body.update(qualityProfileId=profile,rootFolderPath=self.options.library_root,
                    seriesType='anime',seasonFolder=True,monitored=True,
                    addOptions={'monitor':monitor,'searchForMissingEpisodes':False})
        return body

    def search(self,series_id):
        """Ask Sonarr once to search the series' missing episodes, and only those; None when none are.

        Owner, 2026-11-28: a SeriesSearch also searches every episode below the profile's cutoff,
        so Sonarr replaced each episode AKON had just imported (HDTV-1080p, under Anime's
        Bluray-1080p cutoff) minutes after the Keep."""
        ids=sorted(row['id'] for row in self._missing(series_id))
        if not ids:return None
        reply=self.request('command',body={'name':'EpisodeSearch','episodeIds':ids})
        if type(reply.get('id')) is not int or reply['id']<=0:raise KeepReview()
        return reply['id']

    def _missing(self,series_id):
        import datetime
        now=datetime.datetime.now(datetime.timezone.utc).isoformat()
        queued=self.request('queue',query={'seriesIds':series_id,'pageSize':1000})
        busy={record.get('episodeId') for record in queued.get('records') or ()}
        return [row for row in self.request('episode',query={'seriesId':series_id})
                if row.get('monitored') and not row.get('hasFile') and row['seasonNumber']>0
                and (row.get('airDateUtc') or '9') <= now and row.get('id') not in busy]

    def missing_episodes(self,series_id):
        """(season, episode) Sonarr wants: monitored, aired, no file, not already downloading."""
        return {(row['seasonNumber'],row['episodeNumber']) for row in self._missing(series_id)}

    def push_release(self,title,magnet,info_hash,indexer,size,seeders):
        """Give Sonarr a release it would not find itself. Returns Sonarr's rejections ([] = grabbed)."""
        import datetime
        rows=self.request('release/push',body={
            'title':title,'magnetUrl':magnet,'infoHash':info_hash,'protocol':'torrent','indexer':indexer,
            'size':size,'seeders':seeders,
            'publishDate':datetime.datetime.now(datetime.timezone.utc).isoformat()})
        if not rows:raise KeepReview()
        return [] if rows[0].get('approved') else [str(x) for x in rows[0].get('rejections') or ('not approved',)]

    def episode(self,selection,series):
        rows=self.request('episode',query={'seriesId':series['id']})
        _,eid=selection.match_sonarr_episode(series,rows)
        return next(row for row in rows if row.get('id')==eid)

    def import_payload(self,selection,series,episode):
        source=remote_source(self.options,selection)
        rows=self.request('manualimport',query={'folder':str(PurePosixPath(source).parent),'filterExistingFiles':'false'})
        candidates=[r for r in rows if r.get('path')==source]
        if len(candidates)!=1:raise KeepReview()
        preview=candidates[0]
        # Reprocess this exact file with explicit verified IDs rather than title matching.
        values={'path':source,'seriesId':series['id'],'seasonNumber':selection.tvdb_season,'episodeIds':[episode['id']],
                'quality':preview.get('quality'),'languages':preview.get('languages',[]),
                'releaseGroup':preview.get('releaseGroup',''),'indexerFlags':preview.get('indexerFlags',0)}
        result=self.request('manualimport',body=[values])
        if type(result) is not list or len(result)!=1:raise KeepReview()
        item=result[0]
        if (item.get('path')!=source or item.get('seriesId')!=series['id'] or item.get('rejections')
                or [r.get('id') for r in item.get('episodes',[])]!=[episode['id']]
                or not item.get('quality') or not item.get('languages')):raise KeepReview()
        values['quality']=item['quality'];values['languages']=item['languages']
        return {'name':'ManualImport','importMode':'copy','files':[values]}
