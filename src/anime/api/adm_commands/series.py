"""adm series --id <anidb_id> [--keep | --reject | --search | --retry | --cancel] [--promote | --unpromote]

One series, by its AniDB ID. Without an action it shows the series: its state, its
torrents and why each waits or stopped, its episodes and recent history. The actions
are the dashboard's:

  --retry    stopped torrents resume, dropped ones start over, a stopped Keep resumes
  --keep     Sonarr takes the series; its staged episodes are imported
  --reject   its torrents and staged files are removed (--reason CODE, --note TEXT)
  --search   search now for the trial episodes no torrent covers
  --cancel   drop its torrents that are not done; staged episodes stay

and, alone or with an action (adm series --id 69 --retry --promote):

  --promote    work on it first: the next free worker takes it, before every other series
  --unpromote  put it back in the usual order
"""
# Owner, 2026-11-27: one command for a series, its actions as options (it replaces adm keep,
# reject, search, retry and report intake).
NAME = 'series'
SUMMARY = 'One series by AniDB ID: show it, or --retry, --keep, --reject, --search, --cancel, --promote'

ACTIONS = {
    'retry': 'stopped torrents resume, dropped ones start over, a stopped Keep resumes',
    'keep': 'Sonarr takes the series; its staged episodes are imported',
    'reject': 'remove its torrents and staged files (with --reason, --note)',
    'search': 'search now for the trial episodes no torrent covers',
    'cancel': 'drop its torrents that are not done; staged episodes stay',
}


def main(argv, prog):
    import argparse
    import json
    import sys
    from src.anime.configuration import ConfigurationStore, cli_config_dir
    from src.anime.series_job.actions import ReleaseInvalid, SeriesDecided, SeriesNotFound
    from src.anime.storage.connection import database
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--id', dest='anidb_id', type=int, required=True, help='AniDB ID of the series, e.g. 69')
    chosen = parser.add_mutually_exclusive_group()
    for action, help_text in ACTIONS.items():
        chosen.add_argument('--' + action, dest='action', action='store_const', const=action, help=help_text)
    order = parser.add_mutually_exclusive_group()
    order.add_argument('--promote', dest='promote', action='store_const', const=True,
                       help='work on it first: the next free worker takes it, before every other series')
    order.add_argument('--unpromote', dest='promote', action='store_const', const=False,
                       help='put it back in the usual order')
    parser.add_argument('--reason', default='', help='with --reject: a reason code (default REJECTED_BY_USER)')
    parser.add_argument('--note', default='', help='with --reject: why, in your words')
    parser.add_argument('--json', action='store_true', help='show the series as JSON')
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    if (args.reason or args.note) and args.action != 'reject':
        parser.error('--reason and --note go with --reject')
    configuration = ConfigurationStore(args.config_dir).read()
    with database(configuration.value('storage.database_path')) as db:
        if args.action is None and args.promote is None:
            return show(db, args.anidb_id, configuration.value('anime.title_language'), args.json)
        from src.anime.series_job.actions import promote_series_sync
        messages = []
        try:
            if args.action is not None:
                messages.append(run(db, args.anidb_id, args.action, args.reason, args.note)['message'])
            if args.promote is not None:                 # After the action: retry it, then put it first.
                messages.append(promote_series_sync(db, args.anidb_id, promoted=args.promote)['message'])
        except (SeriesNotFound, SeriesDecided, ReleaseInvalid) as exc:
            db.commit()                                  # An action that ran stays done.
            print('\n'.join(messages + [f'Refused: {exc}']), file=sys.stderr)
            return 1
        db.commit()
    print('\n'.join(messages))
    return 0


def run(db, anidb_id, action, reason='', note=''):
    from src.anime.series_job import actions
    if action == 'reject':
        return actions.reject_series_sync(db, anidb_id, reason=reason or 'REJECTED_BY_USER', note_text=note)
    return getattr(actions, f'{action}_series_sync')(db, anidb_id)


def show(db, anidb_id, language, as_json=False):
    import json
    from src.anime.series_job.views import series_view
    item = series_view(db, anidb_id, language)
    if item is None:
        print(f'AniDB {anidb_id} has no series.')
        return 1
    if as_json:
        print(json.dumps(item, indent=2, sort_keys=True))
        return 0
    print(f"Series {item['anidb_id']} {item['title']}: {item['state']}"
          + (" · promoted: worked on first" if item.get('promoted_at_ms') else ""))
    if item.get('stopped'):
        print(f"  Stopped: {item['stopped']} (adm series --id {anidb_id} --retry resumes it)")
    if item.get('waiting'):
        print(f"  Waiting: {item['waiting']} (it goes on by itself once that is fixed)")
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
              f"[{', '.join(t['episodes'])}]" + ('' if t['progress'] is None else f" {int(t['progress'] * 100)}%"))
    print('Recent:')
    for line in item['recent']:
        print('  ' + line)
    return 0
