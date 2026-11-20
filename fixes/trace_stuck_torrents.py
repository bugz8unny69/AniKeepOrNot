#!/usr/bin/env python3
"""Read-only: why is each AniKeepOrNot torrent still in qBittorrent?

One qBittorrent call (torrents/info for our category), then read-only SQL.
Every torrent is traced hash -> journal -> job -> operation -> cleanup job,
and grouped by why it is still there. Nothing is changed anywhere.

    python3 fixes/trace_stuck_torrents.py [--config-dir DIR] [--examples N] [--json]
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixes.cleanup_legacy_not_listed import configured_database, open_database  # noqa: E402
from src.anime.configuration import cli_config_dir  # noqa: E402

CLEANUP_KINDS = ('CLEANUP_DECIDED_DOWNLOAD', 'CLEANUP_BATCH_DOWNLOAD', 'CLEANUP_CANCELED_OPERATION')

# Every journal that records a torrent we added: (journal, info_hash, marker, job_id).
JOURNALS = """
SELECT 'magnet_intents', info_hash, marker, job_id, state FROM magnet_intents
UNION ALL
SELECT 'batch_metadata_admissions', info_hash, ownership_marker, verification_job_id, state
  FROM batch_metadata_admissions
UNION ALL
SELECT 'torrent_submissions', p.info_hash, p.submission_marker, p.job_id, s.state
  FROM acquisition_plans p JOIN torrent_submissions s USING(plan_id)
UNION ALL
SELECT 'batch_torrent_submissions', info_hash, ownership_marker, reservation_job_id, state
  FROM batch_torrent_submissions
"""

OPERATION = """
SELECT o.operation_id, o.state, o.cleanup_state, o.reason_code
FROM job_operation_members m JOIN job_operations o USING(operation_id) WHERE m.job_id=?
"""

LAST_JOB = """
SELECT j.job_id, j.kind, j.state, j.last_reason_code FROM jobs j
JOIN job_operation_members m USING(job_id) WHERE m.operation_id=?
ORDER BY j.updated_at_ms DESC, j.job_id DESC LIMIT 1
"""

# Cleanup jobs tied to the operation: created by a member, or pointing at one in the payload.
CLEANUP_JOB = f"""
SELECT c.job_id, c.kind, c.state, c.last_reason_code FROM jobs c
WHERE c.kind IN ({','.join('?' * len(CLEANUP_KINDS))}) AND (
  c.created_by_job_id IN (SELECT job_id FROM job_operation_members WHERE operation_id=?)
  OR json_extract(c.payload_json,'$.staging_job_id') IN (SELECT job_id FROM job_operation_members WHERE operation_id=?)
  OR json_extract(c.payload_json,'$.decision_job_id') IN (SELECT job_id FROM job_operation_members WHERE operation_id=?)
  OR json_extract(c.payload_json,'$.target_job_id') IN (SELECT job_id FROM job_operation_members WHERE operation_id=?))
ORDER BY c.job_id DESC LIMIT 1
"""


def qbittorrent_torrents(config_dir):
    """All torrents in our category, in one call."""
    from src.anime.acquisition.settings import download_options
    from src.anime.configuration import ConfigurationStore
    from src.anime.secrets import SecretStore
    from src.anime.torrent_client.qbittorrent import Client, _json
    configuration = ConfigurationStore(config_dir).read()
    options = download_options(configuration)
    client = Client(options, SecretStore(config_dir).qbittorrent_credentials())
    client.login()
    raw, _ = client._request('torrents/info', query={'category': options.category}, limit=268435456)
    rows = _json(raw)
    return options, [r for r in rows if r.get('category') == options.category]


def trace(db, torrent, journals):
    digest = torrent['hash']
    tags = [t.strip() for t in (torrent.get('tags') or '').split(',') if t.strip()]
    row = {'hash': digest, 'name': torrent.get('name'), 'qbt_state': torrent.get('state'),
           'progress': torrent.get('progress'), 'save_path': torrent.get('save_path'), 'tags': tags}
    records = journals.get(digest, [])
    if not records:
        row['bucket'] = 'NOT_OWNED (no journal row: DB wipe leftover, or added outside the app)'
        return row
    journal, marker, job_id, journal_state = records[0]
    row.update(journal=journal, marker=marker, journal_state=journal_state, job_id=job_id,
               journal_rows=len(records), tag_matches=('ad-' + marker) in tags,
               save_path_matches=(torrent.get('save_path') or '').rstrip('/').endswith('/ad-' + marker))
    op = db.execute(OPERATION, (job_id,)).fetchone()
    if op is None:
        row['bucket'] = 'JOB_HAS_NO_OPERATION'
        return row
    row.update(operation_id=op[0], op_state=op[1], cleanup_state=op[2], op_reason=op[3])
    last = db.execute(LAST_JOB, (op[0],)).fetchone()
    if last:
        row['last_job'] = dict(zip(('job_id', 'kind', 'state', 'reason'), last))
    cleanup = db.execute(CLEANUP_JOB, (*CLEANUP_KINDS, op[0], op[0], op[0], op[0])).fetchone()
    if cleanup:
        row['cleanup_job'] = dict(zip(('job_id', 'kind', 'state', 'reason'), cleanup))
    row['bucket'] = bucket(row)
    return row


def bucket(r):
    cleanup = r.get('cleanup_job')
    state = r['op_state']
    if state == 'ACTIVE':
        last = r.get('last_job') or {}
        text = f"ACTIVE · last job {last.get('kind')} {last.get('state')}"
        return text + f" {last['reason']}" if last.get('reason') else text
    if cleanup is None:
        return f"{state} (cleanup {r['cleanup_state']}) · NO cleanup job was ever queued"
    text = f"{state} (cleanup {r['cleanup_state']}) · {cleanup['kind']} {cleanup['state']}"
    if cleanup['reason']:
        text += f" {cleanup['reason']}"
    if cleanup['state'] == 'DONE' or r['cleanup_state'] == 'COMPLETE':
        # Cleanup says done but the torrent is still here.
        if not r['tag_matches']:
            text += ' · TAG MISSING (cleanup saw it as foreign and skipped it)'
        elif r['journal_state'] in ('RELEASED', 'EXTERNAL'):
            text += f" · journal {r['journal_state']} (no longer looked at)"
    return text


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--config-dir', default=os.environ.get('NEXTGEN_CONFIG_DIR') or cli_config_dir())
    p.add_argument('--examples', type=int, default=3, help='Example torrents shown per group')
    p.add_argument('--json', action='store_true', help='Every torrent with its full trace')
    args = p.parse_args(argv)

    options, torrents = qbittorrent_torrents(args.config_dir)
    db = open_database(configured_database(args.config_dir), readonly=True)
    try:
        db.execute('PRAGMA query_only=ON')
        journals = defaultdict(list)
        for journal, digest, marker, job_id, state in db.execute(JOURNALS):
            journals[digest].append((journal, marker, job_id, state))
        rows = [trace(db, t, journals) for t in torrents]
    finally:
        db.close()

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    groups = Counter(r['bucket'] for r in rows)
    print(f"Torrents in category '{options.category}': {len(rows)}\n")
    for name, count in groups.most_common():
        print(f"{count:6}  {name}")
        for r in [r for r in rows if r['bucket'] == name][:args.examples]:
            where = f"op {r['operation_id']} job {r['job_id']}" if 'operation_id' in r else 'no record'
            print(f"          {r['hash'][:8]}  {where}  qbt={r['qbt_state']}  {r['name']}")
    print("\nNext step for any example: adm report job --id <job>")
    return 0


if __name__ == '__main__':
    sys.exit(main())
