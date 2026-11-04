"""Reserve verified batch episodes and route owned jobs to stopped submission.

The successor only receives the reservation job ID; all file selections and
metadata are reloaded from the authenticated database before any qB mutation.
"""
from dataclasses import dataclass
from src.nextgen.episode_reservations import ReservationConflict, EpisodeReserved
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration, Successor
from src.nextgen.storage.batch_reservations import (
    BatchReservationInput, load_batch_reservation, persist_batch_reservations,
)
from src.nextgen.worker import blocking

KIND='RESERVE_BATCH'
DEFINITION=HandlerDefinition(KIND,'Reserve verified batch episodes')


@dataclass(frozen=True)
class BatchReservationCompletion:
    source: BatchReservationInput
    reservation_job_id: int


def _persist(db,claim,value):
    if (type(value) is not BatchReservationCompletion
            or value.reservation_job_id!=claim.job_id):
        raise ValueError('Batch reservation completion does not match the claiming job')
    from src.nextgen.storage.review_decisions import require_allowed, AnimeRejected
    try:
        require_allowed(db,value.source.anidb_id)
        return (persist_batch_reservations(db,claim,value.source),)
    except AnimeRejected as exc:
        raise CompletionRejected(Outcome('hold',exc.reason_code)) from None
    except EpisodeReserved as exc:
        facts = {}
        if getattr(exc, 'owner_job_id', None) is not None:
            facts['blocking_job_id'] = exc.owner_job_id
        if getattr(exc, 'operation_id', None) is not None:
            facts['operation_id'] = exc.operation_id
        raise CompletionRejected(Outcome('hold', exc.reason_code, facts=facts or None)) from None
    except ReservationConflict as exc:
        from src.nextgen.job_contracts import failure_facts
        raise CompletionRejected(Outcome('review', exc.reason_code, facts=failure_facts(exc))) from None


def registration(*, submission_kind=None):
    if submission_kind is not None and (type(submission_kind) is not str
            or not submission_kind.strip() or len(submission_kind)>64
            or submission_kind==KIND):
        raise ValueError('invalid batch submission successor')
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'evaluation_job_id'} or context.claim.event_id is None:
                raise ValueError()
            job=integer(payload['evaluation_job_id'],positive=True)
        except (ValueError,TypeError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            result=await blocking(
                load_batch_reservation,
                context.configuration.value('storage.database_path'),
                job,context.claim.event_id)
        except ReservationConflict:
            return Outcome('review','EPISODE_RESERVATION_CONFLICT')
        return Outcome(value=BatchReservationCompletion(result,context.claim.job_id))
    def route(value):
        return () if submission_kind is None else (
            Successor.create(submission_kind,{'reservation_job_id':value.reservation_job_id}),)
    return Registration(KIND,DEFINITION,execute,persist=_persist,route=route,
                        successor_kinds=() if submission_kind is None else (submission_kind,))
