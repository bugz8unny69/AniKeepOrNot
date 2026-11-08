"""Request a Jellyfin library scan; acceptance does not prove item visibility."""
import http.client
import urllib.error
import urllib.request

from src.anime.acquisition.settings import endpoint
from src.anime.torrent_client.qbittorrent import _NoRedirect


class RefreshConfiguration(ValueError):
    reason_code = 'SERVICE_SETUP_INVALID'
    service = 'jellyfin'
    service_status = 'NOT_CONFIGURED'
    def __init__(self):super().__init__('Configure Jellyfin address and API key.')


class RefreshUnavailable(RuntimeError):
    reason_code = 'SERVICE_UNAVAILABLE'
    service = 'jellyfin'
    def __init__(self):super().__init__('Jellyfin refresh is temporarily unavailable.')


class RefreshReview(ValueError):
    reason_code='MEDIA_SERVER_REFRESH_REVIEW'
    def __init__(self):super().__init__('Jellyfin refresh needs review.')


def request_refresh(configuration,secrets,*,transport=None):
    try:
        address=endpoint(configuration.value('jellyfin.base_url'))
        timeout=configuration.value('jellyfin.timeout_seconds')
        key=secrets.jellyfin_api_key()
    except (ValueError,AttributeError):raise RefreshConfiguration() from None
    if type(key) is not str or not key or any(ord(c)<33 or ord(c)>126 for c in key):raise RefreshConfiguration()
    opener=urllib.request.build_opener(_NoRedirect(),urllib.request.ProxyHandler({}))
    send=transport or (lambda request,timeout:opener.open(request,timeout=timeout))
    request=urllib.request.Request(address+'/Library/Refresh',data=b'',method='POST',
        headers={'X-Emby-Token':key,'Accept':'application/json'})
    def check(code):
        if code in (401,403):raise RefreshConfiguration()
        if code==429 or code>=500:raise RefreshUnavailable()
        if code!=204:raise RefreshReview()
    try:
        with send(request,timeout) as response:check(response.getcode())
    except urllib.error.HTTPError as exc:
        code=exc.code;exc.close();check(code)
    except (OSError,TimeoutError,urllib.error.URLError,http.client.HTTPException):
        raise RefreshUnavailable() from None
