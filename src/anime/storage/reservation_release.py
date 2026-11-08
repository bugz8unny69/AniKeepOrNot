"""Retire canceled acquisition slots only after proven drained cleanup."""
from src.anime.cancellation import CancellationError
from src.anime.history import job_attempt, append_job_history


def active(db,operation_id):
    single=db.execute(
        "SELECT 1 FROM episode_reservations r JOIN job_operation_members m "
        "ON m.job_id=r.owner_job_id WHERE m.operation_id=? AND r.state='ACTIVE' LIMIT 1",
        (operation_id,)).fetchone()
    batch=db.execute(
        "SELECT 1 FROM batch_episode_reservations r JOIN job_operation_members m "
        "ON m.job_id=r.owner_job_id WHERE m.operation_id=? AND r.state='ACTIVE' LIMIT 1",
        (operation_id,)).fetchone()
    return single is not None or batch is not None


def validate_releasable(db,operation_id):
    """Slots of canceled work are freed once nothing in it is still running."""
    if db.execute("SELECT 1 FROM jobs j JOIN job_operation_members m USING(job_id) WHERE m.operation_id=? AND j.state='RUNNING' LIMIT 1",(operation_id,)).fetchone():raise CancellationError()


def release_cancelled(db,operation_id,job_id,now,*,component):
    """After cleanup deleted what the work owned, free its episode slots.

    Caller holds exclusive operation drain protection and its transaction.
    """
    if not db.in_transaction:raise CancellationError()
    if db.execute('SELECT state,cleanup_state FROM job_operations WHERE operation_id=?',
                  (operation_id,)).fetchone()!=('CANCELED','COMPLETE'):raise CancellationError()
    validate_releasable(db,operation_id)
    members='SELECT job_id FROM job_operation_members WHERE operation_id=?'
    rows=db.execute(f"SELECT reservation_id FROM episode_reservations WHERE owner_job_id IN ({members}) AND state='ACTIVE'",(operation_id,)).fetchall()
    woken=()
    from src.anime.storage.episode_reservations import wake_waiters
    for (reservation,) in rows:
        db.execute("UPDATE episode_reservations SET state='RELEASED',ended_at_ms=?,ended_by_job_id=? WHERE reservation_id=? AND state='ACTIVE'",(now,job_id,reservation))
        woken+=wake_waiters(db,reservation,now,component=component)
    db.execute(f"UPDATE acquisition_plans SET state='RELEASED' WHERE job_id IN ({members}) AND state='PREPARED'",(operation_id,))
    batch_rows=db.execute(f"SELECT reservation_id FROM batch_episode_reservations WHERE owner_job_id IN ({members}) AND state='ACTIVE'",(operation_id,)).fetchall()
    db.execute(f"UPDATE batch_episode_reservations SET state='RELEASED',ended_at_ms=?,ended_by_job_id=? "
               f"WHERE owner_job_id IN ({members}) AND state='ACTIVE'",(now,job_id,operation_id))
    db.execute(f"UPDATE magnet_intents SET state='RELEASED',updated_at_ms=? WHERE job_id IN ({members}) AND state<>'RELEASED'",(now,operation_id))
    db.execute(f"UPDATE batch_metadata_admissions SET state='RELEASED',updated_at_ms=? WHERE verification_job_id IN ({members}) "
               "AND state NOT IN ('RELEASED','EXTERNAL','ADOPTED')",(now,operation_id))
    if not rows and not batch_rows:return woken
    return (append_job_history(db,job_id=job_id,event_code='EPISODE_RESERVATIONS_RELEASED',
        reason_code='CANCELED_RESERVATIONS_RELEASED',component=component,occurred_at_ms=now,details={'attempt':job_attempt(db,job_id)}),*woken)


def cleanup_completed(db,operation_id,job_id,now,*,component,previous_cleanup_state):
    """Everything that follows a cancelled operation's cleanup reaching COMPLETE.

    The one place for it, used by the cleanup service and by the immediate
    Cancel of already drained work with nothing to delete: free the episode
    slots, then (only on the first completion) let the single-episode and
    batch candidate controllers move on to their next candidate.
    """
    records=tuple(release_cancelled(db,operation_id,job_id,now,component=component))
    if previous_cleanup_state!='COMPLETE':
        from src.anime.storage.release_candidates import schedule_after_cleanup
        from src.anime.storage.batch_candidates import schedule_after_cleanup_batch
        records+=tuple(schedule_after_cleanup(db,operation_id))
        records+=tuple(schedule_after_cleanup_batch(db,operation_id))
    return records
