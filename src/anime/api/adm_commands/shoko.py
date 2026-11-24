"""adm shoko: tell Shoko which AniDB episode each kept file of a series is."""
NAME = 'shoko'
SUMMARY = 'Link a series\' kept files to their episodes in Shoko (runs after every Keep)'


def main(argv, prog):
    import argparse
    from src.anime.configuration import ConfigurationStore, cli_config_dir
    from src.anime.jobs.link_shoko_job import enqueue, kept_files
    from src.anime.storage.job_queue import JobQueue
    from src.anime.storage.sqlite import transaction
    parser = argparse.ArgumentParser(prog=prog, description='Queue the Shoko link for a series; the worker runs it. '
                                     'Every Keep already does this; use it for series kept before.')
    parser.add_argument('link', choices=('link',))
    parser.add_argument('--id', dest='anidb_id', type=int, required=True, help='AniDB ID of the series, e.g. 18162')
    parser.add_argument('--config-dir', default=cli_config_dir())
    args = parser.parse_args(argv)
    database = ConfigurationStore(args.config_dir).read().value('storage.database_path')
    with JobQueue(database).connection() as db, transaction(db):
        files = kept_files(db, args.anidb_id)
        if not files:
            print(f'AniDB {args.anidb_id} has no kept files; nothing to link.')
            return 1
        job = enqueue(db, args.anidb_id)
    print(f'Queued: link {len(files)} kept file(s) of AniDB {args.anidb_id} in Shoko (job {job}). '
          'The worker log says what Shoko did.')
    return 0
