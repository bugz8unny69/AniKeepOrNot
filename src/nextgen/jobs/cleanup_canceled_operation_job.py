"""Cleanup control job, deliberately outside the canceled target scope."""
from src.nextgen.async_io import blocking
from src.nextgen.cancellation_service import CancellationService
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration
from src.nextgen.storage.management_actions import ManagementActions, ActionUnavailable
from src.nextgen.storage.sqlite import transaction

KIND='CLEANUP_CANCELED_OPERATION'
SERVICES=('qbittorrent',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Clean up canceled work')


def _persist(db,claim,value):
    row=db.execute('SELECT o.state,o.cleanup_state FROM management_actions a '
        'JOIN job_operations o ON o.operation_id=a.operation_id '
        "WHERE a.job_id=? AND a.action='CLEANUP' AND a.target_job_id=?",(claim.job_id,value)).fetchone()
    if row!=('CANCELED','COMPLETE'):
        raise CompletionRejected(Outcome('review','CANCELLATION_REVIEW'))


def registration(secrets,*,service_factory=CancellationService):
    async def execute(context):
        actions=ManagementActions(context.configuration.value('storage.database_path'))
        try:target=await blocking(actions.bind,context.claim,'CLEANUP')
        except (ActionUnavailable,ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        def guard(db=None):
            if db is not None:
                actions.queue._owns(db,context.claim,actions.queue.now())
            else:
                with actions.queue.connection() as connection,transaction(connection):
                    actions.queue._owns(connection,context.claim,actions.queue.now())
        service=service_factory(context.configuration,secrets,logger=context.logger,admitted=True,guard=guard,component="worker")
        status=await blocking(service.cleanup,target)
        if status['cleanup_state']=='COMPLETE':return Outcome(value=target)
        if status['cleanup_state']=='REVIEW':
            # Name the failed check on this job's own History, not only the target's.
            facts={k:status[k] for k in ('failed_check','failed_path') if status.get(k)}
            return Outcome('review','CANCELLATION_REVIEW',facts=facts or None)
        # A bounded reconciliation pass frees a slot. Waiting is not retry failure
        # and never rearms an uncertain external cleanup command.
        return Outcome('defer',status['reason_code'] or 'CANCELLATION_PENDING',delay_ms=30000)
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist)
