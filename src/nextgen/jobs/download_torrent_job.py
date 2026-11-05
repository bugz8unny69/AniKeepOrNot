"""Fixed file selection, one-shot start, then client-reported selected progress."""
from src.nextgen.acquisition.metadata import AcquisitionConflict, MetadataInvalid, SourceMissing
from src.nextgen.acquisition.settings import DownloadConfigurationError, SizeEvidenceMissing
from src.nextgen.acquisition.submission import ClientUnavailable, ClientInvalid, ClientAuthenticationFailed, ClientUnsupported
from src.nextgen.acquisition.qbittorrent import Client
from src.nextgen.acquisition.transfers import (TransferConflict, PieceOverlap, SnapshotChanged, CHECKING,
                                             verify_snapshot)
from src.nextgen.diagnostics import log_history
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration, Successor, failure_facts
from src.nextgen.storage.transfers import TransferJournal, _current, _record
from src.nextgen.worker import blocking

KIND='DOWNLOAD_TORRENT'
SERVICES=('qbittorrent',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Select video, start torrent and monitor download')


def _persist(db,claim,value):
    try:
        if type(value) is not dict or set(value)!={'preparation_job_id','download_job_id'} or value['download_job_id']!=claim.job_id:raise ValueError()
        preparation=integer(value['preparation_job_id'],positive=True)
        current=_current(db,claim,preparation)
        record=_record(db,current)
        if record is None or record.phase!='COMPLETE':raise TransferConflict()
    except (ValueError,TypeError) as exc:
        reason=exc.reason_code if hasattr(exc,'reason_code') else 'TORRENT_TRANSFER_CONFLICT'
        raise CompletionRejected(Outcome('review',reason)) from None


def registration(secrets,*,client_factory=Client,journal_factory=TransferJournal,verification_kind=None):
    if verification_kind is not None and (type(verification_kind) is not str or not verification_kind.strip()
            or len(verification_kind)>64 or verification_kind==KIND):raise ValueError("invalid verification successor")
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'preparation_job_id'} or context.claim.event_id is None:raise ValueError()
            preparation=integer(payload['preparation_job_id'],positive=True)
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        journal=journal_factory(context.configuration.value('storage.database_path'))
        intent=None
        def log(records):
            for record in records:log_history(context.logger,record)
        def wait(reason,active=False):
            return Outcome('defer',reason,delay_ms=30000,activity_code='DOWNLOADING' if active else 'WAITING_FOR_DOWNLOAD')
        try:
            intent,record=await blocking(journal.load,context.claim,preparation)
            if record is not None and record.phase=='REVIEW':return Outcome('review',record.reason_code)
            credentials=await blocking(secrets.qbittorrent_credentials)
            client=client_factory(intent.options,credentials)
            await blocking(client.login)
            snapshot=await blocking(client.inspect_transfer,intent.plan.manifest.info_hash)
            if snapshot is None:return wait('TORRENT_DOWNLOAD_WAITING')
            verify_snapshot(intent,snapshot)
            if snapshot.observation.state in CHECKING:return wait('TORRENT_SNAPSHOT_CHANGED')
            if record is None:
                record,permit,receipts=await blocking(journal.select,context.claim,intent,snapshot)
                log(receipts)
                if permit is not None:
                    for action,needed in (('skip',any(p!=0 for i,p in enumerate(snapshot.priorities) if i!=record.selected_index)),
                                          ('wanted',snapshot.priorities[record.selected_index]!=1)):
                        if not needed:continue
                        if context.cancelled.is_set():return wait('TORRENT_SELECTION_UNCERTAIN')
                        await blocking(journal.guard,context.claim,intent,record,action)
                        try:await blocking(client.apply_selection,intent,record.fingerprint,permit,action)
                        except (ClientUnavailable,ClientAuthenticationFailed,ClientInvalid):return wait('TORRENT_SELECTION_UNCERTAIN')
                    snapshot=await blocking(client.inspect_transfer,intent.plan.manifest.info_hash)
                    if snapshot is None:return wait('TORRENT_SELECTION_UNCERTAIN')
            if record.phase=='SELECT_UNCERTAIN':
                if snapshot.observation.state in CHECKING:return wait('TORRENT_SNAPSHOT_CHANGED')
                record,receipts=await blocking(journal.selected,context.claim,intent,snapshot)
                log(receipts)
                if record.phase=='SELECT_UNCERTAIN':return wait('TORRENT_SELECTION_UNCERTAIN')
            if record.phase=='SELECTED':
                # Read again after selection is durably confirmed, before consuming start.
                snapshot=await blocking(client.inspect_transfer,intent.plan.manifest.info_hash)
                if snapshot is None:return wait('TORRENT_DOWNLOAD_WAITING')
                if snapshot.observation.state in CHECKING:return wait('TORRENT_SNAPSHOT_CHANGED')
                record,permit,receipts=await blocking(journal.start,context.claim,intent,snapshot)
                log(receipts)
                if permit is not None:
                    if context.cancelled.is_set():return wait('TORRENT_START_UNCERTAIN')
                    await blocking(journal.guard,context.claim,intent,record,'start')
                    try:await blocking(client.start_selected,intent,record.fingerprint,permit)
                    except (ClientUnavailable,ClientAuthenticationFailed,ClientInvalid):return wait('TORRENT_START_UNCERTAIN')
            # start() can return another job's newer record without a permit.
            # Read again before reconciling that record, even when we did not send.
            snapshot=await blocking(client.inspect_transfer,intent.plan.manifest.info_hash)
            record,receipts=await blocking(journal.observe,context.claim,intent,snapshot,expected_record=record)
            log(receipts)
            if record.phase=='COMPLETE':return Outcome(value={'preparation_job_id':preparation,'download_job_id':context.claim.job_id})
            if record.phase=='REVIEW':return Outcome('review',record.reason_code)
            return wait('TORRENT_DOWNLOAD_IN_PROGRESS' if record.phase=='ACTIVE' else 'TORRENT_START_UNCERTAIN',record.phase=='ACTIVE')
        except (TransferConflict,PieceOverlap) as exc:
            if intent is not None:log(await blocking(journal.review,context.claim,intent,exc.reason_code))
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
        except SnapshotChanged:return wait('TORRENT_SNAPSHOT_CHANGED')
        except ClientUnavailable:return Outcome('retry','DOWNLOAD_CLIENT_UNAVAILABLE')
        except (AcquisitionConflict,MetadataInvalid,SourceMissing,SizeEvidenceMissing,DownloadConfigurationError,
                ClientInvalid,ClientAuthenticationFailed,ClientUnsupported) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
    def route(value):
        return () if verification_kind is None else (Successor.create(verification_kind,value),)
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist,route=route,
        successor_kinds=() if verification_kind is None else (verification_kind,))
