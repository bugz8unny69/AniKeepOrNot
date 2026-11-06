"""Worker-side episode reset; always wait for owned cleanup before searching."""
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration
from src.nextgen.storage.episode_reset import EpisodeResetUnavailable, Review, finish, progress
from src.nextgen.worker import blocking

KIND = 'RESET_EPISODE_SEARCH'
DEFINITION = HandlerDefinition(KIND, 'Clean failed episode attempts and search again')


def _review(review):
    """CANCELLATION_REVIEW naming the rule that stopped the reset and, when an
    owner's cleanup stopped, the cleanup job to inspect and retry instead."""
    facts=review.facts() if isinstance(review,Review) else None
    return Outcome('review','CANCELLATION_REVIEW',facts=facts or None)


def registration():
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'plan_id','episode','mode'} or context.claim.event_id is None:
                raise ValueError()
            plan_id=integer(payload['plan_id'],positive=True)
            episode=integer(payload['episode'],positive=True)
            mode=payload['mode']
            if mode not in ('search','replay'): raise ValueError()
        except (TypeError,ValueError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            result=await blocking(
                progress,context.configuration.value('storage.database_path'),plan_id,episode,
                job_id=context.claim.job_id)
        except EpisodeResetUnavailable as exc:
            return _review(exc.review)
        if result=='REVIEW':
            return _review(result)
        if result!='READY':
            return Outcome('defer','CANCELLATION_PENDING',delay_ms=30000)
        return Outcome(value=(plan_id,episode,mode))

    def persist(db,claim,value):
        if (type(value) is not tuple or len(value)!=3
                or any(type(x) is not int or x<=0 for x in value[:2])
                or value[2] not in ('search','replay')):
            raise CompletionRejected(Outcome('review','JOB_PAYLOAD_INVALID'))
        try:
            return finish(db,claim,*value)
        except EpisodeResetUnavailable as exc:
            raise CompletionRejected(_review(exc.review)) from None

    return Registration(KIND,DEFINITION,execute,persist=persist)
