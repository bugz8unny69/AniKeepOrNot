"""adm shoko link --id: tell Shoko which AniDB episode each kept file of a series is, now."""
NAME = 'shoko'
SUMMARY = "Link a series' kept files to their episodes in Shoko, then rescan Jellyfin (Keep does this too)"


def main(argv, prog):
    import argparse
    from src.anime.api.adm_commands._series import open_series
    from src.anime.configuration import cli_config_dir
    from src.anime.series_job.shoko_link import link_files
    from src.anime.series_job.keep import kept_files
    from src.anime.shoko import Shoko
    parser = argparse.ArgumentParser(prog=prog, description=SUMMARY)
    parser.add_argument('link', choices=('link',))
    parser.add_argument('--id', dest='anidb_id', type=int, required=True, help='AniDB ID, e.g. 18162')
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    configuration, secrets, series = open_series(args.config_dir, args.anidb_id)
    files = kept_files(series) if series is not None and 'keep' in series.work else []
    if not files:
        print(f'AniDB {args.anidb_id} has no kept files; nothing to link.')
        return 1
    linked, no_file, no_episode = link_files(Shoko(configuration, secrets), args.anidb_id, files)
    print(f'Shoko: {linked} file(s) newly linked of {len(files)}.')
    for line in no_file:
        print(f'  Shoko has not recorded {line} yet (it was asked to import new files).')
    for line in no_episode:
        print(f'  Shoko has not fetched the episode of {line} yet.')
    if configuration.value('jellyfin.base_url'):
        from src.anime.jellyfin import request_refresh
        request_refresh(configuration, secrets)
        print('Jellyfin asked to rescan.')
    return 0 if not no_file and not no_episode else 2
