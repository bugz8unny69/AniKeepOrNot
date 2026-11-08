"""One-shot, audio-proof-fenced batch file unlock journal.

Only a completed JP+EN E01 verification authorizes an unlock. A first
durable UNCERTAIN row returns a one-use process-local filePrio permit; a
restart can inspect, but never recreate, the external mutation permission.
"""
from dataclasses import dataclass
import json

from src.anime.acquisition.media import VerifiedMedia
from src.anime.acquisition.metadata import AcquisitionConflict
from src.anime.acquisition.transfers import (
    TransferPermit, TransferConflict, STOPPED, ACTIVE, CHECKING,
)
from src.anime.batch.unlock import (
    BatchUnlockIntent, planned_priorities, plan_verified_unlock,
    unlock_fingerprint, validate_unlock_snapshot, exact_unlock_selection,
)
from src.anime.history import append_job_history, integer
from src.anime.storage.batch_acquisition_candidate import _candidate
from src.anime.storage.batch_submission_journal import _intent
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import transaction


def _decode_media(raw,reservation_job_id,transfer_job_id,plan_id,
                  submission_fingerprint,transfer_fingerprint):
    if type(raw) is not str or len(raw.encode('utf-8'))>65536:
        raise TransferConflict()
    def unique(items):
        out={}
        for k,v in items:
            if k in out:raise TransferConflict()
            out[k]=v
        return out
    try:
        value=json.loads(raw,object_pairs_hook=unique)
        if type(value) is not dict or set(value)!={'path','root','file','sha256','duration_ms','languages'}:
            raise TransferConflict()
        media=VerifiedMedia(
            reservation_job_id,transfer_job_id,plan_id,
            submission_fingerprint,transfer_fingerprint,
            value['path'],tuple(value['root']),tuple(value['file']),value['sha256'],
            value['duration_ms'],tuple(value['languages']))
        if media.json()!=raw:raise TransferConflict()
        return media
    except (KeyError,ValueError,TypeError):
        raise TransferConflict() from None


@dataclass(frozen=True)
class BatchUnlockRecord:
    intent: BatchUnlockIntent
    phase: str
    first_job_id: int
    first_attempt: int
    start_job_id: int | None
    start_attempt: int | None
    reason_code: str


def _record(db,intent,media):
    row=db.execute(
        'SELECT unlock_job_id,unlock_attempt,verification_job_id,'
        'fingerprint,priorities_json,piece_size,phase,start_job_id,'
        'start_attempt,reason_code FROM batch_torrent_unlocks WHERE plan_id=?',
        (intent.candidate.plan_id,)).fetchone()
    if row is None:return None
    job,attempt,verification,fp,encoded,piece_size,phase,start_job,start_attempt,reason=row
    target=planned_priorities(intent)
    if (fp!=unlock_fingerprint(intent,media,piece_size)
            or encoded!=json.dumps(target,separators=(',',':'))
            or verification<=0):
        raise TransferConflict()
    plan=BatchUnlockIntent(intent.candidate.plan_id,fp,target,piece_size,verification)
    return BatchUnlockRecord(plan,phase,job,attempt,start_job,start_attempt,reason)


class BatchUnlockJournal:
    def __init__(self,database,*,clock=None):
        self.queue=JobQueue(database,clock=clock)

    def _current(self,db,claim,reservation_job_id,inventory):
        self.queue._owns(db,claim,self.queue.now())
        if claim.kind!='UNLOCK_BATCH':raise AcquisitionConflict()
        candidate=_candidate(db,reservation_job_id,inventory)
        intent=_intent(db,candidate,inventory)
        if intent is None or intent.state!='OWNED_STOPPED':raise TransferConflict()
        source=db.execute(
            "SELECT m.verification_job_id,m.evidence_json,m.transfer_job_id,"
            "m.submission_fingerprint,m.transfer_fingerprint,v.state,"
            "u.created_by_job_id,u.event_id,o.state "
            "FROM batch_probe_media m "
            "JOIN jobs v ON v.job_id=m.verification_job_id "
            "JOIN jobs u ON u.job_id=? "
            "JOIN job_operation_members vm ON vm.job_id=v.job_id "
            "JOIN job_operation_members um ON um.job_id=u.job_id "
            "AND um.operation_id=vm.operation_id "
            "JOIN job_operation_members rm ON rm.job_id=? "
            "AND rm.operation_id=vm.operation_id "
            "JOIN job_operations o ON o.operation_id=vm.operation_id "
            "WHERE m.plan_id=?",
            (claim.job_id,reservation_job_id,candidate.plan_id)).fetchone()
        if (source is None or source[5:]!=(
                'DONE',source[0],claim.event_id,'ACTIVE')):
            raise AcquisitionConflict()
        media=_decode_media(source[1],reservation_job_id,source[2],
                            candidate.plan_id,source[3],source[4])
        if (media.plan_id!=candidate.plan_id
                or media.preparation_job_id!=reservation_job_id
                or media.submission_fingerprint!=intent.fingerprint
                or media.relative_path!=inventory.files[candidate.probe_index].path):
            raise TransferConflict()
        receipt=_record(db,intent,media)
        if receipt is not None and receipt.intent.verification_job_id!=source[0]:
            raise TransferConflict()
        return intent,media,receipt

    def source_hash(self,claim,verification_job_id):
        integer(verification_job_id,positive=True)
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            if claim.kind!='UNLOCK_BATCH':raise AcquisitionConflict()
            row=db.execute(
                "SELECT s.reservation_job_id,s.info_hash,u.event_id,"
                "proof_job.state,op.state "
                "FROM jobs u "
                "JOIN jobs proof_job ON proof_job.job_id=u.created_by_job_id "
                "JOIN batch_probe_media m ON m.verification_job_id=proof_job.job_id "
                "JOIN batch_torrent_submissions s ON s.plan_id=m.plan_id "
                "JOIN job_operation_members um ON um.job_id=u.job_id "
                "JOIN job_operation_members pm ON pm.job_id=proof_job.job_id "
                "AND pm.operation_id=um.operation_id "
                "JOIN job_operation_members rm ON rm.job_id=s.reservation_job_id "
                "AND rm.operation_id=um.operation_id "
                "JOIN job_operations op ON op.operation_id=um.operation_id "
                "WHERE u.job_id=? AND proof_job.job_id=? AND s.state='OWNED_STOPPED'",
                (claim.job_id,verification_job_id)).fetchone()
            if row is None or row[2:]!=(claim.event_id,'DONE','ACTIVE'):
                raise AcquisitionConflict()
            self.queue._owns(db,claim,self.queue.now())
            return row[0],row[1]

    def load(self,claim,reservation_job_id,inventory):
        integer(reservation_job_id,positive=True)
        with self.queue.connection() as db,transaction(db):
            result=self._current(db,claim,reservation_job_id,inventory)
            self.queue._owns(db,claim,self.queue.now())
            return result

    def authorize(self,claim,intent,media,snapshot,reservation_job_id):
        with self.queue.connection() as db,transaction(db):
            current,proof,prior=self._current(
                db,claim,reservation_job_id,intent.inventory)
            if current!=intent or proof!=media:raise TransferConflict()
            if prior is not None:return prior,None,()
            proof_job=db.execute(
                'SELECT verification_job_id FROM batch_probe_media WHERE plan_id=?',
                (intent.candidate.plan_id,)).fetchone()[0]
            plan=plan_verified_unlock(intent,snapshot,proof,proof_job)
            now=self.queue.now()
            db.execute(
                'INSERT INTO batch_torrent_unlocks(plan_id,unlock_job_id,'
                'unlock_attempt,verification_job_id,fingerprint,priorities_json,'
                'piece_size,phase,reason_code,created_at_ms,updated_at_ms) '
                "VALUES(?,?,?,?,?,?,?,'SELECT_UNCERTAIN','TORRENT_SELECTION_UNCERTAIN',?,?)",
                (plan.plan_id,claim.job_id,claim.attempt,proof_job,
                 plan.fingerprint,json.dumps(plan.priorities,separators=(',',':')),
                 plan.piece_size,now,now))
            audit=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_SELECTION_RECORDED',
                reason_code='TORRENT_SELECTION_UNCERTAIN',component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,intent,proof),TransferPermit(plan.fingerprint,('unlock',)),(audit,)

    def guard(self,claim,intent,record,action,reservation_job_id):
        with self.queue.connection() as db,transaction(db):
            current,media,stored=self._current(
                db,claim,reservation_job_id,intent.inventory)
            if current!=intent or stored is None or stored.intent!=record.intent:
                raise TransferConflict()
            if action=='unlock':
                if (stored.phase!='SELECT_UNCERTAIN'
                        or (stored.first_job_id,stored.first_attempt)
                        !=(claim.job_id,claim.attempt)):
                    raise TransferConflict()
            elif action=='start':
                if (stored.phase!='START_UNCERTAIN'
                        or (stored.start_job_id,stored.start_attempt)
                        !=(claim.job_id,claim.attempt)):
                    raise TransferConflict()
            else:raise TransferConflict()
            self.queue._owns(db,claim,self.queue.now())

    def selected(self,claim,intent,snapshot,reservation_job_id):
        with self.queue.connection() as db,transaction(db):
            current,proof,record=self._current(
                db,claim,reservation_job_id,intent.inventory)
            if current!=intent or record is None:raise TransferConflict()
            validate_unlock_snapshot(intent,snapshot,stopped=True)
            if not exact_unlock_selection(record.intent,snapshot):return record,()
            if record.phase!='SELECT_UNCERTAIN':return record,()
            now=self.queue.now()
            db.execute(
                "UPDATE batch_torrent_unlocks SET phase='SELECTED',"
                "reason_code='TORRENT_SELECTION_CONFIRMED',updated_at_ms=? "
                'WHERE plan_id=?',(now,intent.candidate.plan_id))
            audit=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_SELECTION_CONFIRMED',
                reason_code='TORRENT_SELECTION_CONFIRMED',component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,intent,proof),(audit,)

    def start(self,claim,intent,snapshot,reservation_job_id):
        with self.queue.connection() as db,transaction(db):
            current,proof,record=self._current(
                db,claim,reservation_job_id,intent.inventory)
            if current!=intent or record is None:raise TransferConflict()
            if record.phase!='SELECTED':return record,None,()
            validate_unlock_snapshot(intent,snapshot,stopped=True)
            if not exact_unlock_selection(record.intent,snapshot):
                raise TransferConflict()
            now=self.queue.now()
            planned=[i for i,p in enumerate(record.intent.priorities) if p==1]
            if all(snapshot.progress[i]==1 for i in planned):
                phase,reason='COMPLETE','TORRENT_DOWNLOAD_COMPLETE'
                permit=None
            else:
                phase,reason='START_UNCERTAIN','TORRENT_START_UNCERTAIN'
                permit=TransferPermit(record.intent.fingerprint,('start',))
            db.execute(
                'UPDATE batch_torrent_unlocks SET phase=?,reason_code=?,'
                'start_job_id=?,start_attempt=?,updated_at_ms=? WHERE plan_id=?',
                (phase,reason,claim.job_id if permit is not None else None,
                 claim.attempt if permit is not None else None,
                 now,intent.candidate.plan_id))
            audit=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_TRANSFER_CHANGED',
                reason_code=reason,component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,intent,proof),permit,(audit,)

    def observe(self,claim,intent,snapshot,reservation_job_id,*,expected_record=None):
        with self.queue.connection() as db,transaction(db):
            current,proof,record=self._current(
                db,claim,reservation_job_id,intent.inventory)
            if current!=intent or record is None:raise TransferConflict()
            if record.phase=='REVIEW':return record,()
            if expected_record is not None and record!=expected_record:
                return record,()
            if snapshot is None or record.phase not in (
                    'START_UNCERTAIN','ACTIVE','COMPLETE'):
                return record,()
            validate_unlock_snapshot(intent,snapshot)
            if not exact_unlock_selection(record.intent,snapshot):
                raise TransferConflict()
            wanted=[i for i,p in enumerate(record.intent.priorities) if p==1]
            if all(snapshot.progress[i]==1 for i in wanted):
                phase,reason='COMPLETE','TORRENT_DOWNLOAD_COMPLETE'
            elif snapshot.observation.state in ACTIVE:
                phase,reason='ACTIVE','TORRENT_DOWNLOAD_IN_PROGRESS'
            elif snapshot.observation.state in STOPPED|CHECKING:
                phase,reason='START_UNCERTAIN','TORRENT_START_UNCERTAIN'
            else:raise TransferConflict()
            if record.phase=='COMPLETE' and phase!='COMPLETE':
                raise TransferConflict()
            now=self.queue.now()
            db.execute(
                'UPDATE batch_torrent_unlocks SET phase=?,reason_code=?,updated_at_ms=? '
                'WHERE plan_id=?',(phase,reason,now,intent.candidate.plan_id))
            audit=() if phase==record.phase else (append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_TRANSFER_CHANGED',
                reason_code=reason,component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt}),)
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,intent,proof),audit


    def stop_probe(self,claim,intent,snapshot,reservation_job_id):
        """Durable one-shot stop permission, or read-only stopped reconciliation."""
        import hashlib
        from src.anime.batch.probe_transfer import (
            exact_probe_selection, verify_probe_snapshot,
        )
        from src.anime.acquisition.transfers import TransferPermit, ACTIVE, STOPPED
        with self.queue.connection() as db,transaction(db):
            current,media,_=self._current(db,claim,reservation_job_id,intent.inventory)
            if current!=intent or snapshot is None:
                raise TransferConflict()
            verify_probe_snapshot(intent,snapshot)
            if not exact_probe_selection(intent,snapshot):
                raise TransferConflict()
            probe=intent.candidate.probe_index
            if snapshot.progress[probe]!=1:
                raise TransferConflict()
            fingerprint=hashlib.sha256(
                ('batch-probe-stop:'+intent.fingerprint+':'+
                 str(snapshot.piece_size)+':'+str(probe)).encode()).hexdigest()
            prior=db.execute(
                'SELECT stop_job_id,stop_attempt,fingerprint,phase,piece_size '
                'FROM batch_probe_stops WHERE plan_id=?',
                (intent.candidate.plan_id,)).fetchone()
            now=self.queue.now()
            if prior is not None:
                if prior[2]!=fingerprint or prior[4]!=snapshot.piece_size:
                    raise TransferConflict()
                if prior[3]=='STOPPED':
                    if snapshot.observation.state not in STOPPED:
                        raise TransferConflict()
                    return 'STOPPED',None,fingerprint,()
                if prior[3]!='STOP_UNCERTAIN':
                    raise TransferConflict()
                if snapshot.observation.state in STOPPED:
                    db.execute(
                        "UPDATE batch_probe_stops SET phase='STOPPED',updated_at_ms=? "
                        "WHERE plan_id=? AND phase='STOP_UNCERTAIN'",
                        (now,intent.candidate.plan_id))
                    record=append_job_history(
                        db,job_id=claim.job_id,event_code='TORRENT_TRANSFER_CHANGED',
                        reason_code='TORRENT_STOPPED_CONFIRMED',component='worker',
                        occurred_at_ms=now,details={'attempt':claim.attempt})
                    return 'STOPPED',None,fingerprint,(record,)
                # The old process may have sent stop; never recreate its permit.
                return 'STOP_UNCERTAIN',None,fingerprint,()
            if snapshot.observation.state in STOPPED:
                phase='STOPPED'
                permit=None
                reason='TORRENT_STOPPED_CONFIRMED'
            elif snapshot.observation.state in ACTIVE:
                phase='STOP_UNCERTAIN'
                permit=TransferPermit(fingerprint,('stop',))
                reason='TORRENT_PROBE_STOP_UNCERTAIN'
            else:
                raise TransferConflict()
            db.execute(
                'INSERT INTO batch_probe_stops(plan_id,stop_job_id,stop_attempt,'
                'fingerprint,phase,piece_size,created_at_ms,updated_at_ms) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (intent.candidate.plan_id,claim.job_id,claim.attempt,
                 fingerprint,phase,snapshot.piece_size,now,now))
            record=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_TRANSFER_CHANGED',
                reason_code=reason,component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            return phase,permit,fingerprint,(record,)

    def guard_probe_stop(self,claim,intent,reservation_job_id,fingerprint):
        with self.queue.connection() as db,transaction(db):
            current,_,_=self._current(db,claim,reservation_job_id,intent.inventory)
            if current!=intent:raise TransferConflict()
            row=db.execute(
                'SELECT stop_job_id,stop_attempt,fingerprint,phase '
                'FROM batch_probe_stops WHERE plan_id=?',
                (intent.candidate.plan_id,)).fetchone()
            if row!=(claim.job_id,claim.attempt,fingerprint,'STOP_UNCERTAIN'):
                raise TransferConflict()
            self.queue._owns(db,claim,self.queue.now())
