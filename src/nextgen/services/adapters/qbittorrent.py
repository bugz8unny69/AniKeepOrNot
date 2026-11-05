"""qBittorrent: log in and confirm a supported version and metadata method."""
from src.nextgen.acquisition.settings import DownloadConfigurationError, download_options
from src.nextgen.acquisition.submission import (
    ClientAuthenticationFailed, ClientInvalid, ClientUnavailable, ClientUnsupported,
)
from src.nextgen.configuration_schema import ConfigurationError
from src.nextgen.services.base import AUTH_FAILED, DOWN, NOT_CONFIGURED, UNSUPPORTED, UP, ServiceAdapter


class QbittorrentAdapter(ServiceAdapter):
    name = 'qbittorrent'
    label = 'qBittorrent'

    def __init__(self, *, client_factory=None):
        self.client_factory = client_factory

    def _factory(self):
        if self.client_factory is not None:
            return self.client_factory
        from src.nextgen.acquisition.qbittorrent import Client
        return Client

    def probe(self, configuration, secrets, *, now_ms=0):
        missing = 'set the qBittorrent address, download folder, username and password in Settings'
        try:
            options = download_options(configuration)
        except (DownloadConfigurationError, ConfigurationError, ValueError):
            return self.result(NOT_CONFIGURED, missing, now_ms=now_ms)
        credentials, problem = self.secret(secrets, 'qbittorrent_credentials', missing, now_ms=now_ms)
        if problem:
            return problem
        client = self._factory()(options, credentials)
        try:
            client.login()
            strategy = client.metadata_strategy
        except Exception as exc:
            status = self.classify(exc) or DOWN
            return self.result(status, _DETAIL.get(status, '') + f' at {options.base_url}', now_ms=now_ms)
        if strategy not in ('fetch', 'guarded_export'):
            return self.result(UNSUPPORTED, 'qBittorrent 4.5 or newer (Web API 2.8.14+) is required',
                               now_ms=now_ms)
        version = getattr(client, 'major', None)
        api = getattr(client, 'api_version', None)
        api_text = '.'.join(map(str, api)) if type(api) is tuple else ''
        detail = f'version {version}, Web API {api_text}' if version and api_text else ''
        return self.result(UP, detail, now_ms=now_ms)

    def classify(self, exc):
        if isinstance(exc, ClientAuthenticationFailed):
            return AUTH_FAILED
        if isinstance(exc, ClientUnsupported):
            return UNSUPPORTED
        if isinstance(exc, (DownloadConfigurationError, ConfigurationError)):
            return NOT_CONFIGURED
        if isinstance(exc, (ClientUnavailable, ClientInvalid)):
            return DOWN
        return None


_DETAIL = {
    AUTH_FAILED: 'login rejected; check the qBittorrent username and password',
    UNSUPPORTED: 'qBittorrent 4.5 or newer (Web API 2.8.14+) is required',
    DOWN: 'could not connect',
}
