#!/usr/bin/env python3
"""One-time: mark series that were already kept as Accepted (work item P4).

Keep is now a series decision: the series shows as Accepted in Intake and no
more work happens for it. Series kept before this change have a Keep action but
no decision. This adds the ACCEPT decision for each of them, recorded on its
first Keep job, exactly as Keep now does. A series with a decision (Reject or
Accept) is left alone. Nothing is downloaded, searched, canceled or deleted.

Preview by default (read-only, services can stay running). --apply backs up the
database first. A second --apply does nothing.
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
from src.anime.configuration import cli_config_dir  # noqa: E402
from src.anime.storage.migrations import LATEST_VERSION as SCHEMA_VERSION  # noqa: E402

# The first Keep job of every series that has none of its own decision.
KEPT = """
SELECT anidb_id, min(job_id) FROM (
  SELECT r.anidb_id, a.job_id FROM management_actions a
  JOIN job_operation_members m ON m.operation_id=a.operation_id
  JOIN staging_publications s ON s.job_id=m.job_id
  JOIN acquisition_plans p ON p.plan_id=s.plan_id
  JOIN episode_reservations r ON r.reservation_id=p.reservation_id
  WHERE a.action='KEEP'
  UNION
  SELECT r.anidb_id, a.job_id FROM management_actions a
  JOIN job_operation_members m ON m.operation_id=a.operation_id
  JOIN batch_staging_publications s ON s.job_id=m.job_id
  JOIN batch_episode_reservations r ON r.plan_id=s.plan_id AND r.file_index=s.file_index
  WHERE a.action='KEEP')
WHERE anidb_id NOT IN (SELECT anidb_id FROM anime_review_decisions)
GROUP BY anidb_id ORDER BY anidb_id
"""


def inspect(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(f'Expected database schema v{SCHEMA_VERSION}, found v{version}')
    return db.execute(KEPT).fetchall()


def execute(path, apply=False, now=None):
    path = Path(path)
    with closing(open_database(path, readonly=not apply)) as db:
        series = inspect(db)
        report = {'series': [aid for aid, _ in series], 'accepted': 0, 'backup': None}
        if not apply or not series:
            return report
        report['backup'] = str(backup(db, path))
        now = time.time_ns() // 1000000 if now is None else now
        with db:
            for aid, job in inspect(db):
                operation = db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?', (job,)).fetchone()[0]
                db.execute("INSERT INTO anime_review_decisions(anidb_id,operation_id,job_id,decision,cleanup_state,decided_at_ms) "
                           "VALUES(?,?,?,'ACCEPT','COMPLETE',?)", (aid, operation, job, now))
                report['accepted'] += 1
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=Path(cli_config_dir()))
    parser.add_argument('--database', type=Path, help='Override settings.json database path')
    parser.add_argument('--apply', action='store_true', help='Back up the DB, then add the ACCEPT decisions')
    args = parser.parse_args(argv)
    try:
        report = execute(args.database or configured_database(args.config_dir), args.apply)
        print(json.dumps(report, indent=2))
        if not args.apply and report['series']:
            print('Preview only: rerun with --apply to mark these series Accepted.')
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, json.JSONDecodeError) as error:
        print(f'Accept kept series refused: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
