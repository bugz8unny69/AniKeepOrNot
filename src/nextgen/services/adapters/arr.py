"""Prowlarr and Sonarr: optional; a blank address turns off automatic search or Keep."""
from src.nextgen.services.adapters._http import KeyedServiceAdapter
from src.nextgen.services.base import AUTH_FAILED, DOWN, NOT_CONFIGURED


class ProwlarrAdapter(KeyedServiceAdapter):
    name = 'prowlarr'
    label = 'Prowlarr'
    status_path = '/api/v1/system/status'
    key_getter = 'prowlarr_api_key'
    key_header = 'X-Api-Key'
    version_keys = ('version',)
    off_detail = 'no address is set, so automatic search is off'
    reasons = {
        'PROWLARR_UNAVAILABLE': DOWN,
        'PROWLARR_SEARCH_TIMEOUT': DOWN,
        'PROWLARR_AUTH_FAILED': AUTH_FAILED,
        'PROWLARR_CONFIGURATION_INVALID': NOT_CONFIGURED,
    }

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


class SonarrAdapter(KeyedServiceAdapter):
    name = 'sonarr'
    label = 'Sonarr'
    status_path = '/api/v3/system/status'
    key_getter = 'sonarr_api_key'
    key_header = 'X-Api-Key'
    version_keys = ('version',)
    off_detail = 'no address is set, so Keep is off'
    supported_major = '4'
    reasons = {
        'KEEP_SERVICE_UNAVAILABLE': DOWN,
        'KEEP_CONFIGURATION_INVALID': NOT_CONFIGURED,
    }

    def classify(self, exc):
        from src.nextgen.sonarr import KeepConfiguration, KeepUnavailable
        if isinstance(exc, KeepConfiguration):
            return NOT_CONFIGURED
        if isinstance(exc, KeepUnavailable):
            return DOWN
        return None
