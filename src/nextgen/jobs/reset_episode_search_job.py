"""Worker-side episode reset; always wait for owned cleanup before searching."""
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration
from src.nextgen.storage.episode_reset import EpisodeResetUnavailable, finish, progress
from src.nextgen.worker import blocking

KIND = 'RESET_EPISODE_SEARCH'
DEFINITION = HandlerDefinition(KIND, 'Clean failed episode attempts and search again')


def registration():
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'plan_id','episode'} or context.claim.event_id is None:
                raise ValueError()
            plan_id=integer(payload['plan_id'],positive=True)
            episode=integer(payload['episode'],positive=True)
        except (TypeError,ValueError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            result=await blocking(
                progress,context.configuration.value('storage.database_path'),plan_id,episode)
        except EpisodeResetUnavailable:
            return Outcome('review','CANCELLATION_REVIEW')
        if result=='REVIEW':
            return Outcome('review','CANCELLATION_REVIEW')
        if result!='READY':
            return Outcome('defer','CANCELLATION_PENDING',delay_ms=30000)
        return Outcome(value=(plan_id,episode))

    def persist(db,claim,value):
        if (type(value) is not tuple or len(value)!=2
                or any(type(x) is not int or x<=0 for x in value)):
            raise CompletionRejected(Outcome('review','JOB_PAYLOAD_INVALID'))
        try:
            return finish(db,claim,*value)
        except EpisodeResetUnavailable:
            raise CompletionRejected(Outcome('review','CANCELLATION_REVIEW')) from None

    return Registration(KIND,DEFINITION,execute,persist=persist)
