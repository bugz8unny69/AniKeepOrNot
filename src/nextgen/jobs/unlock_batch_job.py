"""One-time, JP+EN-gated batch episode unlock/transfer worker.

No release-file guess or job payload can authorize a priority change. A
persisted ffprobe+SHA256 proof from the completed E01 verifier is mandatory.
Any uncertain filePrio or start operation is observed, never resent.
"""
import inspect

from src.nextgen.acquisition.metadata import (
    read_batch_inventory, AcquisitionConflict, MetadataInvalid,
)
from src.nextgen.acquisition.submission import (
    ClientUnavailable, ClientInvalid, ClientAuthenticationFailed, ClientUnsupported,
)
from src.nextgen.acquisition.metainfo_source import (
    MetadataSourceWaiting, MetadataSourceUnavailable,
    MetadataSourceConfigurationError, MetadataSourceDenied,
)
from src.nextgen.acquisition.transfers import (
    TransferConflict, PieceOverlap, SnapshotChanged, CHECKING,
)
from src.nextgen.batch.unlock import validate_unlock_snapshot
from src.nextgen.batch.qb_client import BatchClient
from src.nextgen.diagnostics import log_history
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration, Successor, failure_facts
from src.nextgen.storage.batch_unlock_journal import BatchUnlockJournal
from src.nextgen.worker import blocking
from src.nextgen.services.base import service_facts


KIND='UNLOCK_BATCH'
SERVICES=('qbittorrent',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Unlock JP+EN-approved trial episodes')


def _persist(db,claim,value):
    try:
        if (type(value) is not dict or set(value)!={'verification_job_id','unlock_job_id','file_indices'}
                or value['unlock_job_id']!=claim.job_id):
            raise TransferConflict()
        verification=integer(value['verification_job_id'],positive=True)
        row=db.execute(
            "SELECT u.phase,u.unlock_job_id FROM batch_torrent_unlocks u "
            "JOIN batch_probe_media m USING(plan_id) "
            "WHERE m.verification_job_id=?",(verification,)).fetchone()
        if row!=('COMPLETE',claim.job_id):raise TransferConflict()
        rows=db.execute(
            'SELECT t.file_index FROM batch_torrent_unlocks u '
            'JOIN batch_user_evaluation_files t USING(plan_id) '
            'JOIN batch_probe_media m USING(plan_id) '
            'WHERE m.verification_job_id=? ORDER BY t.file_index',
            (verification,)).fetchall()
        if not 1<=len(rows)<=32 or value['file_indices']!=tuple(r[0] for r in rows):
            raise TransferConflict()
    except (ValueError,TypeError,TransferConflict):
        raise CompletionRejected(Outcome('review','TORRENT_TRANSFER_CONFLICT')) from None


def registration(read_batch_metainfo,secrets,*,client_factory=BatchClient,
                 journal_factory=BatchUnlockJournal,next_kind=None):
    if not inspect.iscoroutinefunction(read_batch_metainfo):
        raise ValueError('Trusted async hash-only metadata reader required')
    if next_kind is not None and (type(next_kind) is not str
            or not next_kind.strip() or len(next_kind)>64 or next_kind==KIND):
        raise ValueError('Invalid follow-on batch verification handler')

    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'verification_job_id'} or context.claim.event_id is None:
                raise ValueError()
            verification=integer(payload['verification_job_id'],positive=True)
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
            reservation,digest=await blocking(
                journal.source_hash,context.claim,verification)
            raw=await read_batch_metainfo(digest,context.configuration,context.cancelled)
            inventory=await blocking(read_batch_inventory,raw,digest)
            intent,media,record=await blocking(
                journal.load,context.claim,reservation,inventory)
            if record is not None and record.phase=='REVIEW':
                return Outcome('review',record.reason_code)
            credentials=await blocking(secrets.qbittorrent_credentials)
            client=client_factory(intent.options,credentials)
            await blocking(client.login)
            snapshot=await blocking(client.inspect_transfer,digest)
            if snapshot is None:return wait('TORRENT_DOWNLOAD_WAITING')
            # E01-only stopping is necessary only before the first
            # expanded-selection intent. Once that intent exists, even an
            # uncertain one, never re-enter the probe-only stop path:
            # authoritative priorities may already include E02+.
            if record is None:
                phase,stop_permit,stop_fingerprint,receipts=await blocking(
                    journal.stop_probe,context.claim,intent,snapshot,reservation)
                log(receipts)
                if stop_permit is not None:
                    if context.cancelled.is_set():
                        return wait('TORRENT_PROBE_STOP_UNCERTAIN')
                    await blocking(journal.guard_probe_stop,context.claim,intent,
                                   reservation,stop_fingerprint)
                    try:
                        await blocking(client.stop_completed_probe,intent,
                                       stop_fingerprint,stop_permit)
                    except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                        return wait('TORRENT_PROBE_STOP_UNCERTAIN')
                    snapshot=await blocking(client.inspect_transfer,digest)
                    if snapshot is None:return wait('TORRENT_PROBE_STOP_UNCERTAIN')
                    phase,_,_,receipts=await blocking(
                        journal.stop_probe,context.claim,intent,snapshot,reservation)
                    log(receipts)
                if phase!='STOPPED':
                    return wait('TORRENT_PROBE_STOP_UNCERTAIN')
                validate_unlock_snapshot(intent,snapshot,stopped=True)
            else:
                validate_unlock_snapshot(intent,snapshot)
            if record is None:
                record,permit,receipts=await blocking(
                    journal.authorize,context.claim,intent,media,snapshot,reservation)
                log(receipts)
                if permit is not None:
                    if context.cancelled.is_set():
                        return wait('TORRENT_SELECTION_UNCERTAIN')
                    await blocking(
                        journal.guard,context.claim,intent,record,'unlock',reservation)
                    try:await blocking(
                        client.apply_audio_verified_unlock,intent,media,
                        record.intent,permit)
                    except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                        return wait('TORRENT_SELECTION_UNCERTAIN')
                    snapshot=await blocking(client.inspect_transfer,digest)
                    if snapshot is None:return wait('TORRENT_SELECTION_UNCERTAIN')
            if record.phase=='SELECT_UNCERTAIN':
                if snapshot.observation.state in CHECKING:
                    return wait('TORRENT_SNAPSHOT_CHANGED')
                record,receipts=await blocking(
                    journal.selected,context.claim,intent,snapshot,reservation)
                log(receipts)
                if record.phase=='SELECT_UNCERTAIN':
                    return wait('TORRENT_SELECTION_UNCERTAIN')
            if record.phase=='SELECTED':
                snapshot=await blocking(client.inspect_transfer,digest)
                if snapshot is None:return wait('TORRENT_DOWNLOAD_WAITING')
                if snapshot.observation.state in CHECKING:
                    return wait('TORRENT_SNAPSHOT_CHANGED')
                record,permit,receipts=await blocking(
                    journal.start,context.claim,intent,snapshot,reservation)
                log(receipts)
                if permit is not None:
                    if context.cancelled.is_set():return wait('TORRENT_START_UNCERTAIN')
                    await blocking(
                        journal.guard,context.claim,intent,record,'start',reservation)
                    try:await blocking(
                        client.start_audio_verified_batch,intent,record.intent,permit)
                    except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                        return wait('TORRENT_START_UNCERTAIN')
            snapshot=await blocking(client.inspect_transfer,digest)
            record,receipts=await blocking(
                journal.observe,context.claim,intent,snapshot,reservation,
                expected_record=record)
            log(receipts)
            if record.phase=='COMPLETE':
                return Outcome(value={'verification_job_id':verification,
                                      'unlock_job_id':context.claim.job_id,
                                      'file_indices':tuple(sorted(i for _,i in intent.candidate.planned_episodes))})
            if record.phase=='REVIEW':
                return Outcome('review',record.reason_code)
            return wait('TORRENT_DOWNLOAD_IN_PROGRESS' if record.phase=='ACTIVE'
                        else 'TORRENT_START_UNCERTAIN',record.phase=='ACTIVE')
        except (TransferConflict,PieceOverlap) as exc:
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
        return () if next_kind is None else tuple(
            Successor.create(next_kind,{
                'unlock_job_id':value['unlock_job_id'],'file_index':index})
            for index in value['file_indices'])
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist,route=route,
                        successor_kinds=() if next_kind is None else (next_kind,))
