"""Independent scan requests after successful staging or Keep."""
from src.anime.async_io import blocking
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import Outcome, Registration
from src.anime.jellyfin import request_refresh, RefreshConfiguration, RefreshUnavailable, RefreshReview
from src.anime.secrets import SecretStorageError
from src.anime.storage.sqlite import connect, enqueue_job
from src.anime.services.base import service_facts

KIND='REFRESH_JELLYFIN'
SERVICES=('jellyfin',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Request Jellyfin library scan')


def enqueue(db,claim):
    # Called inside the source completion transaction, exactly once. A separate
    # operation prevents refresh retries from changing staging/Keep eligibility.
    return enqueue_job(db,kind=KIND,payload={'source_job_id':claim.job_id},component='worker',
        event_id=claim.event_id,created_by_job_id=claim.job_id)


def registration(secrets,*,refresh=request_refresh):
    async def execute(context):
        try:
            payload=context.data()
            if set(payload)!={'source_job_id'}:raise ValueError()
            source=integer(payload['source_job_id'],positive=True)
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        def run():
            db=connect(context.configuration.value('storage.database_path'))
            try:
                row=db.execute('SELECT kind,state,event_id FROM jobs WHERE job_id=?',(source,)).fetchone()
                origin=db.execute('SELECT created_by_job_id FROM jobs WHERE job_id=?',(context.claim.job_id,)).fetchone()
                if (not row or row[0] not in ('STAGE_USER_EVALUATION_MEDIA','STAGE_BATCH_EPISODE','KEEP_MEDIA','KEEP_BATCH','CLEANUP_KEPT_MEDIA','CLEANUP_BATCH_KEPT_MEDIA') or row[1]!='DONE'
                        or row[2]!=context.claim.event_id or origin!=(source,)):raise ValueError()
            finally:db.close()
            if context.cancelled.is_set():return
            refresh(context.configuration,secrets)
        try:await blocking(run)
        except RefreshConfiguration:return Outcome('review','SERVICE_SETUP_INVALID',facts=service_facts('jellyfin','NOT_CONFIGURED'))
        except (RefreshUnavailable,SecretStorageError):return Outcome('retry','SERVICE_UNAVAILABLE',facts=service_facts('jellyfin'))
        except RefreshReview:return Outcome('review','MEDIA_SERVER_REFRESH_REVIEW')
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        return Outcome()
    return Registration(KIND,DEFINITION,execute,services=SERVICES)
