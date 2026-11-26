"""adm report intake --id <anidb_id>: one series, its torrents, its episodes and its history."""
NAME = 'report'
SUMMARY = 'Read-only report of a series (intake --id ANIDB_ID)'


def main(argv, prog):
    import argparse
    import json
    from src.anime.configuration import ConfigurationStore, cli_config_dir
    from src.anime.series_job.views import series_view
    from src.anime.storage.connection import database
    parser = argparse.ArgumentParser(prog=prog, description=SUMMARY)
    parser.add_argument('what', choices=('intake',))
    parser.add_argument('--id', dest='anidb_id', type=int, required=True, help='AniDB ID, e.g. 18162')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    configuration = ConfigurationStore(args.config_dir).read()
    with database(configuration.value('storage.database_path')) as db:
        item = series_view(db, args.anidb_id, configuration.value('anime.title_language'))
    if item is None:
        print(f'AniDB {args.anidb_id} has no series.')
        return 1
    if args.json:
        print(json.dumps(item, indent=2, sort_keys=True))
        return 0
    print(f"Series {item['anidb_id']} {item['title']}: {item['state']}")
    if item['keep']:
        keep = item['keep']
        print(f"  Keep: at {keep['step']}" + (f"; stopped: {keep['stopped']}" if keep['stopped'] else ''))
    if item['search']['wanted']:
        print(f"  Trial episodes wanted (AniDB): {', '.join(map(str, item['search']['wanted']))}")
    print('Episodes:')
    for code, data in sorted(item['episodes'].items()):
        print(f"  {code}  {data.get('state'):<12} {data.get('file') or ''}")
    print('Torrents:')
    for t in item['torrents']:
        why = f" ({t['why']})" if t['why'] else ''
        print(f"  {t['release_name']}: {t['step']}, {t['state']}{why} "
              f"[{', '.join(t['episodes'])}] {int((t['progress'] or 0) * 100)}%")
    print('Recent:')
    for line in item['recent']:
        print('  ' + line)
    return 0
