"""Trusted management-service cancellation library. Caller holds maintenance admission."""
from src.nextgen.cancellation import CancellationError
from src.nextgen.history import job_attempt, append_job_history, integer
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction

CANCELABLE=frozenset(('PENDING','RETRY_WAIT','ON_HOLD','BLOCKED','NEEDS_REVIEW','RUNNING','FAILED'))


def operation(db,job_id):
    row=db.execute('SELECT o.operation_id,o.state,o.cleanup_state,o.reason_code FROM job_operations o '
        'JOIN job_operation_members m USING(operation_id) WHERE m.job_id=?',(job_id,)).fetchone()
    if row is None:raise CancellationError('OPERATION_NOT_FOUND')
    return row


def unfinished_resources(db,operation_id):
    return (db.execute('SELECT 1 FROM acquisition_plans p JOIN job_operation_members m ON m.job_id=p.job_id '
                       'WHERE m.operation_id=? LIMIT 1',(operation_id,)).fetchone() is not None
            or db.execute('SELECT 1 FROM sonarr_magnet_intents i JOIN job_operation_members m '
                          "ON m.job_id=i.job_id WHERE m.operation_id=? AND i.state<>'RELEASED' LIMIT 1",(operation_id,)).fetchone() is not None
            or db.execute('SELECT 1 FROM batch_metadata_admissions i JOIN job_operation_members m '
                          'ON m.job_id=i.verification_job_id WHERE m.operation_id=? '
                          "AND i.state NOT IN ('RELEASED','EXTERNAL') LIMIT 1",
                          (operation_id,)).fetchone() is not None
            or db.execute('SELECT 1 FROM staging_cleanup_artifacts '
                          'WHERE operation_id=? LIMIT 1',(operation_id,)).fetchone() is not None)


def can_cancel_operation(db,op):
    """Queue completion is not acquisition completion; STAGED closes Cancel.

    A reservation can outlive every completed job before a metadata adapter is
    installed. It keeps cancellation available without asserting that torrent
    or file cleanup is needed, or granting permission to release the slot.
    """
    if op[1]!='ACTIVE':return False
    if db.execute("SELECT 1 FROM jobs j JOIN job_operation_members m USING(job_id) "
                  "WHERE m.operation_id=? AND j.kind='CLEANUP_DECIDED_DOWNLOAD' LIMIT 1",
                  (op[0],)).fetchone():return False
    states=tuple(sorted(CANCELABLE))
    waiting=db.execute('SELECT 1 FROM jobs j JOIN job_operation_members m USING(job_id) '
                       'WHERE m.operation_id=? AND j.state IN ('+','.join('?' for _ in states)+') LIMIT 1',
                       (op[0],*states)).fetchone()
    if waiting or unfinished_resources(db,op[0]):return True
    return (db.execute('SELECT 1 FROM episode_reservations r JOIN job_operation_members m '
                      'ON m.job_id=r.owner_job_id WHERE m.operation_id=? LIMIT 1',
                      (op[0],)).fetchone() is not None or
            db.execute('SELECT 1 FROM batch_episode_reservations r JOIN job_operation_members m '
                       'ON m.job_id=r.owner_job_id WHERE m.operation_id=? LIMIT 1',
                       (op[0],)).fetchone() is not None)


def cancel_job(db,job_id,now,component='worker',*,reason_code='JOB_CANCELED'):
    row=db.execute('SELECT state,attempt_count,last_reason_code FROM jobs WHERE job_id=?',(job_id,)).fetchone()
    if row is None or row[0] not in CANCELABLE:return ()
    saved_reason = row[2] if row[0] == 'FAILED' and row[2] else reason_code
    db.execute("UPDATE jobs SET state='CANCELED',lease_token=NULL,lease_expires_at_ms=NULL,activity_code=NULL,"
               "last_reason_code=?,updated_at_ms=? WHERE job_id=?",(saved_reason,now,job_id))
    return (append_job_history(db,job_id=job_id,event_code='CANCELED',reason_code=reason_code,component=component,
                              occurred_at_ms=now,details={'attempt':row[1]}),)


def mark_staged(db,claim):
    """Future trusted staging persist callback, in the same fenced completion tx.

    Call only after verified durable publication; not a public management action.
    Partial staging/refresh failures must not call this. No file proof is invented.
    """
    if not db.in_transaction or claim.kind not in ('STAGE_TRIAL_MEDIA','STAGE_BATCH_EPISODE'):raise CancellationError('TRANSACTION_OR_KIND_INVALID')
    row=db.execute('SELECT state,lease_token,attempt_count,lease_expires_at_ms FROM jobs WHERE job_id=?',(claim.job_id,)).fetchone()
    import time
    now=time.time_ns()//1000000
    if row is None or row[:3]!=('RUNNING',claim.token,claim.attempt) or row[3]<=now:raise CancellationError('CLAIM_EXPIRED_OR_NOT_RUNNING')
    op=operation(db,claim.job_id)
    if op[1]!='ACTIVE':raise CancellationError('OPERATION_NOT_ACTIVE')
    if claim.kind=='STAGE_BATCH_EPISODE':
        plan=db.execute(
            'SELECT plan_id FROM batch_staging_publications WHERE job_id=?',
            (claim.job_id,)).fetchone()
        if plan is None:raise CancellationError('PLAN_NOT_FOUND')
        selected=db.execute(
            'SELECT count(*) FROM batch_trial_files WHERE plan_id=?',plan).fetchone()[0]
        published=db.execute(
            "SELECT count(*) FROM batch_staging_publications "
            "WHERE plan_id=? AND state='PUBLISHED'",plan).fetchone()[0]
        if selected<1 or selected!=published:raise CancellationError('PUBLICATIONS_INCOMPLETE')
    phases=db.execute('SELECT phase FROM staging_cleanup_artifacts WHERE operation_id=?',(op[0],)).fetchall()
    if not phases or any(p[0]!='PUBLISHED' for p in phases):raise CancellationError('PUBLICATIONS_NOT_PUBLISHED')
    if db.execute("SELECT 1 FROM jobs j JOIN job_operation_members m USING(job_id) WHERE m.operation_id=? AND j.job_id<>? AND j.state<>'DONE' LIMIT 1",
                  (op[0],claim.job_id)).fetchone():raise CancellationError('JOB_NOT_DONE')
    db.execute("UPDATE job_operations SET state='STAGED',updated_at_ms=? WHERE operation_id=?",(now,op[0]))
    # Every selected episode is published: the active batch candidate and
    # its season succeeded (WI-13). Nothing else counts as success.
    from src.nextgen.storage.batch_candidates import succeed_operation_candidate
    succeed_operation_candidate(db,op[0],now_ms=now)


def request_cancellation(db,job_id,now,*,component='api',reason_code='JOB_CANCELED'):
    """Cancel via the normal guarded operation lifecycle inside the caller transaction."""
    if not db.in_transaction:raise CancellationError('TRANSACTION_REQUIRED')
    if db.execute('SELECT 1 FROM management_actions WHERE job_id=?',(job_id,)).fetchone():raise CancellationError('MANAGEMENT_ACTION_EXISTS')
    op=operation(db,job_id)
    if op[1] in ('CANCEL_REQUESTED','CANCELED'):return op[0],()
    members=db.execute('SELECT j.job_id,j.state,j.attempt_count FROM jobs j JOIN job_operation_members m USING(job_id) '
                       'WHERE m.operation_id=?',(op[0],)).fetchall()
    if not can_cancel_operation(db,op):raise CancellationError('OPERATION_NOT_CANCELABLE')
    db.execute("UPDATE job_operations SET state='CANCEL_REQUESTED',cleanup_state='PENDING',reason_code='CANCELLATION_DRAINING',"
               'updated_at_ms=? WHERE operation_id=?',(now,op[0]))
    # A batch candidate stopped by a person is CANCELED, not FAILED. An
    # automatic failure records FAILED before it reaches here; first wins.
    from src.nextgen.storage.batch_candidates import record_candidate_stop
    record_candidate_stop(db,op[0],'CANCELED',reason_code,now_ms=now)
    receipts=[]
    for member,state,attempt in members:
        if state not in CANCELABLE:continue
        receipts.append(append_job_history(db,job_id=member,event_code='CANCELLATION_REQUESTED',reason_code=reason_code,
            component=component,occurred_at_ms=now,details={'attempt':attempt}))
        if state!='RUNNING':receipts.extend(cancel_job(db,member,now,component,reason_code=reason_code))
    if not receipts:
        receipts.append(append_job_history(db,job_id=job_id,event_code='CANCELLATION_REQUESTED',reason_code=reason_code,
            component=component,occurred_at_ms=now,details={'attempt':job_attempt(db,job_id)}))
    return op[0],tuple(receipts)


class CancellationStore:
    def __init__(self,database,*,clock=None):self.queue=JobQueue(database,clock=clock)
    def status(self,job_id):
        integer(job_id,positive=True)
        with self.queue.connection() as db,transaction(db):
            op=operation(db,job_id)
            from src.nextgen.storage.reservation_release import active
            return {'operation_id':op[0],'state':op[1],'cleanup_state':op[2],'reason_code':op[3],
                    'can_cancel':not db.execute('SELECT 1 FROM management_actions WHERE job_id=?',(job_id,)).fetchone() and can_cancel_operation(db,op),
                    'can_cleanup':op[1] in ('CANCEL_REQUESTED','CANCELED') and (op[2]!='COMPLETE' or active(db,op[0]))}
    def request(self,job_id):
        integer(job_id,positive=True)
        with self.queue.connection() as db,transaction(db):
            return self.request_in_transaction(db,job_id)
    def request_in_transaction(self,db,job_id):
        """Caller transaction allows cancellation + cleanup enqueue to be atomic."""
        return request_cancellation(db,job_id,self.queue.now())

    def acknowledge(self,claim):
        with self.queue.connection() as db,transaction(db):
            op=operation(db,claim.job_id)
            if op[1] not in ('CANCEL_REQUESTED','CANCELED'):raise CancellationError('OPERATION_NOT_CANCEL_REQUESTED')
            row=db.execute('SELECT state,lease_token,attempt_count FROM jobs WHERE job_id=?',(claim.job_id,)).fetchone()
            if row==('RUNNING',claim.token,claim.attempt):return cancel_job(db,claim.job_id,self.queue.now())
            return ()
