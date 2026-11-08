"""Verify Sonarr Keep results before deleting exact staged batch copies."""
from src.anime.async_io import blocking
from src.anime.batch_post_keep_cleanup import BatchPostKeepCleanup
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import CompletionRejected, Outcome, Registration
from src.anime.sonarr import KeepReview, KeepUnavailable
from src.anime.staging import StagingReview, StagingBusy
from src.anime.keep_selection import KeepSelectionInvalid
from src.anime.acquisition.media import MediaInvalid
from src.anime.storage.sqlite import enqueue_job

KIND='CLEANUP_BATCH_KEPT_MEDIA'
SERVICES=('sonarr',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Verify batch Keep and clean staged copies')


def enqueue(db,claim):
    return enqueue_job(
        db,kind=KIND,component='worker',
        payload={'keep_job_id':claim.job_id},
        event_id=claim.event_id,created_by_job_id=claim.job_id)


def _persist(db,claim,value):
    if type(value) is not dict or set(value)!={'keep_job_id'}:
        raise CompletionRejected(Outcome('review','POST_KEEP_CLEANUP_REVIEW'))
    keep=integer(value['keep_job_id'],positive=True)
    row=db.execute(
        'SELECT phase,decision_job_id FROM batch_keep_cleanup WHERE job_id=?',
        (claim.job_id,)).fetchone()
    if row!=('COMPLETE',keep):
        raise CompletionRejected(Outcome('review','POST_KEEP_CLEANUP_REVIEW'))
    from src.anime.jobs.cleanup_batch_download_job import enqueue as enqueue_download
    from src.anime.storage.decision_cleanup_status import _staging_rows
    if not _staging_rows(db,keep):   # Packs staged before this change.
        enqueue_download(db,claim,decision=keep)


def registration(secrets,*,service_factory=BatchPostKeepCleanup):
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'keep_job_id'}:raise ValueError()
            keep=integer(payload['keep_job_id'],positive=True)
        except (ValueError,TypeError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            complete=await blocking(
                service_factory(context.configuration,secrets).run,
                context.claim,keep,context.cancelled)
        except StagingBusy:
            return Outcome('defer','POST_KEEP_CLEANUP_WAITING',delay_ms=1000)
        except (KeepReview,StagingReview,KeepSelectionInvalid,MediaInvalid,
                ValueError,KeyError,TypeError):
            return Outcome('review','POST_KEEP_CLEANUP_REVIEW')
        except (KeepUnavailable,OSError):
            return Outcome('retry','POST_KEEP_CLEANUP_UNAVAILABLE')
        return (Outcome(value={'keep_job_id':keep}) if complete
                else Outcome('defer','POST_KEEP_CLEANUP_WAITING',delay_ms=30000))
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist)
