"""Owned batch torrent cleanup using the immutable submitted inventory."""
from src.nextgen.async_io import blocking
from src.nextgen.acquisition.metadata import MetadataInvalid, AcquisitionConflict
from src.nextgen.acquisition.submission import (
    ClientUnavailable, ClientInvalid, ClientAuthenticationFailed, ClientUnsupported,
)
from src.nextgen.batch_decision_cleanup import BatchDecisionCleanup
from src.nextgen.cancellation import CancellationError, CleanupBusy
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration
from src.nextgen.storage.sqlite import enqueue_job

KIND='CLEANUP_BATCH_DOWNLOAD'
SERVICES=('qbittorrent',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Reconcile and remove owned completed batch torrent')


def enqueue(db,claim,*,decision=None):
    decision=claim.job_id if decision is None else integer(decision,positive=True)
    return enqueue_job(
        db,kind=KIND,component='worker',
        payload={'decision_job_id':decision},
        event_id=claim.event_id,created_by_job_id=claim.job_id)


STAGING_CLEANUP_DELAY_MS=60_000


def staged_cleanup(db,plan_id):
    """The cleanup queued when this pack finished staging, if any."""
    rows=db.execute(
        "SELECT c.job_id FROM jobs c JOIN jobs d "
        "ON d.job_id=json_extract(c.payload_json,'$.decision_job_id') "
        "WHERE c.kind=? AND json_extract(c.payload_json,'$.plan_id')=? "
        "AND d.kind='STAGE_BATCH_EPISODE'",(KIND,plan_id)).fetchall()
    if len(rows)>1:raise CancellationError()
    return rows[0][0] if rows else None


def enqueue_after_staging(db,claim,plan_id):
    """Inside the last STAGE_BATCH_EPISODE completion, exactly once."""
    import time
    if staged_cleanup(db,plan_id) is not None:raise CancellationError()
    return enqueue_job(
        db,kind=KIND,component='worker',
        payload={'decision_job_id':claim.job_id,'plan_id':plan_id},
        event_id=claim.event_id,created_by_job_id=claim.job_id,
        available_at_ms=time.time_ns()//1_000_000+STAGING_CLEANUP_DELAY_MS)


def _persist(db,claim,value):
    if type(value) is not dict or set(value) not in (
            {'decision_job_id'}, {'decision_job_id', 'plan_id'}):
        raise CompletionRejected(Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW'))
    decision=integer(value['decision_job_id'],positive=True)
    plan = integer(value['plan_id'],positive=True) if 'plan_id' in value else None
    if plan is not None:
        row = db.execute(
            "SELECT c.phase,j.state,j.kind,k.created_by_job_id "
            "FROM batch_download_cleanup c JOIN jobs j ON j.job_id=c.decision_job_id "
            "JOIN jobs k ON k.job_id=? WHERE c.decision_job_id=? AND c.plan_id=?",
            (claim.job_id,decision,plan)).fetchone()
        if (row is None or row[0]!='COMPLETE'
                or row[2] not in ('REJECT_SERIES','STAGE_BATCH_EPISODE')
                or row[3]!=decision):
            raise CompletionRejected(Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW'))
        return
    row=db.execute(
        "SELECT c.phase,j.state,j.kind,k.created_by_job_id,k.event_id "
        "FROM batch_download_cleanup c "
        "JOIN jobs j ON j.job_id=c.decision_job_id "
        "JOIN jobs k ON k.job_id=? "
        "WHERE c.decision_job_id=?",
        (claim.job_id,decision)).fetchone()
    if (row is None or row[0]!='COMPLETE' or row[1]!='DONE'
            or row[2]!='KEEP_BATCH'
            or row[4]!=claim.event_id):
        raise CompletionRejected(Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW'))
    if row[2]=='KEEP_BATCH':
        valid=db.execute(
            "SELECT 1 FROM batch_keep_cleanup b JOIN jobs j ON j.job_id=b.job_id "
            "WHERE b.decision_job_id=? AND b.phase='COMPLETE' "
            "AND b.job_id=? AND j.kind='CLEANUP_BATCH_KEPT_MEDIA' AND j.state='DONE'",
            (decision,row[3])).fetchone()
        if valid is None:
            raise CompletionRejected(Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW'))
    elif row[3]!=decision:
        raise CompletionRejected(Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW'))


def registration(secrets,*,service_factory=BatchDecisionCleanup):
    async def execute(context):
        payload=context.data()
        try:
            if set(payload) not in ({'decision_job_id'}, {'decision_job_id','plan_id'}):
                raise ValueError()
            decision=integer(payload['decision_job_id'],positive=True)
            plan=integer(payload['plan_id'],positive=True) if 'plan_id' in payload else None
        except (ValueError,TypeError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        try:
            service=service_factory(context.configuration,secrets)
            if plan is None:
                complete=await blocking(service.run,context.claim,decision,context.cancelled)
            else:
                complete=await blocking(
                    service.run,context.claim,decision,context.cancelled,plan_id=plan)
            return (Outcome(value=payload) if complete
                    else Outcome('defer','DECISION_DOWNLOAD_CLEANUP_WAITING',
                                 delay_ms=30000))
        except CleanupBusy:
            return Outcome('defer','DECISION_DOWNLOAD_CLEANUP_WAITING',
                           delay_ms=1000)
        except ClientUnavailable:
            return Outcome('retry','DECISION_DOWNLOAD_CLEANUP_UNAVAILABLE')
        except (CancellationError,ClientInvalid,ClientAuthenticationFailed,
                ClientUnsupported,MetadataInvalid,AcquisitionConflict):
            return Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW')
        except OSError:
            return Outcome('retry','DECISION_DOWNLOAD_CLEANUP_UNAVAILABLE')
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist)
