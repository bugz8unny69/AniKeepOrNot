"""Explicit management actions, never arbitrary HTTP-supplied job kinds.

Caller holds maintenance admission. Each action has a separate control scope;
its target scope may be canceled/staged without canceling the control job.
"""
from contextlib import ExitStack
from src.nextgen.cancellation import CleanupBusy, OperationGate, CancellationError
from src.nextgen.history import job_attempt, append_job_history, integer
from src.nextgen.messages import MessageCatalog
from src.nextgen.storage.cancellation import CancellationStore, operation, unfinished_resources, can_cancel_operation
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import enqueue_job_with_receipt, transaction
from src.nextgen.storage.decision_cleanup_status import kept_status as cleanup_status, download_status, STOPPED_JOBS

KINDS={'KEEP':'KEEP_MEDIA','CLEANUP':'CLEANUP_CANCELED_OPERATION'}
CATALOG=MessageCatalog()


class ActionUnavailable(ValueError):
    reason_code='MANAGEMENT_ACTION_UNAVAILABLE'
    def __init__(self):super().__init__('This action is not available for this work.')


class ActionNotFound(ActionUnavailable):
    reason_code='API_ROUTE_NOT_FOUND'


def is_batch_operation(db,operation_id):
    return db.execute(
        "SELECT 1 FROM batch_staging_publications p "
        "JOIN job_operation_members m ON m.job_id=p.job_id "
        "WHERE m.operation_id=? LIMIT 1",(operation_id,)).fetchone() is not None


def target(db,job_id):
    integer(job_id,positive=True)
    if not db.execute('SELECT 1 FROM jobs WHERE job_id=?',(job_id,)).fetchone():raise ActionNotFound()
    if db.execute('SELECT 1 FROM management_actions WHERE job_id=?',(job_id,)).fetchone():raise ActionUnavailable()
    return operation(db,job_id)


def disabled_reason(state,reason_code=None):
    # Presentation only: never changes scheduling or authorizes an action.
    code={'PENDING':'MANAGEMENT_JOB_QUEUED','RUNNING':'MANAGEMENT_JOB_RUNNING',
          'RETRY_WAIT':'MANAGEMENT_JOB_WAITING','DONE':'MANAGEMENT_JOB_COMPLETE',
          'CANCELED':'MANAGEMENT_JOB_CANCELED'}.get(state)
    if code is None:
        code=reason_code or ('MANAGEMENT_JOB_ATTENTION' if state in
                            ('ON_HOLD','BLOCKED','NEEDS_REVIEW') else 'MANAGEMENT_ACTION_UNAVAILABLE')
    return CATALOG.reason(code)


def receipt(db,job_id,*,reused=False):
    row=db.execute('SELECT state,activity_code,last_reason_code FROM jobs WHERE job_id=?',(job_id,)).fetchone()
    data={'job_id':job_id,'state':row[0],'status':CATALOG.job_status(row[0],row[1]),
            'reason':None if row[2] is None else CATALOG.reason(row[2]),
            'status_url':f'/api/v1/jobs/{job_id}','history_url':f'/api/v1/jobs/{job_id}/history',
            'notification':CATALOG.reason('MANAGEMENT_JOB_EXISTS' if reused else 'MANAGEMENT_JOB_SUBMITTED'),
            'reused':reused,'disable_action':True,
            'disabled_reason':disabled_reason(row[0],row[2])}
    if db.execute("SELECT 1 FROM management_actions WHERE job_id=? AND action='KEEP'",(job_id,)).fetchone():
        data['post_keep_cleanup']=cleanup_status(db,job_id)
        from src.nextgen.jobs.enrich_shoko_job import status as shoko_status
        data['optional_shoko']=shoko_status(db,job_id)
        data['execution_available']=True
        data['availability_reason']=None
        single_complete=db.execute(
            "SELECT 1 FROM keep_imports WHERE job_id=? AND phase='COMPLETE'",
            (job_id,)).fetchone() is not None
        batch_rows=db.execute(
            'SELECT phase FROM batch_keep_imports WHERE job_id=?',
            (job_id,)).fetchall()
        batch_complete=bool(batch_rows) and all(r[0]=='COMPLETE' for r in batch_rows)
        data['completion']=CATALOG.reason('KEEP_COMPLETE') if (
            single_complete or batch_complete) else None
    data['download_cleanup']=download_status(db,job_id)
    return data


class ManagementActions:
    def __init__(self,database):
        self.queue=JobQueue(database)
        self.cancellations=CancellationStore(database)

    def submit(self,job_id,action):
        if action not in ('keep','cancel','cleanup'):raise ActionUnavailable()
        records=[]
        with ExitStack() as locks,self.queue.connection() as db,transaction(db):
            op=target(db,job_id)
            selected='KEEP' if action=='keep' else 'CLEANUP'
            prior=db.execute('SELECT job_id FROM management_actions WHERE operation_id=? AND action=?',
                             (op[0],selected)).fetchone()
            if prior:
                state=db.execute('SELECT state FROM jobs WHERE job_id=?',(prior[0],)).fetchone()[0]
                if action=='cleanup' and state=='DONE' and op[1:3]==('CANCELED','COMPLETE'):
                    from src.nextgen.storage.reservation_release import active
                    if active(db,op[0]):
                        now=self.queue.now()
                        db.execute("UPDATE jobs SET state='PENDING',available_at_ms=?,last_reason_code=NULL,updated_at_ms=? WHERE job_id=?",(now,now,prior[0]))
                        records.append(append_job_history(db,job_id=prior[0],event_code='MANAGEMENT_ACTION_RESUMED',component='api',occurred_at_ms=now,details={'attempt':job_attempt(db,prior[0])}))
                        data=receipt(db,prior[0]);data['resumed']=True
                        return 202,data,tuple(records)
                if action=='keep' and state=='DONE':
                    download=download_status(db,prior[0])
                    if download and download['state'] in STOPPED_JOBS:
                        now=self.queue.now()
                        db.execute("UPDATE jobs SET state='PENDING',available_at_ms=?,last_reason_code=NULL,activity_code=NULL,updated_at_ms=? WHERE job_id=?",(now,now,download['job_id']))
                        records.append(append_job_history(db,job_id=download['job_id'],event_code='MANAGEMENT_ACTION_RESUMED',component='api',occurred_at_ms=now,details={'attempt':job_attempt(db,download['job_id'])}))
                        data=receipt(db,prior[0],reused=True);data['download_cleanup_resumed']=True
                        return 202,data,tuple(records)
                if action=='keep' and state=='DONE':
                    cleanup=cleanup_status(db,prior[0])
                    if cleanup and cleanup['state'] in ('NEEDS_REVIEW','BLOCKED'):
                        now=self.queue.now()
                        db.execute("UPDATE jobs SET state='PENDING',available_at_ms=?,last_reason_code=NULL,updated_at_ms=? WHERE job_id=?",(now,now,cleanup['job_id']))
                        records.append(append_job_history(db,job_id=cleanup['job_id'],event_code='MANAGEMENT_ACTION_RESUMED',component='api',occurred_at_ms=now,details={'attempt':job_attempt(db,cleanup['job_id'])}))
                        data=receipt(db,prior[0],reused=True);data['cleanup_resumed']=True
                        return 202,data,tuple(records)
                if action=='keep' and state=='DONE':
                    from src.nextgen.jobs.enrich_shoko_job import status as shoko_status
                    shoko=shoko_status(db,prior[0])
                    if shoko and shoko['state'] in ('NEEDS_REVIEW','BLOCKED'):
                        now=self.queue.now()
                        db.execute("UPDATE jobs SET state='PENDING',available_at_ms=?,last_reason_code=NULL,activity_code=NULL,updated_at_ms=? WHERE job_id=?",(now,now,shoko['job_id']))
                        records.append(append_job_history(db,job_id=shoko['job_id'],event_code='MANAGEMENT_ACTION_RESUMED',component='api',occurred_at_ms=now,details={'attempt':job_attempt(db,shoko['job_id'])}))
                        data=receipt(db,prior[0],reused=True);data['shoko_resumed']=True
                        return 202,data,tuple(records)
                if action in ('cleanup','keep') and state in ('NEEDS_REVIEW','BLOCKED'):
                    now=self.queue.now()
                    db.execute("UPDATE jobs SET state='PENDING',available_at_ms=?,last_reason_code=NULL,activity_code=NULL,updated_at_ms=? WHERE job_id=?",(now,now,prior[0]))
                    records.append(append_job_history(db,job_id=prior[0],event_code='MANAGEMENT_ACTION_RESUMED',component='api',occurred_at_ms=now,details={'attempt':job_attempt(db,prior[0])}))
                    data=receipt(db,prior[0]);data['resumed']=True
                    return 202,data,tuple(records)
                return 202,receipt(db,prior[0],reused=True),()
            now=self.queue.now()
            if action=='keep':
                if op[1]!='STAGED':raise ActionUnavailable()
                if db.execute('SELECT 1 FROM anime_review_decisions d JOIN episode_reservations r ON r.anidb_id=d.anidb_id JOIN job_operation_members m ON m.job_id=r.owner_job_id WHERE m.operation_id=?',(op[0],)).fetchone():raise ActionUnavailable()
                if db.execute('SELECT 1 FROM anime_review_decisions d JOIN batch_episode_reservations r ON r.anidb_id=d.anidb_id JOIN job_operation_members m ON m.job_id=r.owner_job_id WHERE m.operation_id=?',(op[0],)).fetchone():raise ActionUnavailable()
            else:
                if action=='cancel':
                    try:_,audits=self.cancellations.request_in_transaction(db,job_id)
                    except CancellationError:raise ActionUnavailable() from None
                    records.extend(audits)
                    op=operation(db,job_id)
                    now=self.queue.now()
                if op[1] not in ('CANCEL_REQUESTED','CANCELED'):raise ActionUnavailable()
                from src.nextgen.storage.reservation_release import active,validate_releasable
                if op[2]=='COMPLETE' and not active(db,op[0]):return 200,{'completed':True,'disable_action':True,
                                                 'notification':CATALOG.reason('CANCELLATION_COMPLETE')},tuple(records)
                running=db.execute("SELECT 1 FROM jobs j JOIN job_operation_members m USING(job_id) "
                                   "WHERE m.operation_id=? AND j.state='RUNNING' LIMIT 1",(op[0],)).fetchone()
                drained=False
                if not running and not unfinished_resources(db,op[0]):
                    # Expired/recovered DB state alone is not proof of drainage.
                    # Nonblocking lock also catches stale handlers still in I/O.
                    try:locks.enter_context(OperationGate(self.queue.database).hold(op[0],cleanup=True))
                    except CleanupBusy:pass
                    else:drained=True
                if drained:
                    try:validate_releasable(db,op[0])
                    except CancellationError:drained=False
                if drained:
                    db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state='COMPLETE',"
                               "reason_code='CANCELLATION_COMPLETE',updated_at_ms=? WHERE operation_id=?",(now,op[0]))
                    records.append(append_job_history(db,job_id=job_id,event_code='CANCELLATION_CLEANUP_CHANGED',
                        reason_code='CANCELLATION_COMPLETE',component='api',occurred_at_ms=now,details={'attempt':job_attempt(db,job_id)}))
                    # Same follow-ups as the cleanup service: free the slots,
                    # then the candidate controllers move on (nothing to delete).
                    from src.nextgen.storage.reservation_release import cleanup_completed
                    records.extend(cleanup_completed(db,op[0],job_id,now,component='api',
                                                     previous_cleanup_state=op[2]))
                    return 200,{'completed':True,'disable_action':True,
                                'notification':CATALOG.reason('CANCELLATION_COMPLETE')},tuple(records)
            event=db.execute('SELECT event_id FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]
            kind=KINDS[selected]
            if selected=='KEEP' and is_batch_operation(db,op[0]):
                kind='KEEP_BATCH'
            control,audit=enqueue_job_with_receipt(db,component='api',kind=kind,event_id=event,
                payload={'target_job_id':job_id},available_at_ms=now)
            db.execute('INSERT INTO management_actions VALUES(?,?,?,?,?)',(op[0],selected,job_id,control,now))
            records.append(audit)
            data=receipt(db,control)
        return 202,data,tuple(records)

    def capabilities(self,db,job_id):
        # Read using caller's snapshot. No mutations or nested connection.
        control=db.execute('SELECT action FROM management_actions WHERE job_id=?',(job_id,)).fetchone()
        if control:
            return {name:{'enabled':False,'disabled_reason':CATALOG.reason('MANAGEMENT_ACTION_UNAVAILABLE')}
                    for name in ('keep','cancel','cleanup')}
        op=operation(db,job_id)
        cancelable=can_cancel_operation(db,op)
        # An action job in this scope disables competing buttons as well.
        # Held/review/blocked jobs are not described as still running.
        busy=db.execute("SELECT j.state,j.last_reason_code FROM management_actions a JOIN jobs j USING(job_id) "
                        "WHERE a.operation_id=? AND j.state IN ('PENDING','RUNNING','RETRY_WAIT') "
                        "ORDER BY CASE j.state WHEN 'RUNNING' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END,j.job_id LIMIT 1",
                        (op[0],)).fetchone()
        result={}
        rejected=(db.execute('SELECT 1 FROM anime_review_decisions d JOIN episode_reservations r ON r.anidb_id=d.anidb_id JOIN job_operation_members m ON m.job_id=r.owner_job_id WHERE m.operation_id=?',(op[0],)).fetchone() is not None or
                  db.execute('SELECT 1 FROM anime_review_decisions d JOIN batch_episode_reservations r ON r.anidb_id=d.anidb_id JOIN job_operation_members m ON m.job_id=r.owner_job_id WHERE m.operation_id=?',(op[0],)).fetchone() is not None)
        from src.nextgen.storage.reservation_release import active
        release_pending=op[1]=='CANCELED' and op[2]=='COMPLETE' and active(db,op[0])
        for name,enabled in (('keep',op[1]=='STAGED' and not rejected),('cancel',cancelable),
                             ('cleanup',op[1] in ('CANCEL_REQUESTED','CANCELED') and (op[2]!='COMPLETE' or release_pending))):
            code='KEEP' if name=='keep' else 'CLEANUP'
            row=db.execute('SELECT job_id FROM management_actions WHERE operation_id=? AND action=?',(op[0],code)).fetchone()
            resumable=name in ('cleanup','keep') and row is not None and db.execute('SELECT state FROM jobs WHERE job_id=?',(row[0],)).fetchone()[0] in ('NEEDS_REVIEW','BLOCKED')
            if name=='cleanup' and release_pending and row is not None:
                resumable=db.execute('SELECT state FROM jobs WHERE job_id=?',(row[0],)).fetchone()[0]=='DONE' or resumable
            if name=='keep' and row is not None:
                cleanup=cleanup_status(db,row[0])
                if cleanup and cleanup['state'] in ('NEEDS_REVIEW','BLOCKED'):
                    resumable=True;enabled=True
            if name=='keep' and row is not None:
                download=download_status(db,row[0])
                if download and download['state'] in STOPPED_JOBS:
                    resumable=True;enabled=True
            if name=='keep' and row is not None:
                from src.nextgen.jobs.enrich_shoko_job import status as shoko_status
                shoko=shoko_status(db,row[0])
                if shoko and shoko['state'] in ('NEEDS_REVIEW','BLOCKED'):
                    resumable=True;enabled=True
            allowed=bool(enabled and (row is None or resumable) and busy is None)
            job=None if row is None else receipt(db,row[0],reused=True)
            reason=None
            if not allowed:
                reason=(disabled_reason(*busy) if busy is not None else
                        CATALOG.reason('ANIME_REJECTED') if rejected and name=='keep' else
                        job['disabled_reason'] if job is not None else
                        CATALOG.reason('MANAGEMENT_ACTION_UNAVAILABLE'))
            result[name]={'enabled':allowed,'disabled_reason':reason,'job':job}
        result['keep']['execution_available']=True
        result['keep']['availability_reason']=None
        return result

    def bind(self,claim,action):
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            data=claim.data()
            if not ({'target_job_id'} <= set(data) <= {'target_job_id', 'failure_reason'}):raise ActionUnavailable()
            integer(data['target_job_id'],positive=True)
            row=db.execute('SELECT target_job_id,operation_id FROM management_actions WHERE job_id=? AND action=?',
                           (claim.job_id,action)).fetchone()
            if row is None or row[0]!=data['target_job_id']:raise ActionUnavailable()
            expected=KINDS[action]
            if action=='KEEP' and is_batch_operation(db,row[1]):
                expected='KEEP_BATCH'
            if claim.kind!=expected:raise ActionUnavailable()
            if operation(db,claim.job_id)[0]==row[1]:raise ActionUnavailable()
            op=operation(db,row[0])
            if op[0]!=row[1] or (op[1]!='STAGED' if action=='KEEP' else op[1] not in ('CANCEL_REQUESTED','CANCELED')):
                raise ActionUnavailable()
            return row[0]
