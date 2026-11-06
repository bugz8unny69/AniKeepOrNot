"""Historical job recovery, diagnostic classification, and safe retry CLI.

Coordinates with MaintenanceGate and reuses request_review_retry / ManagementActions.
Never performs ad-hoc SQL updates or resets attempt counters.
Preview is read-only; the apply subcommand explicitly requests mutations.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.maintenance import MaintenanceGate
from src.nextgen.messages import MessageCatalog
from src.nextgen.storage.review_retry import (
    capability,
    request_review_retry,
    RetryNotAllowed,
    RetryStale,
)

CATALOG = MessageCatalog()

RECOVERY_MATRIX = {
    'TORRENT_METADATA_TIMEOUT': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Candidate metadata timed out; retrying activates the next candidate.',
        'action': 'retry',
    },
    'ACQUISITION_SIZE_LIMIT': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Release exceeded size limit; retrying activates the next candidate.',
        'action': 'retry',
    },
    'TORRENT_PIECE_OVERLAP': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Piece overlap policy relaxed; retrying proceeds safely.',
        'action': 'retry',
    },
    'KEEP_IMPORT_REVIEW': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Batch keep receipt, plan linkage, and NFS trust fixed; retrying re-attempts import.',
        'action': 'retry',
    },
    'STAGING_REVIEW': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Staging requirements simplified; retrying re-attempts staging.',
        'action': 'retry',
    },
    'IDENTITY_INVALID': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'AnimeSeries schema attributes validation fixed; retrying proceeds.',
        'action': 'retry',
    },
    'PROWLARR_SEARCH_REVIEW': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Search failure classified; retrying searches again.',
        'action': 'retry',
    },
    'SONARR_CANDIDATES_EXHAUSTED': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Candidates exhausted; retrying searches again or marks waiting for dub.',
        'action': 'retry',
    },
    'WAITING_FOR_DUB': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Waiting for dub; retrying initiates Search Again for dual/multi audio.',
        'action': 'retry',
    },
    'RELEASE_EPISODE_MAPPING_MISSING': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'AniDB-first verification decoupled from TVDB; retrying proceeds.',
        'action': 'retry',
    },
    'MEDIA_AUDIO_REQUIRED': {
        'category': 'AUTONOMOUS_RETRY',
        'description': 'Media audio required; retrying activates next ranked candidate if available.',
        'action': 'retry',
    },
    'IDENTITY_INCOMPLETE': {
        'category': 'MANUAL_ACTION',
        'description': 'Ambiguous or unverified AniDB series identity; enter match in Matching details.',
        'action': 'manual_identity',
    },
    'IDENTITY_CONFLICT': {
        'category': 'MANUAL_ACTION',
        'description': 'Conflicting AniDB / TVDB identity mappings; review and confirm series identity.',
        'action': 'manual_identity',
    },
    'RELEASE_TYPE_UNSUPPORTED': {
        'category': 'MANUAL_ACTION',
        'description': 'Unsupported release format (e.g. movie/specials); inspect release.',
        'action': 'inspect_release',
    },
    'CANCELLATION_REVIEW': {
        'category': 'MANUAL_ACTION',
        'description': 'Cleanup stopped to protect unowned or unexpected files; inspect with: adm report job --id N',
        'action': 'inspect_report',
    },
    'DECISION_DOWNLOAD_CLEANUP_REVIEW': {
        'category': 'MANUAL_ACTION',
        'description': 'Download cleanup stopped; inspect via: adm report job --id N',
        'action': 'inspect_report',
    },
    'TORRENT_METADATA_CONFIGURATION_INVALID': {
        'category': 'MANUAL_ACTION',
        'description': 'Torrent metadata or indexer configuration invalid; verify provider credentials.',
        'action': 'check_configuration',
    },
}


def connect(config_dir, readonly=True):
    database = ConfigurationStore(config_dir).read().value('storage.database_path')
    db = sqlite3.connect(f'file:{database}?mode={"ro" if readonly else "rw"}', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    if readonly:
        db.execute('PRAGMA query_only=ON')
    return database, db


def create_backup(database_path, dest_dir=None):
    """Create a consistent SQLite online backup before any recovery mutation."""
    dest_dir = Path(dest_dir or Path(database_path).parent)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f"{Path(database_path).stem}_backup_{timestamp}.sqlite"
    backup_file = dest_dir / backup_name
    try:
        dest = sqlite3.connect(backup_file)
    except (PermissionError, OSError):
        backup_file = Path('/tmp') / backup_name
        dest = sqlite3.connect(backup_file)
    source = sqlite3.connect(f'file:{database_path}?mode=ro', uri=True, timeout=15)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    try:
        os.chmod(backup_file, 0o600)
    except OSError:
        pass
    return str(backup_file)


def inspect_stopped_jobs(db):
    """Retrieve stopped jobs and evaluate recovery capability."""
    sql = """
    SELECT j.job_id, j.kind, j.state, j.attempt_count, j.last_reason_code,
           j.created_at_ms, j.updated_at_ms, j.lease_token, j.lease_expires_at_ms,
           o.state AS op_state, o.cleanup_state,
           (SELECT h.details_json FROM job_history h 
            WHERE h.job_id=j.job_id ORDER BY h.history_id DESC LIMIT 1) AS last_details
    FROM jobs j
    LEFT JOIN job_operation_members m ON m.job_id=j.job_id
    LEFT JOIN job_operations o ON o.operation_id=m.operation_id
    WHERE j.state IN ('NEEDS_REVIEW', 'BLOCKED', 'ON_HOLD', 'FAILED')
    ORDER BY j.job_id
    """
    cursor = db.execute(sql)
    cols = [c[0] for c in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    results = []
    for row in rows:
        job_id = row['job_id']
        cap = capability(db, job_id)
        raw_enabled = cap['enabled']
        disabled_reason = cap['disabled_reason']
        reason_code = row['last_reason_code']
        matrix_entry = RECOVERY_MATRIX.get(reason_code, {
            'category': 'MANUAL_ACTION',
            'description': 'Unclassified stopped job; manual inspection required.',
            'action': 'inspect_job'
        })
        category = matrix_entry['category']
        if not raw_enabled and disabled_reason:
            dis_code = disabled_reason.get('code') if isinstance(disabled_reason, dict) else getattr(disabled_reason, 'code', None)
            if dis_code in ('MANAGEMENT_JOB_RUNNING', 'MANAGEMENT_JOB_QUEUED', 'CANCELLATION_DRAINING'):
                category = 'IN_FLIGHT'

        # Eligible for automated/bulk recovery means retryable by worker policy AND in AUTONOMOUS_RETRY
        auto_eligible = raw_enabled and category == 'AUTONOMOUS_RETRY'

        failed_check = None
        if row['last_details']:
            try:
                details = json.loads(row['last_details'])
                failed_check = details.get('failed_check')
            except (ValueError, TypeError):
                pass

        results.append({
            'job_id': job_id,
            'kind': row['kind'],
            'state': row['state'],
            'attempt': row['attempt_count'],
            'reason_code': reason_code,
            'category': category,
            'eligible': auto_eligible,
            'retryable': raw_enabled,
            'disabled_reason': disabled_reason,
            'recommendation': matrix_entry['description'],
            'failed_check': failed_check,
            'op_state': row['op_state'],
        })
    return results


def build_summary(db):
    """Aggregate stopped jobs by reason and kind."""
    jobs = inspect_stopped_jobs(db)
    summary = {}
    for job in jobs:
        key = (job['reason_code'] or 'UNKNOWN', job['kind'])
        if key not in summary:
            summary[key] = {
                'reason_code': key[0],
                'kind': key[1],
                'total': 0,
                'eligible': 0,
                'manual': 0,
                'in_flight': 0,
                'sample_ids': [],
                'recommendation': job['recommendation'],
                'category': job['category'],
            }
        entry = summary[key]
        entry['total'] += 1
        if job['eligible']:
            entry['eligible'] += 1
        elif job['category'] == 'IN_FLIGHT':
            entry['in_flight'] += 1
        else:
            entry['manual'] += 1
        if len(entry['sample_ids']) < 5:
            entry['sample_ids'].append(job['job_id'])
    return list(summary.values())


def list_jobs(db, *, reason=None, kind=None, eligible_only=False, manual_only=False, limit=50):
    jobs = inspect_stopped_jobs(db)
    filtered = []
    for job in jobs:
        if reason and job['reason_code'] != reason:
            continue
        if kind and job['kind'] != kind:
            continue
        if eligible_only and not job['eligible']:
            continue
        if manual_only and job['eligible']:
            continue
        filtered.append(job)
        if len(filtered) >= limit:
            break
    return filtered


def preview_recovery(db, *, job_id=None, reason=None, all_eligible=False, limit=50):
    jobs = inspect_stopped_jobs(db)
    targets = []
    for job in jobs:
        if job_id is not None:
            if job['job_id'] == job_id:
                targets.append(job)
                break
        elif reason is not None:
            # A reason is an explicit operator selection, not autonomous recovery.
            if job['reason_code'] == reason:
                targets.append(job)
        elif all_eligible:
            if job['eligible']:
                targets.append(job)
        if len(targets) >= limit:
            break

    previews = []
    for t in targets:
        # Operator-selected jobs may override the recommendation matrix, but
        # must still pass the same capability gate as dashboard Retry.
        can_retry = t['retryable'] if job_id is not None or reason is not None else t['eligible']
        previews.append({
            'job_id': t['job_id'],
            'kind': t['kind'],
            'state': t['state'],
            'attempt': t['attempt'],
            'reason': t['reason_code'],
            'eligible': can_retry,
            'recommendation': t['recommendation'],
            'failed_check': t['failed_check'],
            'would_transition': 'PENDING' if can_retry else 'BLOCKED_FROM_RETRY',
            'disabled_reason': t['disabled_reason'],
        })
    return previews


def apply_recovery(config_dir, *, job_id=None, reason=None, all_eligible=False, limit=50, backup=True):
    database, db = connect(config_dir, readonly=True)
    limit = max(1, min(limit, 100))
    previews = preview_recovery(db, job_id=job_id, reason=reason, all_eligible=all_eligible, limit=limit)
    db.close()

    eligible_jobs = [p for p in previews if p['eligible']]
    if not eligible_jobs:
        return {
            'backup_path': None,
            'recovered': [],
            'failed': [],
            'total_eligible': 0,
            'total_attempted': 0,
            'message': 'No eligible stopped jobs to recover matching criteria.',
        }

    backup_path = None
    if backup:
        backup_path = create_backup(database)

    recovered = []
    failed = []

    with MaintenanceGate(database).operation():
        for job in eligible_jobs:
            jid = job['job_id']
            attempt = job['attempt']
            rc = job['reason']
            try:
                receipt = request_review_retry(
                    database,
                    jid,
                    expected_attempt=attempt,
                    expected_reason=rc,
                )
                recovered.append({
                    'job_id': jid,
                    'kind': job['kind'],
                    'attempt': attempt,
                    'history_id': receipt.history_id,
                    'outcome': receipt.outcome,
                })
            except (RetryNotAllowed, RetryStale, ValueError) as exc:
                failed.append({
                    'job_id': jid,
                    'kind': job['kind'],
                    'error': str(exc) or exc.__class__.__name__,
                })

    return {
        'backup_path': backup_path,
        'recovered': recovered,
        'failed': failed,
        'total_eligible': len(eligible_jobs),
        'total_attempted': len(eligible_jobs),
        'message': f"Recovered {len(recovered)} jobs into PENDING (failed: {len(failed)}).",
    }


def format_table(headers, rows):
    if not rows:
        return 'No records found.'
    widths = [len(h) for h in headers]
    for r in rows:
        for idx, col in enumerate(r):
            widths[idx] = max(widths[idx], len(str(col)))
    fmt = '  '.join(f'{{:<{w}}}' for w in widths)
    separator = '  '.join('-' * w for w in widths)
    lines = [fmt.format(*headers), separator]
    for r in rows:
        lines.append(fmt.format(*[str(c) for c in r]))
    return '\n'.join(lines)


def parser():
    p = argparse.ArgumentParser(
        prog='adm recover',
        description='Historical stopped jobs recovery, diagnostic classification, and safe retry tooling'
    )
    p.add_argument('--config-dir', default='/etc/anime-discovery', help='Path to anime-discovery configuration directory')
    p.add_argument('--json', action='store_true', help='Output results as structured JSON')

    sub = p.add_subparsers(dest='action', required=False)

    # summary
    sub_summary = sub.add_parser('summary', help='Summary of stopped jobs by reason and recovery category')
    sub_summary.add_argument('--json', action='store_true', default=argparse.SUPPRESS)

    # list
    sub_list = sub.add_parser('list', help='List individual stopped jobs with recovery eligibility')
    sub_list.add_argument('--reason', help='Filter by reason code')
    sub_list.add_argument('--kind', help='Filter by job kind')
    sub_list.add_argument('--eligible-only', action='store_true', help='Show only jobs ready to be retried')
    sub_list.add_argument('--manual-only', action='store_true', help='Show only jobs requiring manual intervention')
    sub_list.add_argument('--limit', type=int, default=50, help='Maximum rows to display (default: 50)')
    sub_list.add_argument('--json', action='store_true', default=argparse.SUPPRESS)

    # preview
    sub_preview = sub.add_parser('preview', help='Dry-run preview of recovery action (no database changes)')
    sub_preview.add_argument('--job-id', type=int, help='Preview recovery of a specific job ID')
    sub_preview.add_argument('--reason', help='Preview recovery of all jobs stopped with reason')
    sub_preview.add_argument('--all-eligible', action='store_true', help='Preview recovery of all eligible stopped jobs')
    sub_preview.add_argument('--limit', type=int, default=50, help='Maximum jobs to preview (default: 50, max: 100)')
    sub_preview.add_argument('--json', action='store_true', default=argparse.SUPPRESS)

    # apply
    sub_apply = sub.add_parser('apply', help='Execute recovery mutations to requeue eligible jobs')
    sub_apply.add_argument('--job-id', type=int, help='Recover a specific job ID')
    sub_apply.add_argument('--reason', help='Recover jobs stopped with specific reason')
    sub_apply.add_argument('--all-eligible', action='store_true', help='Recover all eligible stopped jobs')
    sub_apply.add_argument('--limit', type=int, default=50, help='Maximum jobs to recover (default: 50, max: 100)')
    sub_apply.add_argument('--no-backup', action='store_true', help='Skip SQLite online pre-recovery backup')
    # Backward compatibility only. The "apply" subcommand already expresses
    # the operator's intent; do not require a redundant confirmation flag.
    sub_apply.add_argument('--apply', action='store_true', help=argparse.SUPPRESS)
    sub_apply.add_argument('--json', action='store_true', default=argparse.SUPPRESS)

    return p


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ['resv']:
        from src.nextgen.episode_reservation_cli import main as resv
        return resv(args[1:])
    if args[:1] == ['batch-audit']:
        # Batch backlog audit and consolidation; preview unless --apply.
        from src.nextgen.batch.backlog_audit import main as batch_audit
        return batch_audit(args[1:])
    parsed = parser().parse_args(argv)
    action = parsed.action or 'summary'
    is_json = getattr(parsed, 'json', False)

    try:
        if action == 'summary':
            _, db = connect(parsed.config_dir, readonly=True)
            try:
                res = build_summary(db)
            finally:
                db.close()
            if is_json:
                print(json.dumps({'summary': res}, indent=2))
            else:
                headers = ['Reason Code', 'Kind', 'Total', 'Eligible', 'Manual', 'Sample IDs']
                rows = []
                total_stopped = sum(r['total'] for r in res)
                total_eligible = sum(r['eligible'] for r in res)
                total_manual = sum(r['manual'] for r in res)
                for r in res:
                    rows.append([
                        r['reason_code'],
                        r['kind'],
                        r['total'],
                        r['eligible'],
                        r['manual'],
                        ', '.join(str(i) for i in r['sample_ids']),
                    ])
                print('\nAnime Discovery — Stopped Jobs Recovery Summary\n')
                print(format_table(headers, rows))
                print(f'\nTotal stopped: {total_stopped} | Eligible for safe recovery: {total_eligible} | Manual action required: {total_manual}\n')
                if total_eligible > 0:
                    print('Run "sudo adm recover preview --all-eligible" to inspect recovery plan.')
                    print('Run "sudo adm recover apply --all-eligible" to resume eligible jobs.\n')
            return 0

        elif action == 'list':
            _, db = connect(parsed.config_dir, readonly=True)
            try:
                res = list_jobs(
                    db,
                    reason=parsed.reason,
                    kind=parsed.kind,
                    eligible_only=parsed.eligible_only,
                    manual_only=parsed.manual_only,
                    limit=parsed.limit,
                )
            finally:
                db.close()
            if is_json:
                print(json.dumps({'jobs': res}, indent=2))
            else:
                headers = ['Job ID', 'Kind', 'State', 'Att', 'Reason', 'Eligible', 'Failed Check']
                rows = []
                for r in res:
                    rows.append([
                        r['job_id'],
                        r['kind'],
                        r['state'],
                        r['attempt'],
                        r['reason_code'] or '-',
                        'YES' if r['eligible'] else 'NO',
                        r['failed_check'] or '-',
                    ])
                print('\nStopped Jobs Inventory\n')
                print(format_table(headers, rows))
                print(f'\nShowing {len(res)} jobs.\n')
            return 0

        elif action == 'preview':
            if not parsed.job_id and not parsed.reason and not parsed.all_eligible:
                print('Error: Specify --job-id, --reason, or --all-eligible for preview.', file=sys.stderr)
                return 1
            _, db = connect(parsed.config_dir, readonly=True)
            try:
                res = preview_recovery(
                    db,
                    job_id=parsed.job_id,
                    reason=parsed.reason,
                    all_eligible=parsed.all_eligible,
                    limit=parsed.limit,
                )
            finally:
                db.close()
            if is_json:
                print(json.dumps({'preview': res}, indent=2))
            else:
                headers = ['Job ID', 'Kind', 'State', 'Reason', 'Eligible', 'Action Transition']
                rows = []
                for r in res:
                    rows.append([
                        r['job_id'],
                        r['kind'],
                        r['state'],
                        r['reason'] or '-',
                        'YES' if r['eligible'] else 'NO',
                        f"-> {r['would_transition']} ({r['recommendation']})",
                    ])
                print('\nRecovery Dry-Run Preview (No database changes made)\n')
                print(format_table(headers, rows))
                eligible_cnt = sum(1 for r in res if r['eligible'])
                print(f'\nPreviewed {len(res)} jobs ({eligible_cnt} eligible to requeue).\n')
            return 0

        elif action == 'apply':
            if not parsed.job_id and not parsed.reason and not parsed.all_eligible:
                print('Error: Specify --job-id, --reason, or --all-eligible for apply.', file=sys.stderr)
                return 1
            res = apply_recovery(
                parsed.config_dir,
                job_id=parsed.job_id,
                reason=parsed.reason,
                all_eligible=parsed.all_eligible,
                limit=parsed.limit,
                backup=not parsed.no_backup,
            )
            if is_json:
                print(json.dumps(res, indent=2))
            else:
                print('\nAnime Discovery — Recovery Execution\n')
                if res['backup_path']:
                    print(f'Database backup created: {res["backup_path"]}')
                if res['recovered']:
                    headers = ['Job ID', 'Kind', 'Attempt', 'History ID', 'Status']
                    rows = [[r['job_id'], r['kind'], r['attempt'], r['history_id'], r['outcome']] for r in res['recovered']]
                    print('\nSuccessfully Requeued Jobs:')
                    print(format_table(headers, rows))
                if res['failed']:
                    headers = ['Job ID', 'Kind', 'Error']
                    rows = [[f['job_id'], f['kind'], f['error']] for f in res['failed']]
                    print('\nFailed to Requeue:')
                    print(format_table(headers, rows))
                print(f'\n{res["message"]}\n')
            return 0 if not res['failed'] else 1

        else:
            print(f'Unknown action: {action}', file=sys.stderr)
            return 1

    except Exception as exc:
        if is_json:
            print(json.dumps({'error': str(exc), 'type': exc.__class__.__name__}))
        else:
            print(f'Error: {exc.__class__.__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
