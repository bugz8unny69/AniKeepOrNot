"""Manual retry of one stopped acquisition step, using existing worker/journals.

No external effects, worker invocation, new job or database migration occurs here.
The handler remains responsible for safely reconciling its own prior side effects.
"""
from dataclasses import dataclass
import json

from src.nextgen.history import append_job_history, integer
from src.nextgen.messages import MessageCatalog, safe_code
from src.nextgen.reasons import get_reason
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction

# Deliberate allowlist: independent acquisition steps, not management decisions,
# cleanup jobs, or independent post-Keep follow-ups with their own resume path.
RETRY_KINDS = frozenset((
    'IDENTIFY_ANIME', 'VERIFY_RELEASE', 'EVALUATE_TRIAL', 'RESERVE_EPISODE', 'DISCOVER_SONARR_BACKFILL',
    'PREPARE_ACQUISITION', 'SUBMIT_TORRENT', 'DOWNLOAD_TORRENT',
    'VERIFY_MEDIA', 'STAGE_TRIAL_MEDIA', 'EVALUATE_BATCH', 'RESERVE_BATCH',
    'SUBMIT_BATCH', 'TRANSFER_BATCH', 'VERIFY_BATCH_PROBE', 'UNLOCK_BATCH',
    'VERIFY_BATCH_EPISODE', 'STAGE_BATCH_EPISODE',
))
CATALOG = MessageCatalog()


class RetryNotAllowed(ValueError):
    reason_code = 'MANAGEMENT_ACTION_UNAVAILABLE'


class RetryStale(ValueError):
    reason_code = 'MANAGEMENT_ACTION_UNAVAILABLE'


@dataclass(frozen=True)
class RetryReceipt:
    job_id: int
    attempt: int
    outcome: str
    history_id: int


def _retryable_state(kind, state, reason):
    return state in ('NEEDS_REVIEW', 'FAILED') or (
        # Waiting for dub: Retry is "Search again".
        kind == 'DISCOVER_SONARR_BACKFILL' and state == 'ON_HOLD'
        and reason == 'WAITING_FOR_DUB') or (
        kind == 'DISCOVER_SONARR_BACKFILL' and state == 'BLOCKED'
        and reason in ('PROWLARR_SEARCH_TIMEOUT', 'PROWLARR_UNAVAILABLE',
                       # Jobs stopped before the Prowlarr search existed.
                       'SONARR_DISCOVERY_EPISODE_NOT_FOUND', 'SONARR_EPISODES_NOT_LISTED',
                       'SONARR_DISCOVERY_TIMEOUT', 'SONARR_DISCOVERY_UNAVAILABLE'))


def _allowed(db, job_id, row):
    kind, state, attempt, reason, token, expiry = row
    if (kind not in RETRY_KINDS or not _retryable_state(kind, state, reason) or attempt <= 0
            or get_reason(reason) is None or token is not None or expiry is not None):
        return False
    if db.execute('SELECT 1 FROM management_actions WHERE job_id=?', (job_id,)).fetchone():
        return False
    # A completed handoff must never enqueue a second successor.
    if db.execute('SELECT 1 FROM jobs WHERE created_by_job_id=? LIMIT 1', (job_id,)).fetchone():
        return False
    operation = db.execute(
        'SELECT o.state FROM job_operations o JOIN job_operation_members m '
        'USING(operation_id) WHERE m.job_id=?', (job_id,)).fetchone()
    return operation is not None and operation[0] == 'ACTIVE'


def _cleanup_target(db, job_id):
    """Target job of a Clean up canceled work control job, else None."""
    row = db.execute("SELECT target_job_id FROM management_actions "
                     "WHERE job_id=? AND action='CLEANUP'", (job_id,)).fetchone()
    return None if row is None else row[0]


def _disabled_reason(db, job_id, row):
    if row is None:
        return CATALOG.reason('MANAGEMENT_ACTION_UNAVAILABLE')
    kind, state, attempt, reason, token, expiry = row
    if state == 'RUNNING' or token is not None or expiry is not None:
        return CATALOG.reason('MANAGEMENT_JOB_RUNNING')
    if state == 'PENDING':
        return CATALOG.reason('MANAGEMENT_JOB_QUEUED')
    if state == 'DONE':
        return CATALOG.reason('MANAGEMENT_JOB_COMPLETE')
    if state == 'CANCELED':
        return CATALOG.reason('MANAGEMENT_JOB_CANCELED')
    if db.execute('SELECT 1 FROM management_actions WHERE job_id=?', (job_id,)).fetchone():
        return CATALOG.reason('MANAGEMENT_JOB_RUNNING')
    if db.execute('SELECT 1 FROM jobs WHERE created_by_job_id=? LIMIT 1', (job_id,)).fetchone():
        return CATALOG.reason('MANAGEMENT_JOB_COMPLETE')
    op = db.execute(
        'SELECT o.state, o.cleanup_state FROM job_operations o JOIN job_operation_members m '
        'USING(operation_id) WHERE m.job_id=?', (job_id,)).fetchone()
    if op is not None and op[0] in ('CANCEL_REQUESTED', 'CANCELED'):
        return CATALOG.reason('CANCELLATION_DRAINING' if op[1] == 'RUNNING' else 'MANAGEMENT_ACTION_UNAVAILABLE')
    if state == 'ON_HOLD' and reason == 'EPISODE_RESERVED':
        return CATALOG.reason('MANAGEMENT_JOB_WAITING')
    if state == 'RETRY_WAIT':
        return CATALOG.reason('MANAGEMENT_JOB_WAITING')
    return CATALOG.reason('MANAGEMENT_ACTION_UNAVAILABLE')


def capability(db, job_id):
    target = _cleanup_target(db, job_id)
    if target is not None:
        # Retry on a stopped cleanup job = the target's existing Cleanup resume.
        from src.nextgen.storage.management_actions import ManagementActions
        state = db.execute('SELECT state FROM jobs WHERE job_id=?', (job_id,)).fetchone()[0]
        cleanup = ManagementActions(db.execute('PRAGMA database_list').fetchone()[2]).capabilities(db, target)['cleanup']
        enabled = state in ('NEEDS_REVIEW', 'BLOCKED') and cleanup['enabled']
        return {'enabled': enabled, 'disabled_reason': None if enabled else
                (cleanup['disabled_reason'] or CATALOG.reason('MANAGEMENT_ACTION_UNAVAILABLE'))}
    row = db.execute(
        'SELECT kind,state,attempt_count,last_reason_code,lease_token,lease_expires_at_ms '
        'FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    enabled = row is not None and _allowed(db, job_id, row)
    return {'enabled': enabled, 'disabled_reason': None if enabled else
            _disabled_reason(db, job_id, row)}


def request_review_retry(database, job_id, *, expected_attempt, expected_reason, clock=None, delete_cache=None):
    """Requeue the *same* reviewed job with snapshot CAS; preserve evidence/history.

    A duplicate request still pending is acknowledged, never scheduled twice.
    No handler or external service runs inside this transaction.
    """
    job_id = integer(job_id, positive=True)
    expected_attempt = integer(expected_attempt, positive=True)
    if (type(expected_reason) is not str or
            safe_code(expected_reason) != expected_reason):
        raise ValueError('expected review reason required')
    if delete_cache is not None and type(delete_cache) is not bool:
        raise RetryNotAllowed()
    queue = JobQueue(database, clock=clock)
    with queue.connection() as db:
        target = _cleanup_target(db, job_id)
        row = db.execute('SELECT state,attempt_count,last_reason_code FROM jobs WHERE job_id=?',
                         (job_id,)).fetchone()
    if target is not None:
        if row[1] != expected_attempt:
            raise RetryStale('job attempt changed')
        if row[0] not in ('NEEDS_REVIEW', 'BLOCKED', 'FAILED'):
            raise RetryNotAllowed('job is not stopped for review')
        if row[2] != expected_reason:
            raise RetryStale('job review reason changed')
        from src.nextgen.storage.management_actions import ManagementActions, ActionUnavailable
        try:
            _, data, records = ManagementActions(database).submit(target, 'cleanup')
        except ActionUnavailable:
            raise RetryNotAllowed('this cleanup cannot be resumed') from None
        if not data.get('resumed'):
            raise RetryNotAllowed('this cleanup cannot be resumed')
        return RetryReceipt(job_id, row[1], 'QUEUED', records[-1].history_id)
    with queue.connection() as db, transaction(db):
        now = queue.now()
        row = db.execute(
            'SELECT kind,state,attempt_count,last_reason_code,lease_token,lease_expires_at_ms '
            'FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise RetryNotAllowed('job does not exist')
        kind, state, attempt, reason, token, expiry = row
        payload = None
        if delete_cache is not None:
            if (kind != 'DISCOVER_SONARR_BACKFILL'
                    or state not in ('ON_HOLD', 'PENDING')
                    or state == 'ON_HOLD' and reason != 'WAITING_FOR_DUB'):
                raise RetryNotAllowed()
            payload = json.loads(db.execute(
                'SELECT payload_json FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0])
            if type(payload.get('exhausted_episode')) is not int:
                raise RetryNotAllowed()
            payload['delete_cache'] = delete_cache
        if attempt != expected_attempt:
            raise RetryStale('job attempt changed')
        if (state == 'PENDING' and kind in RETRY_KINDS and token is None
                and expiry is None and db.execute(
                    'SELECT 1 FROM job_operation_members m JOIN job_operations o '
                    "USING(operation_id) WHERE m.job_id=? AND o.state='ACTIVE'",
                    (job_id,)).fetchone()):
            previous = db.execute(
                "SELECT history_id,details_json FROM job_history WHERE job_id=? "
                "AND event_code='RETRY_REQUESTED' ORDER BY history_id DESC LIMIT 1",
                (job_id,)).fetchone()
            if previous is not None:
                details = json.loads(previous[1])
                if (details.get('attempt') == attempt and details.get('cause_reason_code') == expected_reason
                        and (delete_cache is None or details.get('delete_cache') is delete_cache)):
                    return RetryReceipt(job_id, attempt, 'ALREADY_QUEUED', previous[0])
        if not _retryable_state(kind, state, reason):
            raise RetryNotAllowed('job is not stopped for review')
        if reason != expected_reason:
            raise RetryStale('job review reason changed')
        if not _allowed(db, job_id, row):
            raise RetryNotAllowed('this job cannot be resumed')
        updated = db.execute(
            "UPDATE jobs SET state='PENDING',available_at_ms=?,updated_at_ms=?,"
            "last_reason_code=NULL,activity_code=NULL,"
            "payload_json=COALESCE(?,payload_json) WHERE job_id=? "
            "AND state=? AND attempt_count=? AND last_reason_code=? "
            "AND lease_token IS NULL AND lease_expires_at_ms IS NULL",
            (now, now, json.dumps(payload,sort_keys=True,separators=(',',':')) if payload else None,
             job_id, state, attempt, reason))
        if updated.rowcount != 1:
            raise RetryStale('job changed while requesting retry')
        facts = {'attempt': attempt, 'cause_reason_code': reason}
        if delete_cache is not None:
            from src.nextgen.storage.sonarr_cached_retry import clear_cached, expire_cached
            expire_cached(db, now)
            facts['delete_cache'] = delete_cache
            if delete_cache:
                facts['cache_entries_deleted'] = clear_cached(
                    db, payload['plan_id'], payload['exhausted_episode'])
        audit = append_job_history(db, job_id=job_id, event_code='RETRY_REQUESTED',
            component='api', occurred_at_ms=now, details=facts)
        return RetryReceipt(job_id, attempt, 'QUEUED', audit.history_id)
