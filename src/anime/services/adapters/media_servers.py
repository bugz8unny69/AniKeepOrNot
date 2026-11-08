"""Optional media-server services: Jellyfin and Shoko. A blank address means turned off."""
from src.anime.services.adapters._http import KeyedServiceAdapter
from src.anime.services.base import DOWN, NOT_CONFIGURED


class JellyfinAdapter(KeyedServiceAdapter):
    name = 'jellyfin'
    label = 'Jellyfin'
    status_path = '/System/Info'          # Requires the API key.
    key_getter = 'jellyfin_api_key'
    key_header = 'X-Emby-Token'
    version_keys = ('Version',)
    reasons = {}  # SERVICE_* codes naming 'jellyfin' are handled by the base class.

    def classify(self, exc):
        from src.anime.jellyfin import RefreshConfiguration, RefreshUnavailable
        if isinstance(exc, RefreshConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, RefreshUnavailable):
            return DOWN
        return None


class ShokoAdapter(KeyedServiceAdapter):
    name = 'shoko'
    label = 'Shoko'
    status_path = '/api/v3/Init/Version'
    key_getter = 'shoko_api_key'
    key_header = 'apikey'
    version_keys = ('Version', 'version')
    reasons = {}  # SERVICE_* codes naming 'shoko' are handled by the base class.

    def classify(self, exc):
        from src.anime.shoko import ShokoConfiguration, ShokoUnavailable
        if isinstance(exc, ShokoConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, ShokoUnavailable):
            return DOWN
        return None
