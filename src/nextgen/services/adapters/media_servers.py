"""Optional media-server services: Jellyfin and Shoko. A blank address means turned off."""
from src.nextgen.services.adapters._http import KeyedServiceAdapter
from src.nextgen.services.base import DOWN, NOT_CONFIGURED


class JellyfinAdapter(KeyedServiceAdapter):
    name = 'jellyfin'
    label = 'Jellyfin'
    status_path = '/System/Info'          # Requires the API key.
    key_getter = 'jellyfin_api_key'
    key_header = 'X-Emby-Token'
    version_keys = ('Version',)
    reasons = {'JELLYFIN_UNAVAILABLE': DOWN, 'JELLYFIN_CONFIGURATION_INVALID': NOT_CONFIGURED}

    def classify(self, exc):
        from src.nextgen.jellyfin import RefreshConfiguration, RefreshUnavailable
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
    reasons = {'SHOKO_UNAVAILABLE': DOWN, 'SHOKO_CONFIGURATION_INVALID': NOT_CONFIGURED}

    def classify(self, exc):
        from src.nextgen.shoko import ShokoConfiguration, ShokoUnavailable
        if isinstance(exc, ShokoConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, ShokoUnavailable):
            return DOWN
        return None
