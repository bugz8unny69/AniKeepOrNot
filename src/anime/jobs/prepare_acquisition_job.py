"""Inspect hash-bound source files and persist an intent; no torrent submission."""
import inspect

from src.anime.acquisition.settings import SizeLimitExceeded, SizeEvidenceMissing, size_limits
from src.anime.acquisition.contracts import AcquisitionProposal
from src.anime.acquisition.metadata import AcquisitionConflict, MetadataInvalid, SourceMissing, parse_metainfo
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import CompletionRejected, Outcome, Registration, Successor, failure_facts
from src.anime.providers.errors import ProviderDeferred, ProviderUnavailable
from src.anime.acquisition.metainfo_source import (MetadataSourceWaiting, MetadataSourceUnavailable,
    MetadataSourceConfigurationError, MetadataSourceDenied)
from src.anime.storage.acquisition import load_acquisition_input, persist_acquisition, validate_declaration
from src.anime.worker import blocking

KIND='PREPARE_ACQUISITION'
SERVICES=('qbittorrent',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Inspect source files and prepare acquisition')


def _persist(db,claim,value):
    if type(value) is not AcquisitionProposal or value.preparation_job_id not in (None,claim.job_id):
        raise ValueError('preparation facts do not match job')
    try:return (persist_acquisition(db,claim,value),)
    except (SizeLimitExceeded,MetadataInvalid) as exc:
        raise CompletionRejected(Outcome('fail',exc.reason_code,facts=getattr(exc,'facts',None) or failure_facts(exc))) from None
    except (AcquisitionConflict,SourceMissing,SizeEvidenceMissing) as exc:
        raise CompletionRejected(Outcome('review',exc.reason_code,facts=getattr(exc,'facts',None) or failure_facts(exc))) from None


def registration(read_metainfo,*,submission_kind=None,legacy_bootstrap=None):
    if submission_kind is not None and (type(submission_kind) is not str or not submission_kind.strip()
                                      or len(submission_kind)>64 or submission_kind==KIND):
        raise ValueError('invalid torrent submission successor')
    # Trusted service callback, never selected by a webhook/job payload.
    if not inspect.iscoroutinefunction(read_metainfo):raise ValueError('async metadata reader required')
    if legacy_bootstrap is not None and not inspect.iscoroutinefunction(legacy_bootstrap):
        raise ValueError('trusted async legacy bootstrap required')
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'evaluation_job_id'} or context.claim.event_id is None:raise ValueError()
            job=integer(payload['evaluation_job_id'],positive=True)
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            source=await blocking(load_acquisition_input,context.configuration.value('storage.database_path'),job,context.claim.event_id)
            # Reconcile any original magnet admission before a new metadata path.
            if legacy_bootstrap is not None:
                outcome=await legacy_bootstrap(source,context)
                if outcome is not None:
                    if type(outcome) is not Outcome:raise ValueError('invalid bootstrap result')
                    return outcome
            raw=await read_metainfo(source,context.configuration,context.cancelled)
            manifest=await blocking(parse_metainfo,raw,source.info_hash)
            validate_declaration(source,manifest)
        except MetadataSourceWaiting as exc:return Outcome('defer',exc.reason_code,delay_ms=exc.delay_ms)
        except MetadataSourceUnavailable as exc:return Outcome('retry',exc.reason_code,facts=failure_facts(exc))
        except (MetadataSourceConfigurationError,MetadataSourceDenied) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
        except ProviderDeferred as exc:return Outcome('defer','JOB_WAITING',delay_ms=exc.delay_ms)
        except ProviderUnavailable as exc:return Outcome('retry',exc.reason_code)
        except (SizeLimitExceeded,MetadataInvalid) as exc:
            return Outcome('fail',exc.reason_code,facts=getattr(exc,'facts',None) or failure_facts(exc))
        except (AcquisitionConflict,SourceMissing,SizeEvidenceMissing) as exc:
            return Outcome('review',exc.reason_code,facts=getattr(exc,'facts',None) or failure_facts(exc))
        return Outcome(value=AcquisitionProposal(source,manifest,context.configuration.desired_revision,size_limits(context.configuration),context.claim.job_id))
    def route(value):
        if submission_kind is None:return ()
        if value.preparation_job_id is None:raise ValueError('preparation job identity required')
        return (Successor.create(submission_kind,{'preparation_job_id':value.preparation_job_id}),)
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist,route=route,
                        successor_kinds=() if submission_kind is None else (submission_kind,))
