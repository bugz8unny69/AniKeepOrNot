"""Shared status-endpoint check for optional HTTP services that use an API key."""
import http.client
import json
import urllib.error
import urllib.request

from src.anime.acquisition.settings import DownloadConfigurationError, endpoint
from src.anime.services.base import AUTH_FAILED, DISABLED, DOWN, NOT_CONFIGURED, UNSUPPORTED, UP, ServiceAdapter


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


class KeyedServiceAdapter(ServiceAdapter):
    """An optional service with an address setting, an API key and a status endpoint.

    A blank address turns the service off (DISABLED), which never blocks work;
    off_detail says what that turns off.
    """
    optional = True
    status_path = ''
    key_getter = ''
    key_header = ''
    version_keys = ()
    supported_major = None   # e.g. '4' for Sonarr v4; None accepts any version.
    off_detail = ''
    response_limit = 65536  # Per-adapter bounded body; shared default remains 64 KiB.

    def probe(self, configuration, secrets, *, now_ms=0, transport=None):
        if not configuration.value(f'{self.name}.base_url'):
            return self.result(DISABLED, self.off_detail, now_ms=now_ms)
        url = base_url(configuration, f'{self.name}.base_url')
        if url is None:
            return self.result(NOT_CONFIGURED, f'the {self.label} address is not valid', now_ms=now_ms)
        key, problem = self.secret(secrets, self.key_getter,
                                   f'set the {self.label} API key in Settings', now_ms=now_ms)
        if problem:
            return problem
        timeout = configuration.value(f'{self.name}.timeout_seconds')
        status, detail, data = get_status(url + self.status_path, {self.key_header: key}, timeout,
                                          transport=transport, limit=self.response_limit)
        if status == AUTH_FAILED:
            return self.result(AUTH_FAILED, f'{detail}; check the {self.label} API key', now_ms=now_ms)
        if status != UP:
            return self.result(status, f'{detail} at {url}', now_ms=now_ms)
        version = version_of(data, *self.version_keys)
        if self.supported_major and not version.startswith(self.supported_major + '.'):
            return self.result(UNSUPPORTED, f'version {version or "unknown"}; '
                               f'{self.label} v{self.supported_major} is required', now_ms=now_ms)
        return self.result(UP, f'version {version}' if version else '', now_ms=now_ms)
