"""Shoko v5/API v3: tell Shoko which AniDB episode each kept file is, and read its titles."""
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from src.anime.acquisition.settings import endpoint
from src.anime.torrent_client.qbittorrent import _NoRedirect
from src.anime.history import integer


class ShokoConfiguration(ValueError):
    def __init__(self):super().__init__('Configure the optional Shoko address and API key.')


class ShokoUnavailable(RuntimeError):
    def __init__(self):super().__init__('Optional Shoko metadata is temporarily unavailable.')


class ShokoReview(ValueError):
    def __init__(self):super().__init__('Optional Shoko metadata needs review.')


def _title(value):
    if type(value) is not str or not value.strip() or len(value)>500 or any(ord(c)<32 or ord(c)==127 for c in value):raise ShokoReview()
    return value


def lookup(configuration,secrets,anidb_id,episode_id,*,transport=None):
    integer(anidb_id,positive=True);integer(episode_id,positive=True)
    try:
        address=endpoint(configuration.value('shoko.base_url'))
        timeout=configuration.value('shoko.timeout_seconds');key=secrets.shoko_api_key()
    except (ValueError,AttributeError):raise ShokoConfiguration() from None
    if type(key) is not str or not key or any(ord(c)<33 or ord(c)>126 for c in key):raise ShokoConfiguration()
    opener=urllib.request.build_opener(_NoRedirect(),urllib.request.ProxyHandler({}))
    send=transport or (lambda request,timeout:opener.open(request,timeout=timeout))
    def get(kind,identity):
        request=urllib.request.Request(f'{address}/api/v3/{kind}/AniDB/{identity}',method='GET',
            headers={'apikey':key,'Accept':'application/json'})
        def check(code):
            if code in (401,403):raise ShokoConfiguration()
            if code==429 or code>=500:raise ShokoUnavailable()
            if code!=200:raise ShokoReview()
        def pairs(items):
            result={}
            for name,value in items:
                if name in result:raise ShokoReview()
                result[name]=value
            return result
        try:
            with send(request,timeout) as response:
                check(response.getcode());raw=response.read(1048577)
            if len(raw)>1048576:raise ShokoReview()
            value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,
                parse_constant=lambda _:(_ for _ in ()).throw(ShokoReview()))
            if type(value) is not dict or type(value.get('ID')) is not int or value['ID']!=identity:raise ShokoReview()
            return _title(value.get('Title'))
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close();check(code)
        except (OSError,TimeoutError,urllib.error.URLError,http.client.HTTPException):raise ShokoUnavailable() from None
        except (ShokoConfiguration,ShokoReview):raise
        except (UnicodeError,ValueError,RecursionError):raise ShokoReview() from None
    return {'anidb_id':anidb_id,'anidb_episode_id':episode_id,'title':get('Series',anidb_id),'episode_title':get('Episode',episode_id)}


class Shoko:
    """Shoko v5.3 API v3 calls Keep needs to make Shoko see kept files (owner, 2026-11-23).

    Shoko's own hash match often fails, and Jellyfin (through Shokofin) only
    shows files Shoko has linked to an episode. So after every Keep, AKON
    tells Shoko which AniDB episode each kept file is.
    """
    def __init__(self, configuration, secrets, *, transport=None):
        try:
            self.address = endpoint(configuration.value('shoko.base_url')) + '/api/v3/'
            self.timeout = configuration.value('shoko.timeout_seconds')
            self.key = secrets.shoko_api_key()
        except (ValueError, AttributeError):
            raise ShokoConfiguration() from None
        if type(self.key) is not str or not self.key:
            raise ShokoConfiguration()
        opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
        self.send = transport or (lambda request, timeout: opener.open(request, timeout=timeout))

    def call(self, method, path, body=None, *, missing_ok=False):
        """JSON from Shoko; None for a 404 when missing_ok. Other errors raise."""
        request = urllib.request.Request(
            self.address + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'apikey': self.key, 'Accept': 'application/json',
                     **({} if body is None else {'Content-Type': 'application/json'})})
        try:
            with self.send(request, self.timeout) as response:
                raw = response.read(4194305)
        except urllib.error.HTTPError as exc:
            code, detail = exc.code, exc.read(500).decode('utf-8', 'replace')
            exc.close()
            if code == 404 and missing_ok:
                return None
            if code in (401, 403):
                raise ShokoConfiguration() from None
            if code == 429 or code >= 500:
                raise ShokoUnavailable() from None
            raise ShokoRefused(method, path, code, detail) from None
        except (OSError, TimeoutError, http.client.HTTPException):
            raise ShokoUnavailable() from None
        return json.loads(raw) if raw.strip() else None

    def refresh_series(self, anidb_id):
        """Ask Shoko to fetch the AniDB series and create its entry if it has none."""
        self.call('POST', f'Series/AniDB/{anidb_id}/Refresh?createSeriesEntry=true')

    def scan(self):
        """Ask Shoko to scan every import folder for new files."""
        for folder in self.call('GET', 'ImportFolder'):
            self.call('GET', f"ImportFolder/{folder['ID']}/Scan")

    def find_file(self, relative_path):
        """(Shoko file ID, AniDB episode IDs it is linked to), or None if Shoko hasn't seen the file."""
        query = urllib.parse.urlencode({'path': relative_path, 'limit': 2})
        found = self.call('GET', f'File/PathEndsWith?{query}')
        if not found:
            return None
        if len(found) > 1:
            raise ShokoRefused('GET', 'File/PathEndsWith', 200, f'{len(found)} files end with {relative_path}')
        file = found[0]
        linked = {episode.get('AniDB') for reference in file.get('SeriesIDs') or ()
                  for episode in reference.get('EpisodeIDs') or ()}
        return file['ID'], linked

    def episode_id(self, anidb_episode_id):
        """Shoko's episode ID for an AniDB episode, or None until Shoko has the series."""
        episode = self.call('GET', f'Episode/AniDB/{anidb_episode_id}/Episode', missing_ok=True)
        return None if episode is None else episode['IDs']['ID']

    def link(self, file_id, episode_id):
        self.call('POST', f'File/{file_id}/Link', {'EpisodeIDs': [episode_id]})


class ShokoRefused(ValueError):
    """Shoko answered a request with an error it gave a reason for."""
    reason_code = 'MEDIA_LINK_REFUSED'

    def __init__(self, method, path, code, detail):
        super().__init__(f'Shoko refused {method} {path} ({code}): {detail}')
