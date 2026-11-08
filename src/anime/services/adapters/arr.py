"""Prowlarr and Sonarr: optional; a blank address turns off automatic search or Keep."""
from src.anime.services.adapters._http import KeyedServiceAdapter
from src.anime.services.base import AUTH_FAILED, DOWN, NOT_CONFIGURED


class ProwlarrAdapter(KeyedServiceAdapter):
    name = 'prowlarr'
    label = 'Prowlarr'
    status_path = '/api/v1/system/status'
    key_getter = 'prowlarr_api_key'
    key_header = 'X-Api-Key'
    version_keys = ('version',)
    off_detail = 'no address is set, so automatic search is off'
    # SERVICE_* codes naming 'prowlarr' are handled by the base class.
    reasons = {'RELEASE_SEARCH_TIMEOUT': DOWN}

    def classify(self, exc):
        from src.anime.acquisition.prowlarr_search import (
            SearchConfiguration, SearchReview, SearchUnauthorized, SearchUnavailable,
        )
        if isinstance(exc, SearchUnauthorized):
            return AUTH_FAILED
        if isinstance(exc, SearchConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, (SearchUnavailable, SearchReview)):
            return DOWN
        return None


class SonarrAdapter(KeyedServiceAdapter):
    name = 'sonarr'
    label = 'Sonarr'
    status_path = '/api/v3/system/status'
    key_getter = 'sonarr_api_key'
    key_header = 'X-Api-Key'
    version_keys = ('version',)
    off_detail = 'no address is set, so Keep is off'
    supported_major = '4'
    # SERVICE_* codes naming 'sonarr' are handled by the base class.
    reasons = {'KEEP_CONFIGURATION_INVALID': NOT_CONFIGURED}

    def classify(self, exc):
        from src.anime.sonarr import KeepConfiguration, KeepUnavailable
        if isinstance(exc, KeepConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, KeepUnavailable):
            return DOWN
        return None
