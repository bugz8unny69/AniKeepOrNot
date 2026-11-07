"""Resume cleanup actions incorrectly parked in review by the old age timer.

Earlier builds promoted *transient* operation-lock or client-transport waits to
CANCELLATION_REVIEW after 15 minutes. That transition must not permanently
block automatic cleanup. This reconciler requeues only those exact historical
cases, and only after it can acquire exclusive protection for the operation.
The existing cleanup handler remains solely responsible for proving torrent and
file removal and releasing reservations.
"""
import json

from src.nextgen.cancellation import CleanupBusy, OperationGate
from src.nextgen.history import append_job_history, job_attempt
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction


_TRANSIENT = frozenset(('CANCELLATION_DRAINING','DOWNLOAD_CLIENT_UNAVAILABLE'))


def _timeout_review(db, operation_id, cleanup_id, target_id):
    row=db.execute(
        "SELECT o.state,o.cleanup_state,o.reason_code,j.state,j.last_reason_code "
        "FROM job_operations o JOIN management_actions a USING(operation_id) "
        "JOIN jobs j ON j.job_id=a.job_id "
        "WHERE a.operation_id=? AND a.action='CLEANUP' "
        "AND a.job_id=? AND a.target_job_id=?",
        (operation_id,cleanup_id,target_id)
    ).fetchone()
    if row!=('CANCELED','REVIEW','CANCELLATION_REVIEW',
             'NEEDS_REVIEW','CANCELLATION_REVIEW'):
        return False
    if db.execute(
        "SELECT 1 FROM jobs j JOIN job_operation_members m USING(job_id) "
        "WHERE m.operation_id=? AND j.state='RUNNING' LIMIT 1",
        (operation_id,)
    ).fetchone():
        return False
    row=db.execute(
        "SELECT reason_code,details_json FROM job_history "
        "WHERE job_id=? AND event_code='CANCELLATION_CLEANUP_CHANGED' "
        "ORDER BY history_id DESC LIMIT 1", (target_id,)
    ).fetchone()
    if row is None or row[0]!='CANCELLATION_REVIEW':
        return False
    try:
        details=json.loads(row[1])
    except (TypeError,ValueError):
        return False
    return type(details) is dict and details.get('cause_reason_code') in _TRANSIENT


def recover_timeout_reviews(database):
    """Requeue transient old reviews when a safe operation lock is available.

    This runs with worker maintenance admission, never after forcing a lease or
    directly changing reservation state. A current handler or other process
    holding the operation lock makes the attempt a no-op.
    """
    queue=JobQueue(database)
    with queue.connection() as db:
        candidates=db.execute(
            "SELECT a.operation_id,a.job_id,a.target_job_id "
            "FROM management_actions a "
            "JOIN job_operations o USING(operation_id) "
            "JOIN jobs j ON j.job_id=a.job_id "
            "WHERE a.action='CLEANUP' AND o.state='CANCELED' "
            "AND o.cleanup_state='REVIEW' AND o.reason_code='CANCELLATION_REVIEW' "
            "AND j.state='NEEDS_REVIEW' AND j.last_reason_code='CANCELLATION_REVIEW' "
            "ORDER BY a.operation_id LIMIT 25"
        ).fetchall()
    records=[]
    for op,control,target in candidates:
        try:
            with OperationGate(database).hold(op,cleanup=True):
                with queue.connection() as db, transaction(db):
                    if not _timeout_review(db,op,control,target):
                        continue
                    now=queue.now()
                    db.execute(
                        "UPDATE job_operations SET cleanup_state='PENDING',"
                        "reason_code='CANCELLATION_DRAINING',updated_at_ms=? "
                        "WHERE operation_id=?", (now,op)
                    )
                    db.execute(
                        "UPDATE jobs SET state='PENDING',available_at_ms=?,"
                        "last_reason_code=NULL,activity_code=NULL,updated_at_ms=? "
                        "WHERE job_id=?", (now,now,control)
                    )
                    records.append(append_job_history(
                        db,job_id=control,event_code='MANAGEMENT_ACTION_RESUMED',
                        component='worker',occurred_at_ms=now,
                        details={'attempt':job_attempt(db,control)}
                    ))
        except CleanupBusy:
            # The old attempt may still exist. Never race its file operations.
            continue
    return tuple(records)
