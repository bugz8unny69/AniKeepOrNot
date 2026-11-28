"""adm retitle [--apply]: staged folders, files and NFOs in the chosen title language.

Owner, 2026-11-27: one folder per TVDB series, named (with its files and NFOs) in the
language set in Settings, so a series can be told apart without Jellyfin or AKON. The
cutover does this once; run it again after changing the language. Preview by default.
The worker must be stopped: it holds the series it works on.
"""
NAME = 'retitle'
SUMMARY = 'Retitle staged folders, files and NFOs in the chosen title language (preview; --apply to do it)'
WORKER = 'akon-worker.service'


def main(argv, prog):
    import argparse
    import subprocess
    import sys
    from src.anime.configuration import ConfigurationStore, cli_config_dir
    from src.anime.series_job import retitle
    from src.anime.series_job.store import get_series, update_series
    from src.anime.storage.connection import database
    from src.anime.storage_mounts import STAGING, StorageNotMounted, require
    parser = argparse.ArgumentParser(prog=prog, description=SUMMARY)
    parser.add_argument('--apply', action='store_true', help='Rename and rewrite (the default only shows what would change)')
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    configuration = ConfigurationStore(args.config_dir).read()
    language = configuration.value('anime.title_language')
    try:
        require(configuration, STAGING)
    except StorageNotMounted as exc:
        print(f'Refused: {exc}', file=sys.stderr)
        return 1
    if args.apply and subprocess.run(['systemctl', 'is-active', '--quiet', WORKER], check=False).returncode == 0:
        print(f'Refused: stop the worker first (systemctl stop {WORKER}); it holds the series it works on.',
              file=sys.stderr)
        return 1
    with database(configuration.value('storage.database_path')) as db:
        series = [get_series(db, aid) for (aid,) in db.execute(
            "SELECT anidb_id FROM series WHERE episodes_json LIKE '%\"staged\"%' ORDER BY anidb_id")]
        renames = retitle.plan(db, series, language)
        for group in renames['groups']:
            print(f"{group['folder']}: {len(group['moves'])} staged episode(s)")
        for item in renames['skipped']:
            print(f"left as it is: {item['name']} [tvdbid-{item['tvdb']}]: {item['why']}")
        if not renames['groups']:
            print(f'Everything staged is already named in {language}.')
            return 0
        if not args.apply:
            print(f'Preview only ({language}); run again with --apply, with the worker stopped.')
            return 0
        done = retitle.apply(renames)
        try:
            retitle.rewrite(series, renames)
            for row in series:
                update_series(db, row)
            db.commit()
        except BaseException:
            db.rollback()
            retitle.undo(done)
            raise
    retitle.finish(renames)
    print(f"Retitled {sum(len(g['moves']) for g in renames['groups'])} staged episode(s) in {language}. "
          'Rescan the library in Jellyfin.')
    return 0
