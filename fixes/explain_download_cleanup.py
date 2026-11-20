#!/usr/bin/env python3
"""Read-only: why did each CLEANUP_DECIDED_DOWNLOAD job stop in Needs review?

Some reviews record no failed_check: the job's ValueError/KeyError/TypeError
branch and the completion check save none. This replays the job's own
checks (Service.facts, then the qBittorrent ownership check) against a
read-only database and one torrents/info + torrents/files read per torrent,
and prints the first check that fails with its file:line.

Nothing is written to the database or sent to qBittorrent except reads.

    python3 fixes/explain_download_cleanup.py [--job N ...] [--config-dir DIR]
"""
import argparse
import json
import os
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixes.cleanup_legacy_not_listed import configured_database, open_database  # noqa: E402
from src.anime.configuration import cli_config_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def where(exc):
    """Innermost src/anime frame of the original error: 'file:LINE function'."""
    chain = exc
    while (chain.__cause__ or chain.__context__) is not None:
        chain = chain.__cause__ or chain.__context__
    found = None
    for frame in traceback.extract_tb(chain.__traceback__):
        try:
            relative = Path(frame.filename).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if relative.startswith('src/anime/'):
            found = f'{relative}:{frame.lineno} {frame.name}  -> {frame.line}'
    check = getattr(exc, 'check', None) or getattr(chain, 'check', None)
    return f"{type(chain).__name__}{' ' + check if check else ''} at {found}"


def explain(service, db, job_id, event_id, payload):
    from src.anime.acquisition.cleanup import owned
    from src.anime.acquisition.transfers import ACTIVE, CHECKING, STOPPED
    from src.anime.storage.job_queue import Claim
    service.claim = Claim(job_id, event_id, 'CLEANUP_DECIDED_DOWNLOAD', json.dumps(payload), 0, '')
    service.decision = payload.get('decision_job_id')
    service.stage_job = payload.get('staging_job_id')
    try:
        op, intent, _, _ = service.facts(db)
    except Exception as exc:
        return 'facts: ' + where(exc), None
    phase = db.execute('SELECT phase FROM cancellation_cleanup WHERE plan_id=?',
                       (intent.plan.plan_id,)).fetchone()
    info = f"op {op} plan {intent.plan.plan_id} journal phase {phase[0] if phase else 'none'}"
    try:
        client = service.client_factory(intent.options, service.secrets.qbittorrent_credentials())
        client.login()
        found = client.inspect(intent.plan.manifest.info_hash)
        if found is None:
            return 'torrent already gone from qBittorrent (job would finish)', info
        current = owned(intent, found)
    except Exception as exc:
        return 'qBittorrent ownership: ' + where(exc), info
    if current.state in STOPPED or current.state in ACTIVE or current.state in CHECKING:
        return f'all checks pass now (qbt state {current.state}): a plain Retry should clean it', info
    return f'qbt state {current.state} is not handled (TORRENT_STATE_UNEXPECTED)', info


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--config-dir', default=os.environ.get('NEXTGEN_CONFIG_DIR') or cli_config_dir())
    p.add_argument('--job', type=int, action='append', help='Only these cleanup job IDs')
    args = p.parse_args(argv)

    from src.anime.configuration import ConfigurationStore
    from src.anime.decision_download_cleanup import Service
    from src.anime.secrets import SecretStore
    configuration = ConfigurationStore(args.config_dir).read()
    service = Service(configuration, SecretStore(args.config_dir))
    service.queue._owns = lambda *a, **k: None    # Not a claimed job: skip the lease check only.
    db = open_database(configured_database(args.config_dir), readonly=True)
    try:
        db.execute('PRAGMA query_only=ON')
        sql = ("SELECT job_id,event_id,payload_json FROM jobs WHERE kind='CLEANUP_DECIDED_DOWNLOAD' "
               "AND state='NEEDS_REVIEW'")
        rows = db.execute(sql + ' ORDER BY job_id').fetchall()
        if args.job:
            rows = [r for r in rows if r[0] in args.job]
        results = []
        for job_id, event_id, payload in rows:
            reason, info = explain(service, db, job_id, event_id, json.loads(payload))
            results.append(reason)
            print(f"job {job_id}: {reason}" + (f"\n          {info}" if info else ''))
    finally:
        db.close()
    print(f"\n{len(results)} job(s) in Needs review. By cause:")
    for reason, count in Counter(results).most_common():
        print(f"{count:5}  {reason}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
