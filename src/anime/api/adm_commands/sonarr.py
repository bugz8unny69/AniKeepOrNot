"""adm sonarr find-dubs --id: AKON finds dubs for Sonarr's missing episodes of a kept series, now."""
NAME = 'sonarr'
SUMMARY = "Find dubs for a kept series' missing episodes and push them to Sonarr (Keep does this too)"


def main(argv, prog):
    import argparse
    from src.anime.api.adm_commands._series import open_series
    from src.anime.configuration import cli_config_dir
    from src.anime.series_job.keep import find_dubs_now
    parser = argparse.ArgumentParser(prog=prog, description=SUMMARY)
    parser.add_argument('find_dubs', choices=('find-dubs',))
    parser.add_argument('--id', dest='anidb_id', type=int, required=True, help='AniDB ID, e.g. 18162')
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    configuration, secrets, series = open_series(args.config_dir, args.anidb_id)
    tvdb = next((t['identity']['anime']['tvdb_series_id'] for t in series.torrents if t.get('identity')), None) \
        if series is not None else None
    if tvdb is None:
        print(f'AniDB {args.anidb_id} has no series with a TVDB ID.')
        return 1
    grabbed, missing = find_dubs_now(series, tvdb, configuration, secrets)
    print(f'Sonarr grabbed {len(grabbed)}: {", ".join(grabbed) or "none"}; nothing usable for {", ".join(missing) or "none"}')
    return 0
