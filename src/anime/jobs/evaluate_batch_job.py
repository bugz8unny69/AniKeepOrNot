"""EVALUATE_BATCH: durable trial intent, no torrent acquisition side effects."""
from dataclasses import dataclass

from src.anime.acquisition.settings import SizeLimitExceeded, SizeEvidenceMissing, size_limits
from src.anime.batch.user_evaluation import BatchUserEvaluationProposal, propose_batch_user_evaluation
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import CompletionRejected, Outcome, Registration, Successor
from src.anime.storage.batch_user_evaluations import load_batch_user_evaluation_source, persist_batch_user_evaluation
from src.anime.user_evaluation_policy import Window, UserEvaluationConflict
from src.anime.worker import blocking

KIND='EVALUATE_BATCH'
DEFINITION=HandlerDefinition(KIND,'Plan verified batch episodes')


@dataclass(frozen=True)
class BatchEvaluationCompletion:
    proposal: BatchUserEvaluationProposal
    evaluation_job_id: int


def _persist(db,claim,value):
    if type(value) is not BatchEvaluationCompletion or value.evaluation_job_id!=claim.job_id:
        raise ValueError('Batch trial completion does not match its job')
    from src.anime.storage.review_decisions import require_allowed, AnimeRejected
    try:
        require_allowed(db,value.proposal.source.anidb_id)
        return (persist_batch_user_evaluation(db,claim,value.proposal),)
    except AnimeRejected as exc:
        raise CompletionRejected(Outcome('hold',exc.reason_code)) from None
    except (UserEvaluationConflict,SizeLimitExceeded,SizeEvidenceMissing) as exc:
        raise CompletionRejected(Outcome('review',exc.reason_code)) from None


def registration(*, reservation_kind=None):
    if reservation_kind is not None and (type(reservation_kind) is not str
            or not reservation_kind.strip() or len(reservation_kind)>64
            or reservation_kind==KIND):
        raise ValueError('invalid batch reservation successor')
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'verification_job_id'} or context.claim.event_id is None:
                raise ValueError()
            job=integer(payload['verification_job_id'],positive=True)
        except (ValueError,TypeError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            source=await blocking(load_batch_user_evaluation_source,
                context.configuration.value('storage.database_path'),job,context.claim.event_id)
            window=Window(context.configuration.value('user_evaluation.backfill_count'),
                          context.configuration.value('user_evaluation.following_count'))
            proposal=propose_batch_user_evaluation(source,window,size_limits(context.configuration),
                                          context.configuration.desired_revision)
        except (UserEvaluationConflict,SizeLimitExceeded,SizeEvidenceMissing) as exc:
            return Outcome('review',exc.reason_code)
        return Outcome(value=BatchEvaluationCompletion(proposal,context.claim.job_id))
    def route(value):
        return () if reservation_kind is None else (
            Successor.create(reservation_kind,{'evaluation_job_id':value.evaluation_job_id}),)
    return Registration(KIND,DEFINITION,execute,persist=_persist,route=route,
                        successor_kinds=() if reservation_kind is None else (reservation_kind,))
