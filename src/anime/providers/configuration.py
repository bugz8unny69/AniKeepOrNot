"""Required provider configuration and worker composition; no HTTP at startup."""
from functools import partial

from src.anime.configuration import ConfigurationStore
from src.anime.configuration_schema import ConfigurationError
from src.anime.providers.errors import ProviderConfigurationError
from src.anime.providers.http import Options


def options_from_configuration(configuration):
    try:
        return Options(configuration.value("anidb.client_name"),
                       configuration.value("anidb.client_version"),
                       configuration.value("providers.timeout_seconds"))
    except ConfigurationError:
        raise ProviderConfigurationError() from None


def series_credentials(secrets):
    """Retrieve required TVDB credentials for anime series identification."""
    return secrets.tvdb_credentials()


def episode_placement_credentials(secrets):
    """Retrieve credentials for optional TVDB episode placement; None if unconfigured."""
    try:
        return secrets.tvdb_credentials()
    except ProviderConfigurationError:
        return None


def validate_provider_configuration(configuration, *, secrets=None, require_series_credentials=False):
    options_from_configuration(configuration)
    if require_series_credentials and secrets is not None:
        series_credentials(secrets)
