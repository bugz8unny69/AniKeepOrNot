"""Bounded, best-effort bulk management; actual actions reuse single-job admission."""
from src.nextgen.async_io import blocking
from src.nextgen.diagnostics import log_history
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.job_contracts import Outcome, Registration
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.management_actions import ManagementActions, ActionUnavailable
from src.nextgen.storage.review_retry import request_review_retry, RetryNotAllowed, RetryStale

KIND = 'BULK_MANAGE_JOBS'
DEFINITION = HandlerDefinition(KIND, 'Apply bulk job actions')


def registration():
    async def execute(context):
        payload = context.data()
        if (set(payload) != {'action', 'job_ids'}
                or type(payload['action']) is not str
                or payload['action'] not in ('retry', 'cancel', 'cleanup')
                or type(payload['job_ids']) is not list
                or not 6 <= len(payload['job_ids']) <= 100
                or any(type(job) is not int or not 1 <= job <= (1 << 63)-1
                       for job in payload['job_ids'])
                or len(set(payload['job_ids'])) != len(payload['job_ids'])):
            return Outcome('cancel', 'JOB_PAYLOAD_INVALID')
        action,ids=payload['action'],payload['job_ids']
        database=context.configuration.value('storage.database_path')

        def apply():
            queue=JobQueue(database)
            management=ManagementActions(database)
            rejected=0
            for job in ids:
                try:
                    if action=='retry':
                        with queue.connection() as db:
                            snapshot=db.execute('SELECT attempt_count,last_reason_code FROM jobs WHERE job_id=?',(job,)).fetchone()
                        if snapshot is None:raise RetryNotAllowed()
                        request_review_retry(database,job,expected_attempt=snapshot[0],expected_reason=snapshot[1])
                    else:
                        _,_,audits=management.submit(job,action)
                        for record in audits:log_history(context.logger,record)
                except (ActionUnavailable,RetryNotAllowed,RetryStale,ValueError):
                    # Independent targets: continue; successful transitions stay durable.
                    rejected+=1
            return rejected

        rejected=await blocking(apply)
        return Outcome('cancel','BULK_MANAGEMENT_PARTIAL') if rejected else Outcome()

    return Registration(KIND,DEFINITION,execute)
