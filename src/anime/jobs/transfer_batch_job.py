"""Opt-in E01-only batch selection, one-shot start and download observation."""
import inspect

from src.anime.acquisition.metadata import (
    read_batch_inventory, AcquisitionConflict, MetadataInvalid,
)
from src.anime.acquisition.submission import (
    ClientUnavailable, ClientInvalid, ClientAuthenticationFailed, ClientUnsupported,
)
from src.anime.acquisition.metainfo_source import (
    MetadataSourceWaiting, MetadataSourceUnavailable,
    MetadataSourceConfigurationError, MetadataSourceDenied,
)
from src.anime.acquisition.transfers import (
    TransferConflict, PieceOverlap, SnapshotChanged, CHECKING,
)
from src.anime.batch.probe_transfer import verify_probe_snapshot
from src.anime.batch.qb_client import BatchClient
from src.anime.diagnostics import log_history
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import (
    CompletionRejected, Outcome, Registration, Successor, failure_facts,
)
from src.anime.storage.batch_transfer_journal import (
    BatchTransferJournal,
)
from src.anime.worker import blocking
from src.anime.services.base import service_facts


KIND='TRANSFER_BATCH'
SERVICES=('qbittorrent',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Select E01 probe and observe batch torrent')


def _persist(db,claim,value):
    try:
        if (type(value) is not dict or set(value)!={
                'submission_job_id','transfer_job_id'}
                or value['transfer_job_id']!=claim.job_id):
            raise TransferConflict()
        submission=integer(value['submission_job_id'],positive=True)
        row=db.execute(
            "SELECT s.plan_id,s.state,s.reservation_job_id FROM "
            "batch_torrent_submissions s WHERE s.first_job_id=?",
            (submission,)).fetchone()
        if row is None or row[1]!='OWNED_STOPPED':
            raise TransferConflict()
        phase=db.execute(
            'SELECT phase,transfer_job_id FROM batch_torrent_transfers '
            'WHERE plan_id=?',(row[0],)).fetchone()
        if phase!=('COMPLETE',claim.job_id):
            raise TransferConflict()
    except (ValueError,TypeError,TransferConflict):
        raise CompletionRejected(Outcome('review','TORRENT_TRANSFER_CONFLICT')) from None


def registration(read_batch_metainfo,secrets,*,client_factory=BatchClient,
                 journal_factory=BatchTransferJournal,verification_kind=None):
    if not inspect.iscoroutinefunction(read_batch_metainfo):
        raise ValueError('Trusted async metadata reader required')
    if verification_kind is not None and (type(verification_kind) is not str
            or not verification_kind.strip() or len(verification_kind)>64
            or verification_kind==KIND):
        raise ValueError('Invalid batch media verifier successor')

    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'submission_job_id'} or context.claim.event_id is None:
                raise ValueError()
            submission=integer(payload['submission_job_id'],positive=True)
        except (ValueError,TypeError):
            return Outcome('review','JOB_PAYLOAD_INVALID')
        journal=journal_factory(context.configuration.value('storage.database_path'))
        intent=None
        def log(records):
            for record in records:log_history(context.logger,record)
        def wait(reason,active=False):
            return Outcome('defer',reason,delay_ms=30000,
                activity_code='DOWNLOADING' if active else 'WAITING_FOR_DOWNLOAD')
        try:
            digest=await blocking(journal.source_hash,context.claim,submission)
            raw=await read_batch_metainfo(digest,context.configuration,context.cancelled)
            inventory=await blocking(read_batch_inventory,raw,digest)
            intent,record=await blocking(
                journal.load,context.claim,submission,inventory)
            if record is not None and record.phase=='REVIEW':
                return Outcome('review',record.reason_code)
            credentials=await blocking(secrets.qbittorrent_credentials)
            client=client_factory(intent.options,credentials)
            await blocking(client.login)
            snapshot=await blocking(client.inspect_transfer,digest)
            if snapshot is None:return wait('TORRENT_DOWNLOAD_WAITING')
            verify_probe_snapshot(intent,snapshot)
            if snapshot.observation.state in CHECKING:
                return wait('TORRENT_SNAPSHOT_CHANGED')
            if record is None:
                record,permit,receipts=await blocking(
                    journal.select,context.claim,intent,snapshot,submission)
                log(receipts)
                if permit is not None:
                    for action,needed in (
                            ('skip',any(p!=0 for i,p in enumerate(snapshot.priorities)
                                        if i!=record.selected_index)),
                            ('wanted',snapshot.priorities[record.selected_index]!=1)):
                        if not needed:continue
                        if context.cancelled.is_set():
                            return wait('TORRENT_SELECTION_UNCERTAIN')
                        await blocking(
                            journal.guard,context.claim,intent,record,action,submission)
                        try:await blocking(
                            client.apply_probe_selection,intent,record.fingerprint,
                            permit,action)
                        except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                            return wait('TORRENT_SELECTION_UNCERTAIN')
                    snapshot=await blocking(client.inspect_transfer,digest)
                    if snapshot is None:return wait('TORRENT_SELECTION_UNCERTAIN')
            if record.phase=='SELECT_UNCERTAIN':
                if snapshot.observation.state in CHECKING:
                    return wait('TORRENT_SNAPSHOT_CHANGED')
                record,receipts=await blocking(
                    journal.selected,context.claim,intent,snapshot,submission)
                log(receipts)
                if record.phase=='SELECT_UNCERTAIN':
                    return wait('TORRENT_SELECTION_UNCERTAIN')
            if record.phase=='SELECTED':
                snapshot=await blocking(client.inspect_transfer,digest)
                if snapshot is None:return wait('TORRENT_DOWNLOAD_WAITING')
                if snapshot.observation.state in CHECKING:
                    return wait('TORRENT_SNAPSHOT_CHANGED')
                record,permit,receipts=await blocking(
                    journal.start,context.claim,intent,snapshot,submission)
                log(receipts)
                if permit is not None:
                    if context.cancelled.is_set():return wait('TORRENT_START_UNCERTAIN')
                    await blocking(
                        journal.guard,context.claim,intent,record,'start',submission)
                    try:await blocking(
                        client.start_probe,intent,record.fingerprint,permit)
                    except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                        return wait('TORRENT_START_UNCERTAIN')
            snapshot=await blocking(client.inspect_transfer,digest)
            record,receipts=await blocking(
                journal.observe,context.claim,intent,snapshot,submission,
                expected_record=record)
            log(receipts)
            if record.phase=='COMPLETE':
                return Outcome(value={'submission_job_id':submission,
                                      'transfer_job_id':context.claim.job_id})
            if record.phase=='REVIEW':
                return Outcome('review',record.reason_code)
            return wait('TORRENT_DOWNLOAD_IN_PROGRESS' if record.phase=='ACTIVE'
                        else 'TORRENT_START_UNCERTAIN',record.phase=='ACTIVE')
        except (TransferConflict,PieceOverlap) as exc:
            if intent is not None:
                log(await blocking(
                    journal.review,context.claim,intent,submission,exc.reason_code))
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
        except SnapshotChanged:
            return wait('TORRENT_SNAPSHOT_CHANGED')
        except ClientUnavailable:
            return Outcome('retry','SERVICE_UNAVAILABLE',facts=service_facts('qbittorrent'))
        except MetadataSourceWaiting as exc:
            return Outcome('defer',exc.reason_code,delay_ms=exc.delay_ms)
        except MetadataSourceUnavailable as exc:
            return Outcome('retry',exc.reason_code,facts=failure_facts(exc))
        except (MetadataSourceConfigurationError,MetadataSourceDenied) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
        except (AcquisitionConflict,MetadataInvalid,ClientInvalid,
                ClientAuthenticationFailed,ClientUnsupported) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))

    def route(value):
        return () if verification_kind is None else (
            Successor.create(verification_kind,{
                'transfer_job_id':value['transfer_job_id']}),)
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist,route=route,
                        successor_kinds=() if verification_kind is None else (verification_kind,))
