"""Optional media-server services: Jellyfin and Shoko. A blank address means turned off."""
from src.nextgen.services.adapters._http import base_url, get_status, version_of
from src.nextgen.services.base import AUTH_FAILED, DISABLED, DOWN, NOT_CONFIGURED, UP, ServiceAdapter


class _OptionalAdapter(ServiceAdapter):
    optional = True
    status_path = ''
    key_getter = ''
    key_header = ''
    version_keys = ()

    def probe(self, configuration, secrets, *, now_ms=0, transport=None):
        if not configuration.value(f'{self.name}.base_url'):
            return self.result(DISABLED, now_ms=now_ms)
        url = base_url(configuration, f'{self.name}.base_url')
        if url is None:
            return self.result(NOT_CONFIGURED, f'the {self.label} address is not valid', now_ms=now_ms)
        key, problem = self.secret(secrets, self.key_getter,
                                   f'set the {self.label} API key in Settings', now_ms=now_ms)
        if problem:
            return problem
        timeout = configuration.value(f'{self.name}.timeout_seconds')
        status, detail, data = get_status(url + self.status_path, {self.key_header: key}, timeout,
                                          transport=transport)
        if status == AUTH_FAILED:
            return self.result(AUTH_FAILED, f'{detail}; check the {self.label} API key', now_ms=now_ms)
        if status != UP:
            return self.result(status, f'{detail} at {url}', now_ms=now_ms)
        version = version_of(data, *self.version_keys)
        return self.result(UP, f'version {version}' if version else '', now_ms=now_ms)


class JellyfinAdapter(_OptionalAdapter):
    name = 'jellyfin'
    label = 'Jellyfin'
    status_path = '/System/Info'          # Requires the API key.
    key_getter = 'jellyfin_api_key'
    key_header = 'X-Emby-Token'
    version_keys = ('Version',)

    def classify(self, exc):
        from src.nextgen.jellyfin import RefreshConfiguration, RefreshUnavailable
        if isinstance(exc, RefreshConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, RefreshUnavailable):
            return DOWN
        return None


class ShokoAdapter(_OptionalAdapter):
    name = 'shoko'
    label = 'Shoko'
    status_path = '/api/v3/Init/Version'
    key_getter = 'shoko_api_key'
    key_header = 'apikey'
    version_keys = ('Version', 'version')

    def classify(self, exc):
        from src.nextgen.shoko import ShokoConfiguration, ShokoUnavailable
        if isinstance(exc, ShokoConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, ShokoUnavailable):
            return DOWN
        return None
