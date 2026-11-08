"""Independently prove every approved batch episode's real JP/EN audio and hash."""
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
from src.nextgen.batch.media import verify_batch_episode_file
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import (
    CompletionRejected, Outcome, Registration, Successor, failure_facts,
)
from src.nextgen.storage.batch_episode_source import BatchEpisodeSource
from src.nextgen.storage.batch_episode_media import persist_batch_episode_media
from src.nextgen.worker import blocking


KIND='VERIFY_BATCH_EPISODE'
DEFINITION=HandlerDefinition(KIND,'Verify batch episode audio and content')


def _persist(db,claim,value):
    try:
        if (type(value) is not dict or set(value)!={
                'verification_job_id','file_index','media'}
                or value['verification_job_id']!=claim.job_id
                or type(value['media']) is not VerifiedMedia):
            raise MediaInvalid()
        persist_batch_episode_media(db,claim,value['file_index'],value['media'])
    except (MediaInvalid,AcquisitionConflict) as exc:
        raise CompletionRejected(Outcome('review',exc.reason_code, facts=failure_facts(exc))) from None


def registration(read_batch_metainfo,*,source_factory=BatchEpisodeSource,
                 verify=verify_batch_episode_file,staging_kind=None):
    if not inspect.iscoroutinefunction(read_batch_metainfo):
        raise ValueError('Trusted async metadata reader required')
    if staging_kind is not None and (type(staging_kind) is not str
            or not staging_kind.strip() or len(staging_kind)>64
            or staging_kind==KIND):
        raise ValueError('Invalid batch staging successor')

    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'unlock_job_id','file_index'}:
                raise ValueError()
            unlock=integer(payload['unlock_job_id'],positive=True)
            file_index=integer(payload['file_index'])
        except (TypeError,ValueError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        source=source_factory(context.configuration.value('storage.database_path'))
        try:
            digest=await blocking(source.source_hash,context.claim,unlock,file_index)
            raw=await read_batch_metainfo(digest,context.configuration,context.cancelled)
            inventory=await blocking(read_batch_inventory,raw,digest)
            intent,record=await blocking(
                source.load,context.claim,unlock,file_index,inventory)
            protected=(
                context.configuration.value('storage.trial_root'),
                context.configuration.value('storage.keep_root'),
                context.configuration.value('storage.database_path'),
            )
            media=await blocking(verify,intent,record,unlock,file_index,
                                 context.cancelled,protected_roots=protected,
                                 audio=context.configuration.value('acquisition.audio_check'))
            return Outcome(value={'verification_job_id':context.claim.job_id,
                                  'file_index':file_index,'media':media})
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
        return () if staging_kind is None else (Successor.create(staging_kind,{
            'verification_job_id':value['verification_job_id']}),)
    return Registration(KIND,DEFINITION,execute,persist=_persist,route=route,
                        successor_kinds=() if staging_kind is None else (staging_kind,))
