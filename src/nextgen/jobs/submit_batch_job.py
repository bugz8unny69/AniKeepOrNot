"""One-shot stopped batch submission; registered in the default worker.

The qBittorrent metadata reader is hash-only; the durable journal owns
permission for one stopped add and all later attempts reconcile without
resending, even on timeout or process restart.
"""
import inspect

from src.nextgen.acquisition.metadata import (
    read_batch_inventory, AcquisitionConflict, MetadataInvalid, SourceMissing,
)
from src.nextgen.acquisition.settings import download_options, DownloadConfigurationError
from src.nextgen.acquisition.submission import (
    ClientUnavailable, ClientInvalid, ClientAuthenticationFailed, ClientUnsupported,
)
from src.nextgen.acquisition.metainfo_source import (
    MetadataSourceWaiting, MetadataSourceUnavailable,
    MetadataSourceConfigurationError, MetadataSourceDenied,
)
from src.nextgen.batch.qb_client import BatchClient
from src.nextgen.diagnostics import log_history
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import (
    CompletionRejected, Outcome, Registration, Successor, failure_facts,
)
from src.nextgen.storage.batch_submission_journal import (
    BatchSubmissionJournal,
)
from src.nextgen.worker import blocking
from src.nextgen.services.base import service_facts


KIND='SUBMIT_BATCH'
SERVICES=('qbittorrent',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Submit owned season pack stopped')


def _persist(db,claim,value):
    try:
        if (type(value) is not dict or set(value)!={
                'reservation_job_id','submission_job_id'}
                or value['submission_job_id']!=claim.job_id):
            raise AcquisitionConflict()
        reservation=integer(value['reservation_job_id'],positive=True)
        row=db.execute(
            'SELECT j.event_id,j.kind,j.state,s.state '
            'FROM batch_torrent_submissions s '
            'JOIN jobs j ON j.job_id=s.reservation_job_id '
            'WHERE s.first_job_id=? AND s.reservation_job_id=?',
            (claim.job_id,reservation)).fetchone()
        if row!=(claim.event_id,'RESERVE_BATCH','DONE','OWNED_STOPPED'):
            raise AcquisitionConflict()
    except (ValueError,TypeError,AcquisitionConflict):
        raise CompletionRejected(Outcome('review','TORRENT_OWNERSHIP_CONFLICT')) from None


def registration(read_batch_metainfo,secrets,*,client_factory=BatchClient,
                 journal_factory=BatchSubmissionJournal,transfer_kind=None):
    if not inspect.iscoroutinefunction(read_batch_metainfo):
        raise ValueError('Trusted asynchronous hash-only metainfo reader required')
    if transfer_kind is not None and (type(transfer_kind) is not str
            or not transfer_kind.strip() or len(transfer_kind)>64
            or transfer_kind==KIND):
        raise ValueError('Invalid batch transfer successor')

    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'reservation_job_id'} or context.claim.event_id is None:
                raise ValueError()
            reservation=integer(payload['reservation_job_id'],positive=True)
        except (ValueError,TypeError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        journal=journal_factory(context.configuration.value('storage.database_path'))
        intent=None
        def log(records):
            for record in records:log_history(context.logger,record)
        def waiting(reason='TORRENT_SUBMISSION_UNCERTAIN'):
            return Outcome('defer',reason,delay_ms=60000)
        try:
            digest=await blocking(journal.source_hash,context.claim,reservation)
            raw=await read_batch_metainfo(digest,context.configuration,context.cancelled)
            inventory=await blocking(read_batch_inventory,raw,digest)
            candidate,prior=await blocking(
                journal.load,context.claim,reservation,inventory)
            if prior is not None and prior.state in ('EXTERNAL','REVIEW'):
                return Outcome('review','TORRENT_ALREADY_PRESENT' if prior.state=='EXTERNAL'
                               else 'TORRENT_OWNERSHIP_CONFLICT')
            # Recheck current ownership even after a previous stopped
            # confirmation; the tag and file list may have changed.
            credentials=await blocking(secrets.qbittorrent_credentials)
            options=prior.options if prior is not None else download_options(context.configuration)
            client=client_factory(options,credentials)
            await blocking(client.login)
            before=await blocking(client.inspect,digest)
            intent,permit,records=await blocking(
                journal.authorize,context.claim,candidate,inventory,
                download_options(context.configuration),client.major,
                before is not None,context.configuration.desired_revision)
            log(records)
            if intent.state=='EXTERNAL':
                return Outcome('review','TORRENT_ALREADY_PRESENT')
            if permit is not None:
                # An observed file list cannot be submitted as a .torrent.
                # Such batches must arrive through the already-owned early
                # admission, which never returns a second send permit.
                if type(raw) is not bytes:
                    raise AcquisitionConflict()
                if context.cancelled.is_set():return waiting()
                client=client_factory(intent.options,credentials)
                await blocking(client.login)
                await blocking(journal.guard_send,context.claim,intent)
                try:await blocking(client.add_batch_stopped,raw,intent,permit)
                except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                    # The remote add may already have committed. Never retry.
                    return waiting()
            observed=await blocking(client.inspect,digest)
            state,reason,records=await blocking(
                journal.observe,context.claim,intent,observed)
            log(records)
            if state=='OWNED_STOPPED':
                return Outcome(value={'reservation_job_id':reservation,
                                      'submission_job_id':context.claim.job_id})
            if state in ('EXTERNAL','REVIEW'):
                return Outcome('review',reason)
            return waiting(reason)
        except ClientUnavailable:
            return waiting() if intent is not None else Outcome(
                'retry','SERVICE_UNAVAILABLE',facts=service_facts('qbittorrent'))
        except MetadataSourceWaiting as exc:
            return Outcome('defer',exc.reason_code,delay_ms=exc.delay_ms)
        except MetadataSourceUnavailable as exc:
            return Outcome('retry',exc.reason_code,facts=failure_facts(exc))
        except (MetadataSourceConfigurationError,MetadataSourceDenied) as exc:
            return Outcome('review',exc.reason_code,facts=failure_facts(exc))
        except (AcquisitionConflict,MetadataInvalid,SourceMissing,
                DownloadConfigurationError,ClientInvalid,
                ClientAuthenticationFailed,ClientUnsupported) as exc:
            return Outcome('review',exc.reason_code,facts=failure_facts(exc))

    def route(value):
        return () if transfer_kind is None else (
            Successor.create(transfer_kind,{
                'submission_job_id':value['submission_job_id']}),)
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist,route=route,
                        successor_kinds=() if transfer_kind is None else (transfer_kind,))
