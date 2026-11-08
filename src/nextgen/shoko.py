"""Optional Shoko v5/API v3 AniDB metadata; no file or mapping writes."""
import http.client
import json
import urllib.error
import urllib.request

from src.nextgen.acquisition.settings import endpoint
from src.nextgen.torrent_client.qbittorrent import _NoRedirect
from src.nextgen.history import integer


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
