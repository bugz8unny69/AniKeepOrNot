"""Shared status-endpoint check for HTTP services that require an API key."""
import http.client
import json
import urllib.error
import urllib.request

from src.nextgen.acquisition.settings import DownloadConfigurationError, endpoint
from src.nextgen.services.base import AUTH_FAILED, DOWN, UP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _default_transport():
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    return lambda request, timeout: opener.open(request, timeout=timeout)


def base_url(configuration, key):
    """Validated base URL, or None when the setting is blank or invalid."""
    value = configuration.value(key)
    if not value:
        return None
    try:
        return endpoint(value).rstrip('/')
    except (DownloadConfigurationError, ValueError):
        return None


def get_status(url, headers, timeout, *, transport=None, limit=65536):
    """GET a status endpoint. Returns (status, detail, json_or_None). Never raises."""
    send = transport or _default_transport()
    request = urllib.request.Request(url, method='GET', headers={**headers, 'Accept': 'application/json'})
    try:
        with send(request, timeout) as response:
            code = response.getcode()
            raw = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (401, 403):
            return AUTH_FAILED, f'HTTP {code}', None
        return DOWN, f'HTTP {code}', None
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        reason = getattr(exc, 'reason', None) or exc
        return DOWN, f'could not connect ({type(reason).__name__})', None
    if code in (401, 403):
        return AUTH_FAILED, f'HTTP {code}', None
    if code != 200:
        return DOWN, f'HTTP {code}', None
    if len(raw) > limit:
        return DOWN, 'status response too large', None
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return DOWN, 'status response was not JSON', None
    return UP, '', data if type(data) is dict else None


def version_of(data, *keys):
    for key in keys:
        value = data.get(key) if data else None
        if type(value) is str and value and len(value) <= 64 and value.isprintable():
            return value
    return ''
