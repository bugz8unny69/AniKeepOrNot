"""adm keep: keep a series in Sonarr by AniDB ID."""
import argparse
import json
import sqlite3
import sys

from src.anime.configuration import cli_config_dir

NAME = 'keep'
SUMMARY = 'Keep a series in Sonarr (--id ANIDB_ID)'


def connect(config_dir):
    from src.anime.configuration import ConfigurationStore
    try:
        db_path = ConfigurationStore(config_dir).read().value('storage.database_path')
    except Exception:
        db_path = '/var/lib/anime-discovery/anime-discovery-develop.sqlite'
    conn = sqlite3.connect(db_path, timeout=10)
    return conn


def main(argv, prog):
    parser = argparse.ArgumentParser(prog=prog, description=SUMMARY)
    parser.add_argument('--id', dest='anidb_id', type=int, required=True, help='AniDB ID, e.g. 16999')
    parser.add_argument('--config-dir', default=cli_config_dir(), help='Configuration directory')
    args = parser.parse_args(argv)

    from src.anime.series_job.actions import keep_series_sync
    try:
        db = connect(args.config_dir)
    except Exception as exc:
        print(f"Cannot open the database ({exc.__class__.__name__}: {exc}).", file=sys.stderr)
        return 1

    try:
        with db:
            result = keep_series_sync(db, args.anidb_id)
            print(result['message'])
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
