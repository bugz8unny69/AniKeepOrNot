"""Whole-pack Keep: reconcile each independently staged episode with Sonarr."""
from functools import partial
import time

from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration
from src.nextgen.storage.batch_keep_imports import BatchKeepJournal, batch_publications
from src.nextgen.keep_service import Service
from src.nextgen.jobs.keep_execution import execute_keep
from src.nextgen.sonarr import KeepReview
from src.nextgen.history import job_attempt, append_job_history
from src.nextgen.storage.cancellation import operation

KIND='KEEP_BATCH'
SERVICES=('sonarr',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Keep verified batch episodes in the permanent library')


def _persist(db,claim,value):
    if type(value) is not dict or set(value)!={'refresh'}:
        raise CompletionRejected(Outcome('review','KEEP_IMPORT_REVIEW'))
    row=db.execute(
        "SELECT target_job_id,operation_id FROM management_actions "
        "WHERE job_id=? AND action='KEEP'",(claim.job_id,)).fetchone()
    if row is None or operation(db,row[0])[:2]!=(row[1],'STAGED'):
        raise CompletionRejected(Outcome('review','KEEP_IMPORT_REVIEW'))
    try:staged=batch_publications(db,row[1])
    except KeepReview:
        raise CompletionRejected(Outcome('review','KEEP_IMPORT_REVIEW')) from None
    if db.execute(
        'SELECT file_index,phase FROM batch_keep_imports '
        'WHERE job_id=? ORDER BY file_index',(claim.job_id,)).fetchall()!=[
        (item[1],'COMPLETE') for item in staged]:
        raise CompletionRejected(Outcome('review','KEEP_IMPORT_REVIEW'))
    from src.nextgen.jobs.cleanup_batch_kept_media_job import enqueue as enqueue_staging_cleanup
    enqueue_staging_cleanup(db,claim)
    if value['refresh']:
        from src.nextgen.jobs.refresh_jellyfin_job import enqueue
        enqueue(db,claim)
    return (append_job_history(
        db,job_id=claim.job_id,event_code='KEEP_COMPLETED',
        reason_code='KEEP_COMPLETE',component='worker',
        occurred_at_ms=time.time_ns()//1000000,
        details={'attempt':job_attempt(db,claim.job_id)}),)


def _episode_journals(actions, target):
    # Trusted operation and published-file inventory; no HTTP or qB action.
    with actions.queue.connection() as db:
        indices = tuple(row[1] for row in batch_publications(
            db, operation(db, target)[0]))
    return tuple(partial(BatchKeepJournal, file_index=index) for index in indices)


def registration(secrets,*,service_factory=Service):
    async def execute(context):
        return await execute_keep(context, secrets, service_factory=service_factory,
                                  journals=_episode_journals)
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist)
