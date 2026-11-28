"""adm running: what is being worked on right now (the Running page)."""
NAME = 'running'
SUMMARY = 'What is being worked on right now'


def main(argv, prog):
    import argparse
    import json
    from src.anime.configuration import ConfigurationStore, cli_config_dir
    from src.anime.series_job.views import running
    from src.anime.storage.connection import database
    parser = argparse.ArgumentParser(prog=prog, description=SUMMARY)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    with database(ConfigurationStore(args.config_dir).read().value('storage.database_path')) as db:
        now = running(db)
    if args.json:
        print(json.dumps(now, indent=2, sort_keys=True))
        return 0
    if not now['series']:
        print('Nothing is being worked on.')
    for s in now['series']:
        print(f"Series {s['anidb_id']} {s['title']}"
              + (f" · Keep at {s['keep']['step']}" if s['keep'] else '') + (' · rejecting' if s['rejecting'] else ''))
        for t in s['torrents']:
            episodes = f" [{', '.join(t['episodes'])}]" if t['episodes'] else ''
            print(f"  {t['release_name']}{episodes}: {t['step'].replace('_', ' ')}"
                  + ('' if t['progress'] is None else f", {int(t['progress'] * 100)}%"))
    if now['waiting_identity']:
        print(f"Identifying with AniDB: {len(now['waiting_identity'])} release(s)")
    return 0
