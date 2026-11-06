"""Fenced, read-only (no reservations) multi-episode trial persistence."""
import sqlite3
import time

from src.nextgen.batch.user_evaluation import (
    BatchEpisodeFact, BatchUserEvaluationSource, BatchUserEvaluationProposal,
)
from src.nextgen.history import integer, append_job_history
from src.nextgen.storage.job_queue import QueueUnavailable
from src.nextgen.storage.migrations import LATEST_VERSION, MigrationError, _open
from src.nextgen.user_evaluation_policy import UserEvaluationConflict


def _source(db, verification_job_id, event_id):
    row=db.execute(
        "SELECT b.batch_id,b.anidb_id,b.tvdb_series_id,b.tvdb_season,j.event_id "
        "FROM batch_verifications b JOIN jobs j ON j.job_id=b.job_id "
        "JOIN incoming_events e ON e.event_id=j.event_id "
        "JOIN identity_results r ON r.result_id=b.identity_result_id "
        "WHERE b.job_id=? AND j.event_id=? AND j.state='DONE' "
        "AND j.kind='VERIFY_RELEASE' AND e.state='IDENTIFIED' "
        "AND r.event_id=j.event_id AND r.anidb_id=b.anidb_id",
        (verification_job_id,event_id)).fetchone()
    if row is None or row[4]!=event_id:
        raise UserEvaluationConflict()
    batch_id,aid,tid,season,_=row
    entries=db.execute(
        "SELECT file_index,release_episode,anidb_episode_id,tvdb_episode,"
        "tvdb_episode_id,file_size_bytes FROM batch_verified_files "
        "WHERE batch_id=? ORDER BY file_index",(batch_id,)).fetchall()
    try:
        return BatchUserEvaluationSource(verification_job_id,event_id,batch_id,aid,tid,
                                season,tuple(BatchEpisodeFact(*x) for x in entries))
    except (ValueError,TypeError):
        raise UserEvaluationConflict() from None


def load_batch_user_evaluation_source(database,verification_job_id,event_id):
    integer(verification_job_id,positive=True)
    integer(event_id,positive=True)
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:
            raise QueueUnavailable()
        return _source(db,verification_job_id,event_id)
    except (OSError,sqlite3.Error,MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()


def persist_batch_user_evaluation(db,claim,proposal):
    """Same transaction as completion; only immutable plan facts, no claim."""
    if not db.in_transaction or type(proposal) is not BatchUserEvaluationProposal:
        raise ValueError('Fenced worker completion and valid batch trial required')
    src=proposal.source
    if (claim.kind!='EVALUATE_BATCH' or claim.event_id!=src.event_id
            or _source(db,src.verification_job_id,src.event_id)!=src):
        raise UserEvaluationConflict()
    row=db.execute(
        'SELECT state,attempt_count,lease_token,lease_expires_at_ms '
        'FROM jobs WHERE job_id=? AND kind=? AND event_id=?',
        (claim.job_id,claim.kind,claim.event_id)).fetchone()
    now=integer(time.time_ns()//1000000)
    if (row is None or row[0]!='RUNNING' or row[1]!=claim.attempt
            or row[2]!=claim.token or row[3] is None or row[3]<=now):
        raise UserEvaluationConflict()
    if db.execute('SELECT 1 FROM batch_user_evaluation_plans WHERE batch_id=? OR evaluation_job_id=?',
                  (src.batch_id,claim.job_id)).fetchone():
        raise UserEvaluationConflict()
    total=sum(x.size_bytes for x in proposal.selected)
    limits=proposal.limits
    if total>limits.plan_bytes:
        from src.nextgen.acquisition.settings import SizeLimitExceeded
        raise SizeLimitExceeded()
    plan=db.execute(
        'INSERT INTO batch_user_evaluation_plans(batch_id,evaluation_job_id,anchor_episode,'
        'backfill_count,following_count,configuration_revision,max_episode_bytes,'
        'max_plan_bytes,planned_size_bytes,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (src.batch_id,claim.job_id,proposal.anchor_episode,
         proposal.window.backfill_count,proposal.window.following_count,
         proposal.configuration_revision,limits.episode_bytes,limits.plan_bytes,
         total,now)).lastrowid
    for item in proposal.selected:
        db.execute('INSERT INTO batch_user_evaluation_files(plan_id,file_index,tvdb_episode,'
                   'selected_size_bytes) VALUES(?,?,?,?)',
                   (plan,item.file_index,item.tvdb_episode,item.size_bytes))
    return append_job_history(
        db,job_id=claim.job_id,event_code='TRIAL_EVALUATED',
        reason_code='TRIAL_SELECTED',component='worker',
        occurred_at_ms=now,details={'attempt':claim.attempt})
