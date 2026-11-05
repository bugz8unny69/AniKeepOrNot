"""Prowlarr and Sonarr: status endpoint protected by the API key."""
from src.nextgen.services.adapters._http import base_url, get_status, version_of
from src.nextgen.services.base import AUTH_FAILED, DOWN, NOT_CONFIGURED, UNSUPPORTED, UP, ServiceAdapter


class _ArrAdapter(ServiceAdapter):
    status_path = ''
    key_getter = ''
    supported_major = None   # e.g. '4' for Sonarr v4; None accepts any version.

    def probe(self, configuration, secrets, *, now_ms=0, transport=None):
        url = base_url(configuration, f'{self.name}.base_url')
        if url is None:
            return self.result(NOT_CONFIGURED, f'set the {self.label} address in Settings', now_ms=now_ms)
        key, problem = self.secret(secrets, self.key_getter,
                                   f'set the {self.label} API key in Settings', now_ms=now_ms)
        if problem:
            return problem
        timeout = configuration.value(f'{self.name}.timeout_seconds')
        status, detail, data = get_status(url + self.status_path, {'X-Api-Key': key}, timeout,
                                          transport=transport)
        if status == AUTH_FAILED:
            return self.result(AUTH_FAILED, f'{detail}; check the {self.label} API key', now_ms=now_ms)
        if status != UP:
            return self.result(status, f'{detail} at {url}', now_ms=now_ms)
        version = version_of(data, 'version')
        if self.supported_major and not version.startswith(self.supported_major + '.'):
            return self.result(UNSUPPORTED, f'version {version or "unknown"}; '
                               f'{self.label} v{self.supported_major} is required', now_ms=now_ms)
        return self.result(UP, f'version {version}' if version else '', now_ms=now_ms)


class ProwlarrAdapter(_ArrAdapter):
    name = 'prowlarr'
    label = 'Prowlarr'
    status_path = '/api/v1/system/status'
    key_getter = 'prowlarr_api_key'

    def classify(self, exc):
        from src.nextgen.acquisition.prowlarr_search import (
            SearchConfiguration, SearchReview, SearchUnauthorized, SearchUnavailable,
        )
        if isinstance(exc, SearchUnauthorized):
            return AUTH_FAILED
        if isinstance(exc, SearchConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, (SearchUnavailable, SearchReview)):
            return DOWN
        return None


class SonarrAdapter(_ArrAdapter):
    name = 'sonarr'
    label = 'Sonarr'
    status_path = '/api/v3/system/status'
    key_getter = 'sonarr_api_key'
    supported_major = '4'

    def classify(self, exc):
        from src.nextgen.sonarr import KeepConfiguration, KeepUnavailable
        if isinstance(exc, KeepConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, KeepUnavailable):
            return DOWN
        return None
