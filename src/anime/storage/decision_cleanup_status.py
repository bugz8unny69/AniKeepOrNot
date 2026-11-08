"""Shared management cleanup status for one episode or a verified batch.

Keep, Reject, and Retry have one operator-facing contract. The underlying
worker owns the single-file or multi-file proof journal; the dashboard and
management actions must not fork into separate recovery algorithms.
"""
from src.anime.cancellation import CancellationError
from src.anime.messages import DEFAULT_CATALOG

STOPPED_JOBS = ('NEEDS_REVIEW', 'BLOCKED')


def _batch_status(db, decision, *, kept):
    if kept:
        rows = db.execute(
            "SELECT job_id,state,last_reason_code FROM jobs "
            "WHERE kind='CLEANUP_BATCH_KEPT_MEDIA' AND created_by_job_id=? "
            "ORDER BY job_id", (decision,)).fetchall()
    else:
        rows = db.execute(
            "SELECT job_id,state,last_reason_code FROM jobs "
            "WHERE kind='CLEANUP_BATCH_DOWNLOAD' "
            "AND json_extract(payload_json,'$.decision_job_id')=? "
            "ORDER BY job_id", (decision,)).fetchall()
    if not rows and not kept:
        rows = _staging_rows(db, decision)
    if not rows:
        return None
    if len(rows) != 1:
        raise CancellationError()
    job, state, reason = rows[0]
    if kept:
        phase = db.execute(
            'SELECT phase FROM batch_keep_cleanup '
            'WHERE job_id=? AND decision_job_id=?', (job, decision)).fetchone()
        completed_reason = 'POST_KEEP_CLEANUP_COMPLETE'
    else:
        phase = db.execute(
            'SELECT c.phase FROM batch_download_cleanup c JOIN jobs j '
            "ON j.job_id=? AND c.decision_job_id=json_extract(j.payload_json,'$.decision_job_id')",
            (job,)).fetchone()
        completed_reason = 'DECISION_DOWNLOAD_CLEANUP_COMPLETE'
    completed = state == 'DONE' and phase == ('COMPLETE',)
    return {
        'job_id': job, 'state': state,
        'status': DEFAULT_CATALOG.job_status(state),
        'completed': completed,
        'completion': DEFAULT_CATALOG.reason(completed_reason) if completed else None,
        'reason': None if reason is None else DEFAULT_CATALOG.reason(reason),
        'status_url': f'/api/v1/jobs/{job}',
    }


def _staging_rows(db, decision):
    from src.anime.jobs.cleanup_batch_download_job import staged_cleanup
    plan = db.execute(
        'SELECT DISTINCT p.plan_id FROM management_actions a '
        'JOIN job_operation_members m ON m.operation_id=a.operation_id '
        'JOIN batch_staging_publications p ON p.job_id=m.job_id WHERE a.job_id=?',
        (decision,)).fetchall()
    if len(plan) != 1:
        return []
    job = staged_cleanup(db, plan[0][0])
    if job is None:
        return []
    return db.execute('SELECT job_id,state,last_reason_code FROM jobs WHERE job_id=?',
                      (job,)).fetchall()


def kept_status(db, decision):
    kind = db.execute(
        'SELECT kind FROM jobs WHERE job_id=?', (decision,)).fetchone()
    if kind == ('KEEP_BATCH',):
        return _batch_status(db, decision, kept=True)
    from src.anime.post_keep_cleanup import status
    return status(db, decision)


def download_status(db, decision):
    kind = db.execute(
        'SELECT kind FROM jobs WHERE job_id=?', (decision,)).fetchone()
    if kind==('KEEP_BATCH',):
        return _batch_status(db, decision, kept=False)
    from src.anime.decision_download_cleanup import status
    return status(db, decision)
