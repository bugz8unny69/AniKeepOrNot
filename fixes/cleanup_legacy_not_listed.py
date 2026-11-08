#!/usr/bin/env python3
"""One-time removal of obsolete Sonarr NOT_LISTED backfill observations.

Preview by default. --apply requires stopped Anime Discovery services, creates
an SQLite backup, and deletes only unclaimed NOT_LISTED result rows. No schema,
job, episode, reservation, candidate or staging data is changed.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

SERVICES = ('anime-discovery-api.service', 'anime-discovery-worker.service')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# The release's own schema version: the fix ships with, and is run from, that release.
from src.anime.storage.migrations import LATEST_VERSION as SCHEMA_VERSION  # noqa: E402

# Each result is historical/observational. Do not reset a result if another
# record suggests the episode has already been selected or acquired.
ROWS = """
SELECT s.plan_id, s.tvdb_episode,
  EXISTS(SELECT 1 FROM episode_release_selections x
         WHERE x.plan_id=s.plan_id AND x.tvdb_episode=s.tvdb_episode),
  EXISTS(SELECT 1 FROM episode_release_candidates x
         WHERE x.plan_id=s.plan_id AND x.tvdb_episode=s.tvdb_episode),
  EXISTS(SELECT 1 FROM user_evaluation_plans p JOIN episode_reservations r
         ON r.tvdb_series_id=p.tvdb_series_id AND r.tvdb_season=p.tvdb_season
         AND r.tvdb_episode=s.tvdb_episode AND r.state='ACTIVE'
         WHERE p.plan_id=s.plan_id),
  EXISTS(SELECT 1 FROM user_evaluation_plans p JOIN batch_episode_reservations r
         ON r.tvdb_series_id=p.tvdb_series_id AND r.tvdb_season=p.tvdb_season
         AND r.tvdb_episode=s.tvdb_episode AND r.state='ACTIVE'
         WHERE p.plan_id=s.plan_id)
FROM episode_discovery_results s
WHERE s.status='NOT_LISTED'
ORDER BY s.plan_id, s.tvdb_episode
"""


def configured_database(config_dir):
    settings = json.loads((Path(config_dir) / 'settings.json').read_text())
    path = settings['settings']['storage']['database_path']
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError('storage.database_path must be an absolute path')
    return Path(path)


def open_database(path, readonly=False):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Database must be an existing regular file, not a symlink')
    return sqlite3.connect(path.resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=5)


def inspect(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(f'Expected NextGen database schema v{SCHEMA_VERSION}, found v{version}')
    rows = db.execute(ROWS).fetchall()
    eligible = [(plan, episode) for plan, episode, *claimed in rows if not any(claimed)]
    protected = [(plan, episode) for plan, episode, *claimed in rows if any(claimed)]
    return eligible, protected


def ensure_stopped():
    for service in SERVICES:
        result = subprocess.run(['systemctl', 'is-active', '--quiet', service], check=False)
        if result.returncode == 0:
            raise RuntimeError(f'Stop {service} before --apply')
        # 3 means inactive; other statuses may indicate failed or unavailable
        # systemctl. Require an unambiguous inactive/failed unit state.
        state = subprocess.run(['systemctl', 'show', '--property=ActiveState', '--value', service],
                               capture_output=True, text=True, check=False)
        if state.returncode != 0 or state.stdout.strip() not in ('inactive', 'failed'):
            raise RuntimeError(f'Cannot verify {service} is stopped')


def backup(db, path):
    target = path.with_name(path.name + '.before-not-listed-' + uuid.uuid4().hex + '.sqlite')
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(target)) as dest:
            db.backup(dest)
            if dest.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise RuntimeError('SQLite backup integrity check failed')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def execute(path, apply=False):
    path = Path(path)
    if apply:
        ensure_stopped()
    with closing(open_database(path, readonly=not apply)) as db:
        eligible, protected = inspect(db)
        report = {'eligible': len(eligible), 'protected': len(protected),
                  'protected_episodes': protected, 'deleted': 0, 'backup': None}
        if not apply or not eligible:
            return report
        saved = backup(db, path)
        report['backup'] = str(saved)
        db.execute('BEGIN IMMEDIATE')
        try:
            again, protected_again = inspect(db)
            if (again, protected_again) != (eligible, protected):
                raise RuntimeError('Backfill observations changed since preview; no changes applied')
            for plan, episode in eligible:
                changed = db.execute(
                    "DELETE FROM episode_discovery_results WHERE plan_id=? "
                    "AND tvdb_episode=? AND status='NOT_LISTED'", (plan, episode)
                ).rowcount
                if changed != 1:
                    raise RuntimeError('Unexpected backfill change; rolling back')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        report['deleted'] = len(eligible)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=Path('/etc/anime-discovery'))
    parser.add_argument('--database', type=Path, help='Override settings.json database path')
    parser.add_argument('--apply', action='store_true', help='Back up DB then perform one-time cleanup')
    args = parser.parse_args(argv)
    try:
        path = args.database or configured_database(args.config_dir)
        report = execute(path, args.apply)
        print(json.dumps(report, indent=2))
        if not args.apply and report['eligible']:
            print('Preview only: stop API and worker, then rerun with --apply.')
        if args.apply and report['deleted']:
            print('Obsolete results cleared. Intake will show Not searched; use Search to retry.')
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, json.JSONDecodeError) as error:
        print(f'Backfill cleanup refused: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
