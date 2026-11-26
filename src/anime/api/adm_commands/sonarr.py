"""adm sonarr: AKON finds dub releases for a series' missing episodes and gives them to Sonarr."""
NAME = 'sonarr'
SUMMARY = "Find dubs for a kept series' missing episodes and push them to Sonarr (runs after every Keep)"


def main(argv, prog):
    import argparse
    from src.anime.configuration import ConfigurationStore, cli_config_dir
    from src.anime.jobs.find_dubs_job import enqueue
    from src.anime.storage.job_queue import JobQueue
    from src.anime.storage.sqlite import transaction
    parser = argparse.ArgumentParser(prog=prog, description="Queue AKON's dub search for the episodes Sonarr is "
                                     'missing; the worker runs it now. Every Keep already does this 10 minutes later.')
    parser.add_argument('action', choices=('find-dubs',))
    parser.add_argument('--id', dest='anidb_id', type=int, required=True, help='AniDB ID of the series, e.g. 18162')
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    database = ConfigurationStore(args.config_dir).read().value('storage.database_path')
    with JobQueue(database).connection() as db, transaction(db):
        row = db.execute('SELECT tvdb_series_id FROM batch_episode_reservations WHERE anidb_id=? '
                         'UNION ALL SELECT tvdb_series_id FROM recognized_series WHERE anidb_id=? LIMIT 1',
                         (args.anidb_id, args.anidb_id)).fetchone()
        if row is None or row[0] is None:
            print(f'AniDB {args.anidb_id}: no TVDB series on record; nothing to search.')
            return 1
        job = enqueue(db, args.anidb_id, row[0], delay_ms=0)
    print(f'Queued: find dubs for AniDB {args.anidb_id} (TVDB {row[0]}) and push them to Sonarr (job {job}). '
          'The worker log names each episode and what Sonarr did.')
    return 0
