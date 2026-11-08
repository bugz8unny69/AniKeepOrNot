#!/usr/bin/env python3
"""One-time: queue torrent removal for episodes staged before PR #204.

Since #204, staging queues the owned-download cleanup automatically. Episodes
staged earlier still have their torrent in qBittorrent until Keep or Reject.
This queues the same CLEANUP_DECIDED_DOWNLOAD job ({"staging_job_id": N}) for
each of them. The worker then stops and removes the exact owned torrent and
leftover download files; staged videos and the library are never touched.

Preview by default. --apply backs up the database and only inserts jobs.
Episodes with a Keep/Reject decision, a cleanup already started, or a queued
staging cleanup are left alone. A second --apply does nothing.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixes.cleanup_legacy_not_listed import backup, configured_database, open_database  # noqa: E402

# The release's own schema version: the fix ships with, and is run from, that release.
from src.nextgen.storage.migrations import LATEST_VERSION as SCHEMA_VERSION  # noqa: E402
KIND = 'CLEANUP_DECIDED_DOWNLOAD'

ROWS = """
SELECT s.job_id, j.event_id, o.operation_id,
  EXISTS(SELECT 1 FROM management_actions a
         WHERE a.operation_id=o.operation_id AND a.action IN ('KEEP','REJECT')),
  EXISTS(SELECT 1 FROM cancellation_cleanup c WHERE c.plan_id=s.plan_id),
  EXISTS(SELECT 1 FROM jobs x WHERE x.kind=?
         AND json_extract(x.payload_json,'$.staging_job_id')=s.job_id)
FROM staging_publications s
JOIN jobs j ON j.job_id=s.job_id AND j.kind='STAGE_USER_EVALUATION_MEDIA' AND j.state='DONE'
JOIN job_operation_members m ON m.job_id=s.job_id
JOIN job_operations o ON o.operation_id=m.operation_id AND o.state='STAGED'
WHERE s.state='PUBLISHED'
ORDER BY s.job_id
"""


def inspect(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(f'Expected NextGen database schema v{SCHEMA_VERSION}, found v{version}')
    rows = db.execute(ROWS, (KIND,)).fetchall()
    eligible = [(job, event) for job, event, _, *skip in rows if not any(skip)]
    skipped = [job for job, _, _, *skip in rows if any(skip)]
    return eligible, skipped


def execute(path, apply=False):
    from src.nextgen.storage.sqlite import enqueue_job
    path = Path(path)
    with closing(open_database(path, readonly=not apply)) as db:
        eligible, skipped = inspect(db)
        report = {'eligible_staging_jobs': [job for job, _ in eligible],
                  'skipped': len(skipped), 'queued': 0, 'backup': None}
        if not apply or not eligible:
            return report
        report['backup'] = str(backup(db, path))
        db.execute('BEGIN IMMEDIATE')
        try:
            if inspect(db)[0] != eligible:
                raise RuntimeError('Staged episodes changed since preview; no changes applied')
            now = time.time_ns() // 1_000_000
            for job, event in eligible:
                enqueue_job(db, kind=KIND, payload={'staging_job_id': job}, component='worker',
                            event_id=event, created_by_job_id=job, available_at_ms=now)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        report['queued'] = len(eligible)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=Path('/etc/anime-discovery'))
    parser.add_argument('--database', type=Path, help='Override settings.json database path')
    parser.add_argument('--apply', action='store_true', help='Back up the DB, then queue the cleanups')
    args = parser.parse_args(argv)
    try:
        path = args.database or configured_database(args.config_dir)
        report = execute(path, args.apply)
        print(json.dumps(report, indent=2))
        if not args.apply and report['eligible_staging_jobs']:
            print('Preview only: rerun with --apply to queue the torrent removals.')
        if args.apply and report['queued']:
            print('Queued. The worker removes the torrents shortly; check each job in the dashboard.')
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, json.JSONDecodeError) as error:
        print(f'Staged torrent cleanup refused: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
