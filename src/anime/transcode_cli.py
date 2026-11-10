"""adm transcode staged/status: queue work locally; encoding belongs to the worker."""
import argparse
import sys

from src.anime.configuration import ConfigurationStore, cli_config_dir
from src.anime.maintenance import MaintenanceGate
from src.anime.services.transcode import TranscodeWaiting
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import transaction
from src.anime.transcode_batch import enqueue, dry_run, status


def main(argv=None):
    parser = argparse.ArgumentParser(description='Transcode staged videos in the background')
    parser.add_argument('--config-dir', default=cli_config_dir())
    commands = parser.add_subparsers(dest='command', required=True)
    staged = commands.add_parser('staged')
    staged.add_argument('--series', type=int, metavar='ANIDB_ID')
    staged.add_argument('--dry-run', action='store_true')
    staged.add_argument('--yes', action='store_true')
    commands.add_parser('status')
    args = parser.parse_args(argv)
    if args.command == 'staged' and args.series is not None and args.series <= 0:
        parser.error('--series must be a positive AniDB ID')
    config = ConfigurationStore(args.config_dir).read()
    database = config.value('storage.database_path')
    queue = JobQueue(database)
    with MaintenanceGate(database).operation():
        if args.command == 'status':
            with queue.connection() as db: batch = status(db)
            if batch is None:
                print('No transcode batch.')
                return 0
            print(f"Batch {batch['batch_id']} {batch['state']}: {batch['done']+batch['skipped']+batch['failed']}/{batch['total']}, "
                  f"{batch['done']} done, {batch['skipped']} skipped, {batch['failed']} failed")
            if batch['current_file']: print(batch['current_file'])
            for failed in batch['failed_files']: print(f"Failed: {failed['file']}: {failed['error']}")
            return 0
        if args.dry_run:
            with queue.connection() as db: files = dry_run(db, config, args.series)
            for item in files: print(f"{item['path']}: {item['size']/1e6:.1f} MB -> ~{item['target']/1e6:.1f} MB")
            print(f"{len(files)} files; estimated saving {sum(item['saved'] for item in files)/1e6:.1f} MB")
            return 0
        if config.value('transcode.mode') == 'off':
            parser.error('Configure Local or Remote mode in Settings > Transcode first')
        if not args.yes:
            answer = input('This is a one-way transcode. Staged videos are replaced and the originals cannot be recovered. Continue? [y/N] ')
            if answer.strip().lower() not in ('y', 'yes'): return 0
        try:
            with queue.connection() as db, transaction(db): batch = enqueue(db, anidb_id=args.series)
        except TranscodeWaiting as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f'Queued transcode batch {batch}; the worker will process it. Use adm transcode status for progress.')
    return 0


if __name__ == '__main__': raise SystemExit(main())
