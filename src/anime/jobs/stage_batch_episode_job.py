"""Stage one fully verified batch episode with no-replace, copy-only receipts."""
from src.anime.acquisition.media import MediaInvalid
from src.anime.async_io import blocking
from src.anime.cancellation import CancellationError
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import CompletionRejected, Outcome, Registration, failure_facts
from src.anime.staging import (
    Publisher, StagingReview, StagingUnavailable, StagingBusy,
)
from src.anime.storage.batch_staging import BatchStagingJournal, persist_batch_stage

KIND='STAGE_BATCH_EPISODE'
DEFINITION=HandlerDefinition(KIND,'Publish verified batch episode into staging')


def _persist(db,claim,value):
    try:
        value=dict(value)
        refresh=value.pop('refresh',False)
        records=persist_batch_stage(db,claim,value)
        if refresh:
            from src.anime.jobs.refresh_jellyfin_job import enqueue
            enqueue(db,claim)
        return records
    except (StagingReview,MediaInvalid,CancellationError) as exc:
        raise CompletionRejected(Outcome('review',exc.reason_code, facts=failure_facts(exc))) from None


def registration(*,publisher_factory=Publisher,journal_factory=BatchStagingJournal):
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'verification_job_id'}:
                raise ValueError()
            verification=integer(payload['verification_job_id'],positive=True)
        except (ValueError,TypeError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        def run():
            journal=journal_factory(context.configuration.value('storage.database_path'))
            publisher=publisher_factory(journal,context.claim,context.cancelled)
            with journal.lock(context.claim):
                plan=journal.prepare(context.claim,verification,context.configuration)
                receipts=publisher.publish(plan)
                journal.guard(context.claim)
                return {'verification_job_id':verification,'receipts':receipts,
                        'refresh':bool(context.configuration.value('jellyfin.base_url'))}
        try:return Outcome(value=await blocking(run))
        except StagingBusy as exc:
            return Outcome('defer',exc.reason_code,delay_ms=1000)
        except FileExistsError as exc:
            return Outcome('review','STAGING_REVIEW', facts=failure_facts(exc))
        except (StagingUnavailable,OSError):
            return Outcome('retry','STAGING_UNAVAILABLE')
        except (StagingReview,MediaInvalid,CancellationError) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
    return Registration(KIND,DEFINITION,execute,persist=_persist)
