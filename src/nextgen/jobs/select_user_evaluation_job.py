"""Evaluate immutable trial windows; no downloads, media or permanent Keep."""
from dataclasses import dataclass
import time

from src.nextgen.acquisition.audio import dual_audio_tagged, wants_english

from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.history import append_job_history
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration, Successor
from src.nextgen.storage.user_evaluations import load_user_evaluation_source, persist_user_evaluation
from src.nextgen.user_evaluation_policy import Window, UserEvaluationProposal, UserEvaluationConflict
from src.nextgen.worker import blocking

KIND='SELECT_USER_EVALUATION'
DEFINITION=HandlerDefinition(KIND,'Evaluate Intake episode selection')


@dataclass(frozen=True)
class EvaluationCompletion:
    proposal: UserEvaluationProposal
    evaluation_job_id: int
    # English audio required and the autobrr title has no audio tag: do not
    # download this delivery; its episode is searched in Prowlarr instead.
    audio_search: bool = False


def _persist(db,claim,value,*,discovery_kind=None):
    if type(value) is not EvaluationCompletion or value.evaluation_job_id!=claim.job_id:
        raise ValueError('episode selection completion does not match its job')
    from src.nextgen.storage.review_decisions import require_allowed, AnimeRejected
    try:
        require_allowed(db,value.proposal.source.anidb_id)
        receipts=(persist_user_evaluation(db,claim,value.proposal),)
        if value.audio_search:
            # Written before the first search is scheduled: the search window
            # reads this event to include the anchor episode.
            from src.nextgen.storage.episode_discovery import AUDIO_SEARCH_EVENT
            receipts+=(append_job_history(db,job_id=claim.job_id,event_code=AUDIO_SEARCH_EVENT,
                reason_code='RELEASE_AUDIO_UNTAGGED',component='worker',
                occurred_at_ms=time.time_ns()//1000000,details={'attempt':claim.attempt}),)
        if discovery_kind is not None:
            from src.nextgen.storage.episode_discovery import schedule_first, BackfillConflict
            try: receipts+=schedule_first(db,claim,value.proposal.source,kind=discovery_kind)
            except BackfillConflict:raise CompletionRejected(Outcome('review','EPISODE_DISCOVERY_PLAN_CONFLICT')) from None
        return receipts
    except AnimeRejected as exc:raise CompletionRejected(Outcome('hold',exc.reason_code)) from None
    except UserEvaluationConflict as exc:raise CompletionRejected(Outcome('review',exc.reason_code)) from None


def registration(*, reservation_kind=None, discovery_kind=None):
    if reservation_kind is not None and (type(reservation_kind) is not str or not reservation_kind.strip()
                                         or len(reservation_kind)>64 or reservation_kind==KIND):
        raise ValueError('invalid episode reservation successor')
    if discovery_kind is not None and (type(discovery_kind) is not str or not discovery_kind.strip()
                                       or len(discovery_kind)>64 or discovery_kind in (KIND,reservation_kind)):
        raise ValueError('invalid Sonarr discovery registration')
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'verification_job_id'} or context.claim.event_id is None:raise ValueError()
            job=integer(payload['verification_job_id'],positive=True)
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            source,(origin,title)=await blocking(load_user_evaluation_source,context.configuration.value('storage.database_path'),
                                                 job,context.claim.event_id,with_delivery=True)
            value=UserEvaluationProposal(source,Window(context.configuration.value('user_evaluation.backfill_count'),
                                             context.configuration.value('user_evaluation.following_count')),
                                context.configuration.desired_revision)
        except UserEvaluationConflict:return Outcome('review','USER_EVALUATION_PLAN_CONFLICT')
        # Gate the webhook release; Prowlarr uses the same eligibility rule.
        audio_search=(discovery_kind is not None and origin=='autobrr'
                      and wants_english(context.configuration.value('acquisition.audio_check'))
                      and not dual_audio_tagged(title))
        return Outcome(value=EvaluationCompletion(value,context.claim.job_id,audio_search))

    def route(value):
        if type(value) is not EvaluationCompletion:raise ValueError('typed episode selection completion required')
        return () if reservation_kind is None or value.audio_search else (Successor.create(reservation_kind,{'evaluation_job_id':value.evaluation_job_id}),)

    return Registration(KIND,DEFINITION,execute,persist=lambda db,claim,value:_persist(db,claim,value,discovery_kind=discovery_kind),route=route,
                        successor_kinds=() if reservation_kind is None else (reservation_kind,))
