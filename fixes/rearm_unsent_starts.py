#!/usr/bin/env python3
"""Let downloads stuck on "Waiting to confirm the download started" start.

Before the start-not-sent fix, a download or unlock job recorded "start sent"
(START_UNCERTAIN) and then gave up before sending it: a re-read of the torrent
raced a state change (TORRENT_SNAPSHOT_CHANGED). An uncertain start is never
sent again, so the job waited forever on a paused torrent.

For each job given, this checks the stored history proves that attempt gave up
with TORRENT_SNAPSHOT_CHANGED and the download was never seen running, then
puts the transfer back to SELECTED. The job's next attempt sends the start once,
exactly as it normally does. Nothing is sent to qBittorrent by this script.

Preview by default (read-only). --apply backs up the database first. Services
can stay running. A second --apply does nothing.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixes.cleanup_legacy_not_listed import backup, configured_database, open_database  # noqa: E402
from src.anime.storage.migrations import LATEST_VERSION as SCHEMA_VERSION  # noqa: E402

TABLES = ('torrent_transfers', 'batch_torrent_transfers', 'batch_torrent_unlocks')
WAITING = ('PENDING', 'RETRY_WAIT')
HISTORY = """
SELECT event_code, reason_code, json_extract(details_json,'$.attempt') FROM job_history
WHERE job_id=? ORDER BY history_id
"""


def transfer(db, job):
    """(table, plan_id, phase, start_attempt) for the job's own start, or None."""
    for table in TABLES:
        row = db.execute(f'SELECT plan_id,phase,start_attempt FROM {table} WHERE start_job_id=?',
                         (job,)).fetchone()
        if row is not None:
            return (table, *row)
    return None


def never_sent(history, attempt):
    """The start attempt gave up on a changed snapshot and nothing ever ran."""
    mine = [(event, reason) for event, reason, at in history if at == attempt]
    if ('TORRENT_TRANSFER_CHANGED', 'TORRENT_START_UNCERTAIN') not in mine:
        return False
    ended = [reason for event, reason in mine if event == 'DEFERRED']
    running = any(reason in ('TORRENT_DOWNLOAD_IN_PROGRESS', 'TORRENT_DOWNLOAD_COMPLETE')
                  for _, reason, _ in history)
    return ended == ['TORRENT_SNAPSHOT_CHANGED'] and not running


def inspect(db, job):
    row = db.execute('SELECT kind,state FROM jobs WHERE job_id=?', (job,)).fetchone()
    if row is None:
        return {'job_id': job, 'status': 'not_found'}
    kind, state = row
    item = {'job_id': job, 'kind': kind, 'job_state': state}
    found = transfer(db, job)
    if found is None:
        return {**item, 'status': 'no_start_recorded'}
    table, plan, phase, attempt = found
    item.update(table=table, plan_id=plan, phase=phase)
    if phase != 'START_UNCERTAIN':
        return {**item, 'status': 'not_stuck'}
    if not never_sent(db.execute(HISTORY, (job,)).fetchall(), attempt):
        return {**item, 'status': 'start_may_have_been_sent'}
    if state not in WAITING:
        return {**item, 'status': 'job_not_waiting'}        # Running: rerun later. Finished: nothing to do.
    return {**item, 'status': 'rearm'}


def check_version(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(f'Expected database schema v{SCHEMA_VERSION}, found v{version}')


def execute(path, jobs, apply=False):
    path = Path(path)
    with closing(open_database(path, readonly=not apply)) as db:
        check_version(db)
        items = [inspect(db, job) for job in jobs]
        report = {'jobs': items, 'rearmed': [], 'backup': None}
        if not apply or not any(i['status'] == 'rearm' for i in items):
            return report
        report['backup'] = str(backup(db, path, 'rearm-start'))
        db.execute('BEGIN IMMEDIATE')                       # Recheck under the write lock.
        try:
            for item in items:
                if item['status'] != 'rearm' or inspect(db, item['job_id'])['status'] != 'rearm':
                    continue
                db.execute(
                    f"UPDATE {item['table']} SET phase='SELECTED',reason_code='TORRENT_SELECTION_CONFIRMED',"
                    "start_job_id=NULL,start_attempt=NULL WHERE plan_id=? AND phase='START_UNCERTAIN'",
                    (item['plan_id'],))
                item['status'] = 'rearmed'
                report['rearmed'].append(item['job_id'])
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('jobs', nargs='+', type=int, help='DOWNLOAD_TORRENT, TRANSFER_BATCH or UNLOCK_BATCH job IDs')
    parser.add_argument('--config-dir', type=Path, default=Path('/etc/anime-discovery'))
    parser.add_argument('--database', type=Path, help='Override settings.json database path')
    parser.add_argument('--apply', action='store_true', help='Back up the DB and let these jobs send the start')
    args = parser.parse_args(argv)
    try:
        path = args.database or configured_database(args.config_dir)
        report = execute(path, args.jobs, args.apply)
        print(json.dumps(report, indent=2))
        if not args.apply and any(i['status'] == 'rearm' for i in report['jobs']):
            print('Preview only: rerun with --apply to let these jobs start their downloads.')
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, json.JSONDecodeError) as error:
        print(f'Rearm refused: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
