"""E01 media hash and Japanese+English audio verification worker."""
import inspect

from src.nextgen.acquisition.metadata import (
    read_batch_inventory, AcquisitionConflict, MetadataInvalid, SourceMissing,
)
from src.nextgen.acquisition.media import (
    VerifiedMedia, MediaInvalid, MediaUnavailable, ProbeUnavailable,
)
from src.nextgen.acquisition.metainfo_source import (
    MetadataSourceWaiting, MetadataSourceUnavailable,
    MetadataSourceConfigurationError, MetadataSourceDenied,
)
from src.nextgen.batch.media import verify_batch_probe_file
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import (
    CompletionRejected, Outcome, Registration, Successor, failure_facts,
)
from src.nextgen.storage.batch_probe_source import BatchProbeSource
from src.nextgen.storage.batch_probe_media import persist_batch_probe_media
from src.nextgen.worker import blocking


KIND='VERIFY_BATCH_PROBE'
DEFINITION=HandlerDefinition(KIND,'Verify E01 audio from actual batch video')


def _persist(db,claim,value):
    try:
        if (type(value) is not dict or set(value)!={'verification_job_id','media'}
                or value['verification_job_id']!=claim.job_id
                or type(value['media']) is not VerifiedMedia):
            raise MediaInvalid()
        persist_batch_probe_media(db,claim,value['media'])
    except (MediaInvalid,AcquisitionConflict) as exc:
        raise CompletionRejected(Outcome('review',exc.reason_code, facts=failure_facts(exc))) from None


def registration(read_batch_metainfo,*,source_factory=BatchProbeSource,
                 verify=verify_batch_probe_file,unlock_kind=None):
    if not inspect.iscoroutinefunction(read_batch_metainfo):
        raise ValueError('Trusted async metadata reader required')
    if unlock_kind is not None and (type(unlock_kind) is not str
            or not unlock_kind.strip() or len(unlock_kind)>64
            or unlock_kind==KIND):
        raise ValueError('Invalid batch unlock successor')

    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'transfer_job_id'} or context.claim.event_id is None:
                raise ValueError()
            transfer=integer(payload['transfer_job_id'],positive=True)
        except (ValueError,TypeError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        source=source_factory(context.configuration.value('storage.database_path'))
        try:
            digest=await blocking(source.source_hash,context.claim,transfer)
            raw=await read_batch_metainfo(digest,context.configuration,context.cancelled)
            inventory=await blocking(read_batch_inventory,raw,digest)
            intent,record=await blocking(
                source.load,context.claim,transfer,inventory)
            protected=(
                context.configuration.value('storage.user_evaluation_root'),
                context.configuration.value('storage.keep_root'),
                context.configuration.value('storage.database_path'),
            )
            evidence=await blocking(
                verify,intent,record,transfer,context.cancelled,
                protected_roots=protected,
                audio=context.configuration.value('acquisition.audio_check'))
            return Outcome(value={
                'verification_job_id':context.claim.job_id,'media':evidence})
        except MetadataSourceWaiting as exc:
            return Outcome('defer',exc.reason_code,delay_ms=exc.delay_ms)
        except MetadataSourceUnavailable as exc:
            return Outcome('retry',exc.reason_code,facts=failure_facts(exc))
        except (MetadataSourceConfigurationError,MetadataSourceDenied) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
        except (MediaUnavailable,ProbeUnavailable):
            return Outcome('retry','MEDIA_VERIFICATION_UNAVAILABLE')
        except (MediaInvalid,AcquisitionConflict,MetadataInvalid,SourceMissing) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))

    def route(value):
        return () if unlock_kind is None else (
            Successor.create(unlock_kind,{
                'verification_job_id':value['verification_job_id']}),)
    return Registration(KIND,DEFINITION,execute,persist=_persist,route=route,
                        successor_kinds=() if unlock_kind is None else (unlock_kind,))
