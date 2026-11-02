#!/usr/bin/env python3
"""One-time: move backfill jobs stuck in review for audio to "Waiting for dub".

Before this fix, when English audio was required and every release of an
episode failed (at least one on the audio check), the backfill job stopped in
Needs review (SONARR_CANDIDATES_EXHAUSTED). Now it waits for a dub instead.

This presses the existing Retry on each of those jobs. The worker then searches
Prowlarr once more: a new Dual/Multi Audio release is tried normally, otherwise
the job goes On hold as "Waiting for dub". Jobs where no release failed the
audio check are left in review. Nothing is downloaded or deleted here.

Preview by default. --apply backs up the database first. Services can stay
running. A second --apply does nothing.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixes.cleanup_legacy_not_listed import backup, configured_database, open_database  # noqa: E402
from src.nextgen.acquisition.audio import wants_english  # noqa: E402
from src.nextgen.configuration_schema import FIELDS  # noqa: E402
from src.nextgen.storage.migrations import LATEST_VERSION as SCHEMA_VERSION  # noqa: E402
from src.nextgen.storage.sonarr_backfill import audio_exhausted  # noqa: E402

REASON = 'SONARR_CANDIDATES_EXHAUSTED'
ROWS = """
SELECT job_id, attempt_count, json_extract(payload_json,'$.plan_id'),
       json_extract(payload_json,'$.exhausted_episode')
FROM jobs WHERE kind='DISCOVER_SONARR_BACKFILL' AND state='NEEDS_REVIEW'
AND last_reason_code=? AND json_extract(payload_json,'$.exhausted_episode') IS NOT NULL
ORDER BY job_id
"""


def audio_setting(config_dir):
    settings = json.loads((Path(config_dir) / 'settings.json').read_text())['settings']
    return settings.get('acquisition', {}).get('audio_check', FIELDS['acquisition.audio_check'].default)


def inspect(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(f'Expected NextGen database schema v{SCHEMA_VERSION}, found v{version}')
    rows = db.execute(ROWS, (REASON,)).fetchall()
    eligible = [(job, attempt) for job, attempt, plan, episode in rows
                if audio_exhausted(db, plan, episode)]
    return eligible, len(rows) - len(eligible)


def execute(path, audio, apply=False):
    from src.nextgen.storage.review_retry import request_review_retry, RetryNotAllowed, RetryStale
    path = Path(path)
    with closing(open_database(path, readonly=not apply)) as db:
        eligible, other = inspect(db)
        report = {'audio_check': audio, 'eligible_jobs': [job for job, _ in eligible],
                  'left_in_review': other, 'requeued': 0, 'refused': [], 'backup': None}
        if not apply or not eligible:
            return report
        if not wants_english(audio):
            raise ValueError('Audio check does not require English; these jobs would stop in review again')
        report['backup'] = str(backup(db, path))
    for job, attempt in eligible:
        try:
            request_review_retry(str(path), job, expected_attempt=attempt, expected_reason=REASON)
            report['requeued'] += 1
        except (RetryNotAllowed, RetryStale):
            report['refused'].append(job)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=Path('/etc/anime-discovery'))
    parser.add_argument('--database', type=Path, help='Override settings.json database path')
    parser.add_argument('--apply', action='store_true', help='Back up the DB, then retry the jobs')
    args = parser.parse_args(argv)
    try:
        path = args.database or configured_database(args.config_dir)
        report = execute(path, audio_setting(args.config_dir), args.apply)
        print(json.dumps(report, indent=2))
        if not args.apply and report['eligible_jobs']:
            print('Preview only: rerun with --apply to retry these jobs.')
        if args.apply and report['requeued']:
            print('Retried. Each job either tries a new dubbed release or shows "Waiting for dub".')
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, json.JSONDecodeError) as error:
        print(f'Waiting-for-dub requeue refused: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
