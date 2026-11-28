"""Shared by the series adm commands: settings, secrets and one series row."""


def open_series(config_dir, anidb_id):
    """(configuration, secrets, series or None) for adm; the series is read without its lease."""
    from src.anime.configuration import ConfigurationStore
    from src.anime.secrets import SecretStore
    from src.anime.series_job.store import get_series
    from src.anime.storage.connection import database
    configuration = ConfigurationStore(config_dir).read()
    with database(configuration.value('storage.database_path')) as db:
        series = get_series(db, anidb_id)
    return configuration, SecretStore(config_dir), series
