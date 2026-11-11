"""Copy verified viewing videos; Keep and library refresh remain separate."""
import logging

from src.transcode_agent.encode import EncodeError
from src.anime.services.transcode import TranscodeWaiting, TranscodeFailed
from src.anime.acquisition.media import MediaInvalid
from src.anime.acquisition.metadata import AcquisitionConflict, MetadataInvalid, SourceMissing
from src.anime.acquisition.settings import SizeEvidenceMissing, DownloadConfigurationError
from src.anime.acquisition.submission import ClientInvalid
from src.anime.acquisition.transfers import TransferConflict
from src.anime.async_io import blocking
from src.anime.cancellation import CancellationError
from src.anime.diagnostics import exception
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import CompletionRejected, Outcome, Registration
from src.anime.staging import Publisher, StagingReview, StagingUnavailable, StagingBusy
from src.anime.storage.staging import Journal, persist
from src.anime.episode_reservations import ReservationConflict

KIND='STAGE_USER_EVALUATION_MEDIA'
DEFINITION=HandlerDefinition(KIND,'Copy verified video into staging with episode metadata')
REVIEW=(StagingReview,MediaInvalid,AcquisitionConflict,MetadataInvalid,SourceMissing,
        SizeEvidenceMissing,DownloadConfigurationError,ClientInvalid,TransferConflict,CancellationError,
        ReservationConflict)  # e.g. the reservation was released by an earlier Cancel


def _persist(db,claim,value):
    try:
        value=dict(value)
        refresh=value.pop('refresh',False)
        records=persist(db,claim,value)
        # The video is safely staged: remove the original torrent
        # and any leftover files now, in their own cleanup operation.
        from src.anime.decision_download_cleanup import enqueue_after_staging
        enqueue_after_staging(db,claim)
        if refresh:
            from src.anime.jobs.refresh_jellyfin_job import enqueue
            enqueue(db,claim)
        return records
    except REVIEW as exc:raise CompletionRejected(Outcome('review',exc.reason_code)) from None


def registration(*,publisher_factory=Publisher,journal_factory=Journal,secrets=None):
    async def execute(context):
        def failure(exc,kind,reason):
            exception(context.logger,'OPERATION_FAILED',exc,level=logging.WARNING,
                      reason_code=reason,component='worker',job_id=context.claim.job_id,
                      event_id=context.claim.event_id,attempt=context.claim.attempt)
            facts=None
            if kind=='review' and isinstance(exc,StagingReview) and getattr(exc,'failed_check',None):
                facts={'failed_check':exc.failed_check,'failed_path':exc.failed_path}
            return Outcome(kind,reason,facts=facts)
        payload=context.data()
        try:
            if set(payload)!={'verification_job_id'} or context.claim.event_id is None:raise ValueError()
            verification=integer(payload['verification_job_id'],positive=True)
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        def run():
            if secrets is not None:
                from src.anime.storage.transcode import prepare
                prepare(context,verification,secrets,batch=False)
            journal=journal_factory(context.configuration.value('storage.database_path'))
            publisher=publisher_factory(journal,context.claim,context.cancelled)
            with journal.lock(context.claim):
                plan=journal.prepare(context.claim,verification,context.configuration)
                receipts=publisher.publish(plan)
                journal.guard(context.claim)
                return {'verification_job_id':verification,'receipts':receipts,'refresh':bool(context.configuration.value('jellyfin.base_url'))}
        try:return Outcome(value=await blocking(run))
        except TranscodeWaiting as exc:
            return Outcome('defer','TRANSCODE_WAITING',delay_ms=10000,facts={'service_detail':str(exc)[:500]})
        except (TranscodeFailed,EncodeError) as exc:
            return Outcome('review','TRANSCODE_FAILED',facts={'service_detail':str(exc)[:500]})
        except StagingBusy as exc:return Outcome('defer',exc.reason_code,delay_ms=1000)
        except FileExistsError as exc:return failure(exc,'review','STAGING_REVIEW')
        except (StagingUnavailable,OSError) as exc:return failure(exc,'retry','STAGING_UNAVAILABLE')
        except REVIEW as exc:return failure(exc,'review',exc.reason_code)
    return Registration(KIND,DEFINITION,execute,persist=_persist)
