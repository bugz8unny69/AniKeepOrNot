#!/usr/bin/env python3
"""Send stopped releases through identification again, from what we stored.

Some release checks (VERIFY_RELEASE) stopped in Needs review because
identification picked the wrong AniDB entry (for example season 1 for an S02
release), so the episode could not be mapped. Retry reuses that same identity.

For each job given, this cancels the stopped release check (nothing was
downloaded yet, so the cancel finishes at once), then queues the release again
as a new delivery, exactly as the webhook does: the same stored release name
and magnet, a fresh IDENTIFY_ANIME. Nothing is searched for and Auto BR is not
involved. The new identification then queues its own release check.

Preview by default (read-only). --apply backs up the database first. Services
can stay running. A second --apply does nothing: a job already sent again is
reported with its new delivery. If a cancel is still being cleaned up, rerun
--apply later and the release is sent then.
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

# Reasons that can come from a wrong AniDB entry.
REASONS = ('RELEASE_EPISODE_MAPPING_MISSING', 'RELEASE_TVDB_EPISODE_MISSING',
           'RELEASE_MAPPING_MISSING', 'SEASON_PACK_FILES_UNCLEAR')
JOB = """
SELECT j.kind, j.state, j.last_reason_code, j.event_id, json_extract(j.payload_json,'$.anidb_id'),
       e.source, e.raw_body, o.state, o.cleanup_state
FROM jobs j JOIN incoming_events e USING(event_id)
LEFT JOIN job_operation_members m ON m.job_id=j.job_id
LEFT JOIN job_operations o ON o.operation_id=m.operation_id
WHERE j.job_id=?
"""


def release_name(raw):
    try:
        name = json.loads(raw).get('release_name')
        return name if type(name) is str else None
    except (ValueError, TypeError, AttributeError):
        return None


def resent(db, event, source, raw):
    """The delivery already sent again for this release, or None."""
    row = db.execute('SELECT event_id FROM incoming_events WHERE source=? AND raw_body=? AND event_id>? '
                     'ORDER BY event_id LIMIT 1', (source, raw, event)).fetchone()
    return None if row is None else row[0]


def inspect(db, job):
    row = db.execute(JOB, (job,)).fetchone()
    if row is None:
        return {'job_id': job, 'status': 'not_found'}
    kind, state, reason, event, aid, source, raw, op_state, cleanup = row
    item = {'job_id': job, 'release': release_name(raw), 'anidb_id': aid, 'event_id': event,
            'job_state': state, 'reason': reason}
    if kind != 'VERIFY_RELEASE':
        return {**item, 'status': 'not_a_release_check'}
    again = resent(db, event, source, raw)
    if again is not None:
        return {**item, 'status': 'already_sent', 'new_event_id': again}
    if state == 'NEEDS_REVIEW' and reason in REASONS:
        return {**item, 'status': 'cancel_and_send'}
    if state == 'CANCELED' and (op_state, cleanup) == ('CANCELED', 'COMPLETE'):
        return {**item, 'status': 'send'}            # Canceled earlier: only the resend is left.
    if state == 'CANCELED':
        return {**item, 'status': 'cancel_pending'}  # Cleanup still running: rerun later.
    return {**item, 'status': 'not_eligible'}


def check_version(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(f'Expected database schema v{SCHEMA_VERSION}, found v{version}')


def send(path, job):
    """Queue the stored delivery again as a new identification."""
    from src.anime.storage.sqlite import connect, accept_event_and_job
    with closing(connect(path)) as db:
        _, _, _, event, _, source, raw, _, _ = db.execute(JOB, (job,)).fetchone()
        if resent(db, event, source, raw) is not None:
            raise RuntimeError('already sent')
        return accept_event_and_job(db, source, raw, kind='IDENTIFY_ANIME', payload={}, component='intake')


def execute(path, jobs, apply=False):
    from src.anime.storage.management_actions import ManagementActions
    path = Path(path)
    with closing(open_database(path, readonly=not apply)) as db:
        check_version(db)
        items = [inspect(db, job) for job in jobs]
        report = {'jobs': items, 'sent': [], 'refused': [], 'backup': None}
        todo = [item for item in items if item['status'] in ('cancel_and_send', 'send')]
        if not apply or not todo:
            return report
        report['backup'] = str(backup(db, path, 'resend'))
    actions = ManagementActions(str(path))
    for item in todo:
        job = item['job_id']
        try:
            if item['status'] == 'cancel_and_send':
                actions.submit(job, 'cancel')
            with closing(open_database(path, readonly=True)) as db:
                now = inspect(db, job)
            item['status'] = now['status']
            if now['status'] != 'send':
                continue                              # Cancel still cleaning up: rerun --apply later.
            event, identify = send(path, job)
            item.update(status='sent', new_event_id=event, identify_job_id=identify)
            report['sent'].append(job)
        except Exception as error:                   # One job never stops the others.
            report['refused'].append({'job_id': job, 'error': f'{type(error).__name__}: {error}'})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('jobs', nargs='+', type=int, help='VERIFY_RELEASE job IDs')
    parser.add_argument('--config-dir', type=Path, default=Path('/etc/anime-discovery'))
    parser.add_argument('--database', type=Path, help='Override settings.json database path')
    parser.add_argument('--apply', action='store_true', help='Back up the DB, cancel, and send again')
    args = parser.parse_args(argv)
    try:
        path = args.database or configured_database(args.config_dir)
        report = execute(path, args.jobs, args.apply)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if not args.apply and any(i['status'] in ('cancel_and_send', 'send') for i in report['jobs']):
            print('Preview only: rerun with --apply to cancel these release checks and identify them again.')
        return 1 if report['refused'] else 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, json.JSONDecodeError) as error:
        print(f'Resend refused: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
