"""Read-only selected-video verification; staging remains a separate component."""
from functools import partial
from src.anime.acquisition.media import verify_file, MediaInvalid, MediaUnavailable, ProbeUnavailable, AudioMissing
from src.anime.acquisition.metadata import AcquisitionConflict, MetadataInvalid, SourceMissing
from src.anime.acquisition.settings import SizeEvidenceMissing, DownloadConfigurationError
from src.anime.acquisition.submission import ClientInvalid
from src.anime.acquisition.transfers import TransferConflict
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import CompletionRejected, Outcome, Registration, Successor, failure_facts
from src.anime.storage.media_verification import load, persist
from src.anime.worker import blocking

KIND='VERIFY_MEDIA'
DEFINITION=HandlerDefinition(KIND,'Verify downloaded video and required audio')
REVIEW=(MediaInvalid,AcquisitionConflict,MetadataInvalid,SourceMissing,SizeEvidenceMissing,
        DownloadConfigurationError,ClientInvalid,TransferConflict)


def _persist(db,claim,value):
    try:
        if type(value) is not dict:raise MediaInvalid()
        value=dict(value)
        value.pop('transcode',None)
        if type(value) is not dict or set(value)!={'media','verification_job_id'} or value['verification_job_id']!=claim.job_id:raise MediaInvalid()
        persist(db,claim,value['media'])
    except MediaInvalid as exc:raise CompletionRejected(Outcome('fail',exc.reason_code, facts=failure_facts(exc))) from None
    except REVIEW as exc:raise CompletionRejected(Outcome('review',exc.reason_code, facts=failure_facts(exc))) from None


def registration(*,verify=verify_file,load_input=load,staging_kind=None,transcode_kind=None):
    if staging_kind is not None and (type(staging_kind) is not str or not staging_kind.strip()
            or len(staging_kind)>64 or staging_kind==KIND):raise ValueError('invalid staging successor')
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'preparation_job_id','download_job_id'} or context.claim.event_id is None:raise ValueError()
            preparation=integer(payload['preparation_job_id'],positive=True)
            download=integer(payload['download_job_id'],positive=True)
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            intent,record=await blocking(load_input,context.configuration.value('storage.database_path'),
                                        context.claim,preparation,download)
            work=partial(verify,intent,record,download,context.cancelled,protected_roots=(
                context.configuration.value('storage.user_evaluation_root'),context.configuration.value('storage.keep_root')),
                audio=context.configuration.value('acquisition.audio_check'))
            value=await blocking(work)
            result={'media':value,'verification_job_id':context.claim.job_id}
            if transcode_kind is not None:
                result['transcode']=context.configuration.value('transcode.mode')!='off'
            return Outcome(value=result)
        except MediaUnavailable as exc:return Outcome('retry',exc.reason_code)
        except ProbeUnavailable as exc:return Outcome('review',exc.reason_code)
        except AudioMissing as exc:
            obs = ','.join(exc.observed) if exc.observed else 'none'
            mis = ','.join(exc.missing) if exc.missing else 'none'
            facts = {'observed_audio': obs, 'missing_audio': mis}
            return Outcome('fail', exc.reason_code, facts=facts)
        except MediaInvalid as exc:
            return Outcome('fail', exc.reason_code, facts=failure_facts(exc))
        except REVIEW as exc:return Outcome('review',exc.reason_code, facts=failure_facts(exc))
    def route(value):
        target=transcode_kind if value.get('transcode') else staging_kind
        return () if target is None else (Successor.create(target,{'verification_job_id':value['verification_job_id']}),)
    return Registration(KIND,DEFINITION,execute,persist=_persist,route=route,
        successor_kinds=tuple(k for k in (staging_kind,transcode_kind) if k is not None))
