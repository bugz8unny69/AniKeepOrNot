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


def run_action(argv, prog, summary, action, **extra_args):
    """Parse --id (and extra_args), run a series action, print its message. 0, or 1 when refused."""
    import argparse
    import sys
    from src.anime.configuration import ConfigurationStore, cli_config_dir
    from src.anime.series_job.actions import ReleaseInvalid, SeriesDecided, SeriesNotFound
    from src.anime.storage.connection import database
    parser = argparse.ArgumentParser(prog=prog, description=summary)
    parser.add_argument('--id', dest='anidb_id', type=int, required=True, help='AniDB ID, e.g. 16999')
    for name, help_text in extra_args.items():
        parser.add_argument('--' + name, default='', help=help_text)
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    with database(ConfigurationStore(args.config_dir).read().value('storage.database_path')) as db:
        try:
            result = action(db, args.anidb_id, **{name: getattr(args, name) for name in extra_args})
        except (SeriesNotFound, SeriesDecided, ReleaseInvalid) as exc:
            print(f'Refused: {exc}', file=sys.stderr)
            return 1
        db.commit()
    print(result['message'])
    return 0
