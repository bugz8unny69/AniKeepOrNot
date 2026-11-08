"""Reject one verified AniDB series, clean its staged episodes and owned torrents.

A single durable series decision fences future intake. The worker reuses the
existing staging receipt-based deletion and owned torrent cleanup jobs.
"""
from types import SimpleNamespace

from src.anime.async_io import blocking
from src.anime.cancellation import CancellationError
from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import CompletionRejected, Outcome, Registration
from src.anime.staging import StagingBusy, StagingReview
from src.anime.sonarr import KeepReview
from src.anime.storage.batch_keep_imports import batch_publications
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.review_decisions import staged, remove_series
from src.anime.storage.sqlite import enqueue_job, transaction
from src.anime.storage.staging import Journal as StageJournal

KIND = 'REJECT_SERIES'
DEFINITION = HandlerDefinition(KIND, 'Reject series and clean all staged acquisitions')


def _targets(db, queue, claim, aid, operations):
    queue._owns(db, claim, queue.now())
    decision = db.execute(
        "SELECT cleanup_state FROM anime_review_decisions "
        "WHERE anidb_id=? AND job_id=? AND decision='REJECT'",
        (aid, claim.job_id)).fetchone()
    if decision is None:
        raise StagingReview()
    sources = []
    for op in operations:
        state = db.execute(
            'SELECT state FROM job_operations WHERE operation_id=?', (op,)).fetchone()
        if state != ('STAGED',):
            raise StagingReview()
        if db.execute(
            "SELECT 1 FROM management_actions WHERE operation_id=? AND action='KEEP'",
            (op,)).fetchone():
            raise StagingReview()
        batches = db.execute(
            'SELECT p.job_id FROM batch_staging_publications p '
            'JOIN job_operation_members m ON m.job_id=p.job_id '
            "WHERE m.operation_id=? AND p.state='PUBLISHED' LIMIT 1",
            (op,)).fetchone()
        if batches:
            rows = batch_publications(db, op)
            if any(row[4] != aid for row in rows):
                raise StagingReview()
            sources.append((op, 'batch', rows[0][0],
                            tuple((row[2], row[3]) for row in rows),
                            None))
        else:
            row = staged(db, op)
            if row[0] != aid:
                raise StagingReview()
            stage = db.execute(
                'SELECT s.job_id,j.event_id FROM staging_publications s '
                'JOIN jobs j ON j.job_id=s.job_id '
                'JOIN job_operation_members m ON m.job_id=s.job_id '
                "WHERE m.operation_id=? AND s.state='PUBLISHED'",
                (op,)).fetchone()
            if stage is None:
                raise StagingReview()
            sources.append((op, 'single', row[1],
                            ((row[2], row[3]),), stage))
    return decision[0], tuple(sources)


def _enqueue_cleanup(db, queue, claim, sources):
    """Queue only existing ownership-proven cleanup mechanisms."""
    from src.anime.decision_download_cleanup import (
        enqueue_after_staging, staging_status,
    )
    statuses = []
    for op, kind, plan, _, stage in sources:
        if kind == 'single':
            stage_job, event = stage
            existing = staging_status(db, stage_job)
            if not existing:
                enqueue_after_staging(
                    db, SimpleNamespace(job_id=stage_job, event_id=event))
                existing = staging_status(db, stage_job)
            if len(existing) != 1:
                raise StagingReview()
            cleanup_job = existing[0][0]
        else:
            from src.anime.jobs.cleanup_batch_download_job import staged_cleanup
            rows = db.execute(
                "SELECT job_id FROM jobs WHERE kind='CLEANUP_BATCH_DOWNLOAD' "
                "AND json_extract(payload_json,'$.decision_job_id')=? "
                "AND json_extract(payload_json,'$.plan_id')=?",
                (claim.job_id, plan)).fetchall()
            if len(rows) > 1:
                raise StagingReview()
            staging = staged_cleanup(db, plan)
            if staging is not None and not rows:
                cleanup_job = staging
            elif rows:
                cleanup_job = rows[0][0]
            else:
                cleanup_job = enqueue_job(
                    db, kind='CLEANUP_BATCH_DOWNLOAD', component='worker',
                    event_id=claim.event_id, created_by_job_id=claim.job_id,
                    payload={'decision_job_id': claim.job_id, 'plan_id': plan})
        job = db.execute('SELECT state FROM jobs WHERE job_id=?',
                         (cleanup_job,)).fetchone()
        if job is None:
            raise StagingReview()
        statuses.append(job[0])
    # The admission path also canceled any live episode reservations. Wait for
    # their normal cleanup rather than claiming the series fully cleaned early.
    pending = db.execute(
        "SELECT j.state FROM management_actions a "
        "JOIN jobs j ON j.job_id=a.job_id "
        "JOIN job_operation_members m ON m.operation_id=a.operation_id "
        "JOIN episode_reservations r ON r.owner_job_id=m.job_id "
        "WHERE r.anidb_id=(SELECT anidb_id FROM anime_review_decisions WHERE job_id=?) "
        "AND a.action='CLEANUP' "
        "UNION "
        "SELECT j.state FROM management_actions a "
        "JOIN jobs j ON j.job_id=a.job_id "
        "JOIN job_operation_members m ON m.operation_id=a.operation_id "
        "JOIN batch_episode_reservations r ON r.owner_job_id=m.job_id "
        "WHERE r.anidb_id=(SELECT anidb_id FROM anime_review_decisions WHERE job_id=?) "
        "AND a.action='CLEANUP'",
        (claim.job_id, claim.job_id)).fetchall()
    statuses.extend(row[0] for row in pending)
    return statuses


def _run(context, aid, operations, remove):
    queue = JobQueue(context.configuration.value('storage.database_path'))
    with StageJournal(queue.database).lock(context.claim):
        with queue.connection() as db, transaction(db):
            state, sources = _targets(db, queue, context.claim, aid, operations)
        if state == 'COMPLETE':
            return 'complete'
        if context.cancelled.is_set():
            raise CancellationError()

        def guard():
            if context.cancelled.is_set():
                raise CancellationError()
            with queue.connection() as db, transaction(db):
                phase, checked = _targets(db, queue, context.claim, aid, operations)
                if phase != 'PENDING' or checked != sources:
                    raise StagingReview()

        # One call removes every staged episode plus the then-empty series
        # folders. Idempotent and never recursive (see remove_series).
        remove(tuple(pub for *_, pubs, _ in sources for pub in pubs), guard,
               context.configuration.value('storage.keep_root'))

        with queue.connection() as db, transaction(db):
            phase, checked = _targets(db, queue, context.claim, aid, operations)
            if phase != 'PENDING' or checked != sources:
                raise StagingReview()
            statuses = _enqueue_cleanup(db, queue, context.claim, sources)
            if any(status in ('NEEDS_REVIEW', 'BLOCKED', 'CANCELED', 'ON_HOLD')
                   for status in statuses):
                return 'review'
            if any(status != 'DONE' for status in statuses):
                return 'waiting'
            if db.execute(
                "UPDATE anime_review_decisions SET cleanup_state='COMPLETE' "
                "WHERE anidb_id=? AND job_id=? AND cleanup_state='PENDING'",
                (aid, context.claim.job_id)).rowcount != 1:
                raise StagingReview()
        return 'complete'


def _persist(db, claim, value):
    if value != 'complete' or db.execute(
        "SELECT cleanup_state FROM anime_review_decisions "
        "WHERE job_id=? AND decision='REJECT'", (claim.job_id,)).fetchone() != ('COMPLETE',):
        raise CompletionRejected(Outcome('review', 'REJECT_CLEANUP_REVIEW'))


def registration(*, remove=remove_series):
    async def execute(context):
        try:
            data = context.data()
            if set(data) != {'anidb_id', 'operation_ids'}:
                raise ValueError()
            aid, operations = data['anidb_id'], data['operation_ids']
            if (type(aid) is not int or aid < 1 or type(operations) is not list
                    or len(operations) > 100 or
                    any(type(op) is not int or op < 1 for op in operations)
                    or operations != sorted(set(operations))):
                raise ValueError()
        except (ValueError, TypeError):
            return Outcome('review', 'JOB_PAYLOAD_INVALID')
        try:
            result = await blocking(_run, context, aid, tuple(operations), remove)
        except StagingBusy:
            return Outcome('defer', 'JOB_WAITING', delay_ms=1000)
        except (StagingReview, KeepReview, CancellationError, ValueError, TypeError, KeyError):
            return Outcome('review', 'REJECT_CLEANUP_REVIEW')
        except OSError:
            return Outcome('retry', 'STAGING_UNAVAILABLE')
        if result == 'review':
            return Outcome('review', 'REJECT_CLEANUP_REVIEW')
        if result == 'waiting':
            return Outcome('defer', 'DECISION_DOWNLOAD_CLEANUP_WAITING', delay_ms=30000)
        return Outcome(value=result)
    return Registration(KIND, DEFINITION, execute, persist=_persist)
