"""Verify a declared episode through providers; this job never acquires content."""
import inspect
import time
from dataclasses import dataclass

from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration, Successor, failure_facts
from src.nextgen.providers.errors import (ProviderDeferred, ProviderUnavailable, ProviderInvalidResponse,
                                          ProviderAuthenticationFailed)
from src.nextgen.release_contracts import VerifiedRelease, ReleaseNotReady, ReleaseConflict
from src.nextgen.batch.provider_worker import BatchVerificationCompletion, BatchCandidatePooled
from src.nextgen.batch.selection import BatchPlanningError
from src.nextgen.acquisition.metadata import MetadataInvalid, SourceMissing
from src.nextgen.storage.batch_verifications import persist_batch_verification
from src.nextgen.acquisition.metainfo_source import (
    MetadataSourceWaiting, MetadataSourceUnavailable,
    MetadataSourceConfigurationError, MetadataSourceDenied,
    MetadataSourceTimeout,
)
from src.nextgen.storage.releases import load_release, persist_release
from src.nextgen.worker import blocking

KIND="VERIFY_RELEASE"
DEFINITION=HandlerDefinition(KIND,"Verify release episode")


@dataclass(frozen=True)
class ReleaseCompletion:
    verified: VerifiedRelease | BatchVerificationCompletion | BatchCandidatePooled
    verification_job_id: int


def _persist(db,claim,value):
    if type(value) is not ReleaseCompletion or value.verification_job_id!=claim.job_id:
        raise ValueError("release completion does not match its job")
    try:
        if type(value.verified) is BatchCandidatePooled:
            # A higher-ranked candidate nothing is running gets activated and
            # scheduled here, atomically with this job's completion (WI-11).
            pooled=value.verified
            if pooled.winner_candidate_id is None:
                return ()
            from src.nextgen.storage.batch_candidates import schedule_winner
            return schedule_winner(db,pooled.scope_id,pooled.winner_candidate_id,
                                   created_by_job_id=claim.job_id,now_ms=time.time_ns()//1000000)
        elif type(value.verified) is BatchVerificationCompletion:
            result=value.verified
            persist_batch_verification(db,claim,result.source,result.inventory,result.verified,
                                       now_ms=time.time_ns()//1000000)
        else:
            persist_release(db,claim,value.verified)
    except (ReleaseNotReady,ReleaseConflict,MetadataInvalid,BatchPlanningError) as exc:
        raise CompletionRejected(Outcome("review",getattr(exc,'reason_code','RELEASE_CONFLICT'), facts=failure_facts(exc))) from None


def registration(verify, *, trial_kind=None, batch_kind=None):
    if not inspect.iscoroutinefunction(verify): raise ValueError("async release verifier required")
    if trial_kind is not None and (type(trial_kind) is not str or not trial_kind.strip()
                                   or len(trial_kind)>64 or trial_kind==KIND):
        raise ValueError("invalid trial evaluation successor")

    if batch_kind is not None and (type(batch_kind) is not str or not batch_kind.strip()
                                   or len(batch_kind)>64 or batch_kind in (KIND,trial_kind)):
        raise ValueError('invalid batch trial successor')

    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={"anidb_id"} or context.claim.event_id is None:
                raise ValueError()
            aid=integer(payload["anidb_id"],positive=True)
        except (ValueError,TypeError): return Outcome("review","JOB_PAYLOAD_INVALID")
        try:
            source=await blocking(load_release,context.configuration.value("storage.database_path"),context.claim.event_id,aid)
            if 'claim' in inspect.signature(verify).parameters:
                result=await verify(source,context.configuration,context.cancelled,
                                    claim=context.claim)
            else:
                result=await verify(source,context.configuration,context.cancelled)
        except MetadataSourceWaiting as exc: return Outcome("defer",exc.reason_code,delay_ms=exc.delay_ms,
                                                   metadata_evidence=exc.evidence)
        except MetadataSourceTimeout as exc: return Outcome("fail", exc.reason_code, facts=failure_facts(exc))
        except MetadataSourceUnavailable as exc: return Outcome("retry",exc.reason_code)
        except (MetadataSourceConfigurationError,MetadataSourceDenied) as exc:
            return Outcome("review",exc.reason_code, facts=failure_facts(exc))
        except ProviderDeferred as exc: return Outcome("defer","JOB_WAITING",delay_ms=exc.delay_ms)
        except (ProviderUnavailable,ProviderInvalidResponse) as exc: return Outcome("retry",exc.reason_code)
        except ProviderAuthenticationFailed as exc: return Outcome("review","PROVIDER_AUTH_FAILED", facts=failure_facts(exc))
        except (ReleaseNotReady,ReleaseConflict,MetadataInvalid,SourceMissing) as exc: return Outcome("review",exc.reason_code, facts=failure_facts(exc))
        if type(result) not in (VerifiedRelease,BatchVerificationCompletion,BatchCandidatePooled) or result.source!=source:
            return Outcome("review","RELEASE_CONFLICT")
        return Outcome(value=ReleaseCompletion(result,context.claim.job_id))

    def route(value):
        if type(value) is not ReleaseCompletion:raise ValueError("typed release completion required")
        if type(value.verified) is BatchCandidatePooled:
            return ()
        if type(value.verified) is BatchVerificationCompletion:
            return () if batch_kind is None else (Successor.create(batch_kind,
                {'verification_job_id':value.verification_job_id}),)
        return () if trial_kind is None else (Successor.create(trial_kind,
            {'verification_job_id':value.verification_job_id}),)

    # Batch trial is a read-only plan; no acquisition job succeeds it.
    successor_kinds=tuple(k for k in (trial_kind,batch_kind) if k is not None)
    return Registration(KIND,DEFINITION,execute,persist=_persist,route=route,
                        successor_kinds=successor_kinds)
