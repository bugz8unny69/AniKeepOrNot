"""Durable exact-episode gates in the worker's fenced completion transaction."""
import sqlite3
import time

from src.nextgen.episode_reservations import ReservationInput, ReservationConflict, EpisodeReserved, EpisodeStaged
from src.nextgen.history import integer, append_job_history
from src.nextgen.storage.job_queue import QueueUnavailable
from src.nextgen.storage.migrations import LATEST_VERSION, MigrationError, _open
from src.nextgen.storage.releases import _source as release_source
from src.nextgen.storage.user_evaluations import _source as user_evaluation_source
from src.nextgen.user_evaluation_policy import Window, UserEvaluationConflict
from src.nextgen.release_contracts import ReleaseNotReady


def _facts(db,evaluation_job_id,event_id):
    row=db.execute("SELECT t.verification_id,t.plan_id,t.selected,t.reason_code,r.job_id,r.anidb_id,"
        "r.anidb_episode_id,r.tvdb_series_id,r.tvdb_season,r.tvdb_episode,r.tvdb_episode_id,"
        "r.identity_result_id,r.release_sha256,p.anidb_id,p.tvdb_series_id,p.tvdb_season,p.tvdb_order,"
        "p.anchor_episode,p.backfill_count,p.following_count,p.anchor_verification_id "
        "FROM user_evaluation_checks t JOIN jobs j ON j.job_id=t.job_id "
        "JOIN release_verifications r ON r.verification_id=t.verification_id "
        "JOIN user_evaluation_plans p ON p.plan_id=t.plan_id "
        "WHERE t.job_id=? AND j.event_id=? AND j.kind='SELECT_USER_EVALUATION' AND j.state='DONE'",
        (evaluation_job_id,event_id)).fetchone()
    if row is None:raise ReservationConflict()
    (verification,plan,selected,reason,release_job,aid,eid,tid,season,episode,tveid,
     identity,digest,plan_aid,plan_tid,plan_season,order,anchor,backfill,following,anchor_id)=row
    try:
        source=user_evaluation_source(db,release_job,event_id)
        original=release_source(db,event_id,aid)
        # The window is in AniDB numbers; TVDB placement stays per episode.
        expected=source.anidb_episode_number in Window(backfill,following).anidb_episodes(anchor)
    except (UserEvaluationConflict,ReleaseNotReady,ValueError,TypeError):raise ReservationConflict() from None
    if (source.verification_id!=verification or original.identity_result_id!=identity or
            original.release_sha256!=digest or original.tvdb_series_id!=tid or
            (source.tvdb_season,source.tvdb_episode)!=(season,episode) or
            (plan_aid,plan_tid,order)!=(aid,tid,'official') or
            bool(selected)!=expected or reason!=('USER_EVALUATION_SELECTED' if expected else 'USER_EVALUATION_OUTSIDE_WINDOW')):
        raise ReservationConflict()
    anchor_row=db.execute("SELECT anidb_id,tvdb_series_id,anidb_episode_number,tvdb_order "
                          "FROM release_verifications WHERE verification_id=?",(anchor_id,)).fetchone()
    owner=db.execute('SELECT anidb_id FROM identified_episodes WHERE anidb_episode_id=?',(eid,)).fetchone()
    if anchor_row!=(aid,tid,anchor,'official') or owner!=(aid,):raise ReservationConflict()
    return ReservationInput(evaluation_job_id,event_id,verification,plan,aid,eid,tid,season,episode,tveid,expected)


def load_reservation_input(database,evaluation_job_id,event_id):
    integer(evaluation_job_id,positive=True)
    integer(event_id,positive=True)
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute("BEGIN")
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:raise QueueUnavailable()
        return _facts(db,evaluation_job_id,event_id)
    except (OSError,sqlite3.Error,MigrationError):raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()


def persist_reservation(db,claim,value):
    """No commit, lease takeover, external action or whole-anime lock here."""
    if not db.in_transaction or type(value) is not ReservationInput:
        raise ValueError('worker transaction and verified episode facts required')
    if claim.event_id!=value.event_id or _facts(db,value.evaluation_job_id,claim.event_id)!=value:
        raise ReservationConflict()
    now=integer(time.time_ns()//1000000)
    if not value.selected:
        return append_job_history(db,job_id=claim.job_id,event_code='EPISODE_RESERVATION_SKIPPED',
            reason_code='USER_EVALUATION_OUTSIDE_WINDOW',component='worker',occurred_at_ms=now,details={'attempt':claim.attempt})
    key=(value.tvdb_series_id,value.tvdb_season,value.tvdb_episode)
    # Check the batch namespace in this same fenced SQLite transaction,
    # before the existing single-episode reservation is inserted.
    tvdb_check = ""
    check_args = [value.anidb_episode_id]
    if value.tvdb_episode_id is not None:
        tvdb_check += " OR tvdb_episode_id=?"
        check_args.append(value.tvdb_episode_id)
    if value.tvdb_season is not None and value.tvdb_episode is not None:
        tvdb_check += " OR (tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=?)"
        check_args.extend([value.tvdb_series_id, value.tvdb_season, value.tvdb_episode])

    batch_owner = db.execute(
        f"SELECT owner_job_id FROM batch_episode_reservations WHERE state='ACTIVE' AND (anidb_episode_id=?{tvdb_check}) LIMIT 1",
        check_args).fetchone()
    if batch_owner:
        owner_id = batch_owner[0]
        op = db.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?", (owner_id,)).fetchone()
        raise EpisodeReserved(owner_job_id=owner_id, operation_id=op[0] if op else None)
    rows=db.execute("SELECT owner_job_id,evaluation_job_id,anidb_id,anidb_episode_id,"
                    "tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id FROM episode_reservations "
                    f"WHERE state='ACTIVE' AND (anidb_episode_id=?{tvdb_check})",
                    check_args).fetchall()
    expected=(value.anidb_id,value.anidb_episode_id,*key,value.tvdb_episode_id)
    prior=db.execute('SELECT state FROM episode_reservations WHERE owner_job_id=?',(claim.job_id,)).fetchone()
    if prior==('RELEASED',):raise ReservationConflict()
    if rows:
        if len(rows)!=1 or rows[0][2:]!=expected:raise ReservationConflict()
        if rows[0][0]!=claim.job_id:
            if published(db,rows[0][0]):raise EpisodeStaged()
            owner_id = rows[0][0]
            op = db.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?", (owner_id,)).fetchone()
            raise EpisodeReserved(owner_job_id=owner_id, operation_id=op[0] if op else None)
        if rows[0][1]!=value.evaluation_job_id:raise ReservationConflict()
    else:
        db.execute("INSERT INTO episode_reservations(owner_job_id,evaluation_job_id,verification_id,plan_id,"
            "anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,reserved_at_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",(claim.job_id,value.evaluation_job_id,value.verification_id,value.plan_id,
            value.anidb_id,value.anidb_episode_id,*key,value.tvdb_episode_id,now))
    return append_job_history(db,job_id=claim.job_id,event_code='EPISODE_RESERVATION_CREATED',
        reason_code='EPISODE_RESERVATION_GRANTED',component='worker',occurred_at_ms=now,details={'attempt':claim.attempt})


def published(db,owner_job_id):
    return db.execute("SELECT 1 FROM episode_reservations r JOIN acquisition_plans p USING(reservation_id) "
        "JOIN staging_publications s ON s.plan_id=p.plan_id WHERE r.owner_job_id=? AND r.state='ACTIVE' "
        "AND s.state='PUBLISHED'",(owner_job_id,)).fetchone() is not None


def wake_waiters(db,reservation_id,now,*,component='worker'):
    """Release hands the episode to ONE oldest eligible waiter at a time.

    Later waiters remain held until this contender also releases its slot or
    a successful publication cancels them. Never run all candidates at once.
    """
    if not db.in_transaction:raise ValueError('reservation transaction required')
    row=db.execute("SELECT anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,"
        "tvdb_episode_id FROM episode_reservations WHERE reservation_id=?",(reservation_id,)).fetchone()
    if row is None:return ()
    records=[]
    waiters=db.execute("SELECT j.job_id,j.event_id,json_extract(j.payload_json,'$.evaluation_job_id'),j.attempt_count "
        "FROM jobs j JOIN job_operation_members m USING(job_id) JOIN job_operations o USING(operation_id) "
        "WHERE j.kind='RESERVE_EPISODE' AND j.state='ON_HOLD' AND j.last_reason_code='EPISODE_RESERVED' "
        "AND o.state='ACTIVE' ORDER BY j.created_at_ms,j.job_id").fetchall()
    for job,event,evaluation,attempt in waiters:
        try:value=_facts(db,evaluation,event)
        except ReservationConflict:continue
        if not value.selected or (value.anidb_id,value.anidb_episode_id,value.tvdb_series_id,
                value.tvdb_season,value.tvdb_episode,value.tvdb_episode_id)!=row:continue
        db.execute("UPDATE jobs SET state='PENDING',available_at_ms=?,updated_at_ms=?,last_reason_code=NULL,"
            "activity_code=NULL WHERE job_id=? AND state='ON_HOLD'",(now,now,job))
        records.append(append_job_history(db,job_id=job,event_code='RETRY_SCHEDULED',
            reason_code='CANCELED_RESERVATIONS_RELEASED',component=component,
            occurred_at_ms=now,details={'attempt':attempt}))
        break  # A successful claim will keep the others held; a later release wakes the next.
    return tuple(records)


def _cancel_staged_waiter(db,job_id,now):
    """Use the same cancellation and owned-resource cleanup as manual Cancel."""
    from src.nextgen.storage.cancellation import operation, request_cancellation
    from src.nextgen.storage.sqlite import enqueue_job_with_receipt
    row=db.execute("SELECT state,last_reason_code,event_id FROM jobs WHERE job_id=?",(job_id,)).fetchone()
    if row is None or row[:2]!=('ON_HOLD','EPISODE_RESERVED'):return ()
    scope=operation(db,job_id)
    if scope[1]!='ACTIVE':return ()
    if db.execute("SELECT 1 FROM management_actions WHERE operation_id=? AND action='CLEANUP'",(scope[0],)).fetchone():
        return ()
    operation_id,records=request_cancellation(db,job_id,now,component='worker',reason_code='EPISODE_ALREADY_STAGED')
    control,audit=enqueue_job_with_receipt(db,component='worker',kind='CLEANUP_CANCELED_OPERATION',
        event_id=row[2],payload={'target_job_id':job_id},available_at_ms=now)
    db.execute('INSERT INTO management_actions VALUES(?,?,?,?,?)',
               (operation_id,'CLEANUP',job_id,control,now))
    return (*records,audit)


def cancel_staged_waiters(db,plan_id,now,*,only_job_id=None):
    """Cancel held competitors once the exact episode has a published STAGED winner."""
    if not db.in_transaction:raise ValueError('staging transaction required')
    row=db.execute("SELECT r.owner_job_id,r.anidb_id,r.anidb_episode_id,r.tvdb_series_id,"
        "r.tvdb_season,r.tvdb_episode,r.tvdb_episode_id FROM episode_reservations r "
        "JOIN acquisition_plans p USING(reservation_id) WHERE p.plan_id=? AND r.state='ACTIVE'",(plan_id,)).fetchone()
    if row is None or not published(db,row[0]):raise ReservationConflict()
    winner=db.execute("SELECT o.state FROM job_operations o JOIN job_operation_members m USING(operation_id) "
                      "WHERE m.job_id=?",(row[0],)).fetchone()
    if winner!=('STAGED',):raise ReservationConflict()
    query=("SELECT j.job_id,j.event_id,CASE WHEN json_valid(j.payload_json) THEN json_extract(j.payload_json,'$.evaluation_job_id') END "
        "FROM jobs j JOIN job_operation_members m USING(job_id) JOIN job_operations o USING(operation_id) "
        "WHERE j.kind='RESERVE_EPISODE' AND j.state='ON_HOLD' AND j.last_reason_code='EPISODE_RESERVED' "
        "AND o.state='ACTIVE'")
    args=()
    if only_job_id is not None:
        query+=' AND j.job_id=?'
        args=(integer(only_job_id,positive=True),)
    records=[]
    for job,event,evaluation in db.execute(query,args).fetchall():
        try:value=_facts(db,evaluation,event)
        except (ReservationConflict,ValueError,TypeError):continue
        if not value.selected or (value.anidb_id,value.anidb_episode_id,value.tvdb_series_id,
                value.tvdb_season,value.tvdb_episode,value.tvdb_episode_id)!=row[1:]:continue
        records.extend(_cancel_staged_waiter(db,job,now))
    return tuple(records)




def _resume_unreserved_waiters(db, now, limit=32):
    """Recover an episode handoff when a woken contender never claimed a slot.

    A failed/stale pending claimant must not strand all the later held copies.
    This runs under the same SQLite transaction as reservation checks.
    """
    waiters=db.execute(
        "SELECT j.job_id,j.event_id,json_extract(j.payload_json,'$.evaluation_job_id'),j.attempt_count "
        "FROM jobs j JOIN job_operation_members m USING(job_id) "
        "JOIN job_operations o USING(operation_id) "
        "WHERE j.kind='RESERVE_EPISODE' AND j.state='ON_HOLD' "
        "AND j.last_reason_code='EPISODE_RESERVED' AND o.state='ACTIVE' "
        "ORDER BY j.created_at_ms,j.job_id"
    ).fetchall()
    records=[]
    seen=set()
    for job,event,evaluation,attempt in waiters:
        try: value=_facts(db,evaluation,event)
        except (ReservationConflict,ValueError,TypeError): continue
        if not value.selected: continue
        key=(value.anidb_id,value.anidb_episode_id,value.tvdb_series_id,
             value.tvdb_season,value.tvdb_episode,value.tvdb_episode_id)
        if key in seen: continue
        seen.add(key)
        tvdb_filter = ""
        check_args = [value.anidb_episode_id]
        if value.tvdb_episode_id is not None:
            tvdb_filter += " OR tvdb_episode_id=?"
            check_args.append(value.tvdb_episode_id)
        if value.tvdb_season is not None and value.tvdb_episode is not None:
            tvdb_filter += " OR (tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=?)"
            check_args.extend([value.tvdb_series_id, value.tvdb_season, value.tvdb_episode])

        if any(db.execute(
            f"SELECT 1 FROM {table} WHERE state='ACTIVE' AND (anidb_episode_id=?{tvdb_filter}) LIMIT 1",
            check_args
        ).fetchone() for table in ('episode_reservations','batch_episode_reservations')):
            continue
        # A previously awakened job may still be queued/running. Do not race it.
        queued_tvdb = ""
        queued_args = [value.anidb_id, value.anidb_episode_id, value.tvdb_series_id]
        if value.tvdb_season is not None and value.tvdb_episode is not None and value.tvdb_episode_id is not None:
            queued_tvdb = "AND v.tvdb_season=? AND v.tvdb_episode=? AND v.tvdb_episode_id=? "
            queued_args.extend([value.tvdb_season, value.tvdb_episode, value.tvdb_episode_id])
        else:
            queued_tvdb = "AND v.tvdb_season IS NULL AND v.tvdb_episode IS NULL AND v.tvdb_episode_id IS NULL "
        queued=db.execute(
            "SELECT 1 FROM jobs j "
            "JOIN job_operation_members m USING(job_id) "
            "JOIN job_operations o USING(operation_id) "
            "JOIN user_evaluation_checks t ON t.job_id=json_extract(j.payload_json,'$.evaluation_job_id') "
            "JOIN release_verifications v ON v.verification_id=t.verification_id "
            "WHERE j.kind='RESERVE_EPISODE' AND j.state IN ('PENDING','RUNNING','RETRY_WAIT') "
            f"AND o.state='ACTIVE' AND t.selected=1 "
            f"AND v.anidb_id=? AND v.anidb_episode_id=? AND v.tvdb_series_id=? "
            f"{queued_tvdb}LIMIT 1",
            queued_args
        ).fetchone()
        if queued: continue
        updated=db.execute(
            "UPDATE jobs SET state='PENDING',available_at_ms=?,updated_at_ms=?,"
            "last_reason_code=NULL,activity_code=NULL WHERE job_id=? "
            "AND state='ON_HOLD' AND last_reason_code='EPISODE_RESERVED'",
            (now,now,job)
        )
        if updated.rowcount:
            records.append(append_job_history(
                db,job_id=job,event_code='RETRY_SCHEDULED',
                reason_code='CANCELED_RESERVATIONS_RELEASED',component='worker',
                occurred_at_ms=now,details={'attempt':attempt}))
        if len(records)>=limit: break
    return tuple(records)


def reconcile_unreserved_waiters(database, *, limit=32):
    """Worker polling pass; one eligible acquisition per unreserved episode."""
    integer(limit,positive=True)
    from src.nextgen.storage.job_queue import JobQueue
    from src.nextgen.storage.sqlite import transaction
    queue=JobQueue(database)
    with queue.connection() as db,transaction(db):
        return _resume_unreserved_waiters(db,queue.now(),limit)

def reconcile_staged_waiters(database,*,limit=32):
    """Worker pass for missed stage events; no external I/O or fix script."""
    integer(limit,positive=True)
    from src.nextgen.storage.job_queue import JobQueue
    from src.nextgen.storage.sqlite import transaction
    queue=JobQueue(database)
    with queue.connection() as db,transaction(db):
        now=queue.now()
        waiters=db.execute("SELECT j.job_id,j.event_id,CASE WHEN json_valid(j.payload_json) THEN json_extract(j.payload_json,'$.evaluation_job_id') END "
            "FROM jobs j JOIN job_operation_members m USING(job_id) JOIN job_operations o USING(operation_id) "
            "WHERE j.kind='RESERVE_EPISODE' AND j.state='ON_HOLD' AND j.last_reason_code='EPISODE_RESERVED' "
            "AND o.state='ACTIVE' ORDER BY j.job_id").fetchall()
        records=[];handled=0
        for job,event,evaluation in waiters:
            try:value=_facts(db,evaluation,event)
            except (ReservationConflict,ValueError,TypeError):continue
            if not value.selected:continue
            staged_tvdb = ""
            staged_args = [value.anidb_id, value.anidb_episode_id]
            if value.tvdb_series_id is not None:
                staged_tvdb += "AND r.tvdb_series_id=? "
                staged_args.append(value.tvdb_series_id)
            if value.tvdb_season is not None and value.tvdb_episode is not None:
                staged_tvdb += "AND r.tvdb_season=? AND r.tvdb_episode=? "
                staged_args.extend([value.tvdb_season, value.tvdb_episode])
            else:
                staged_tvdb += "AND r.tvdb_season IS NULL AND r.tvdb_episode IS NULL "
            if value.tvdb_episode_id is not None:
                staged_tvdb += "AND r.tvdb_episode_id=? "
                staged_args.append(value.tvdb_episode_id)
            else:
                staged_tvdb += "AND r.tvdb_episode_id IS NULL "

            plan=db.execute("SELECT p.plan_id FROM episode_reservations r "
                "JOIN acquisition_plans p USING(reservation_id) "
                "JOIN staging_publications s ON s.plan_id=p.plan_id AND s.state='PUBLISHED' "
                "JOIN job_operation_members m ON m.job_id=r.owner_job_id "
                "JOIN job_operations o ON o.operation_id=m.operation_id AND o.state='STAGED' "
                f"WHERE r.state='ACTIVE' AND r.anidb_id=? AND r.anidb_episode_id=? "
                f"{staged_tvdb}LIMIT 1",
                staged_args).fetchone()
            if plan is None:continue
            records.extend(cancel_staged_waiters(db,plan[0],now,only_job_id=job))
            handled+=1
            if handled>=limit:break
        return tuple(records)
