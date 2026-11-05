"""Fenced probe-only batch selection/start journal, independent of qB transport.

Every external mutation receives a one-time in-memory permit after a durable
uncertain record. A fresh process can *observe* but never reconstruct sends.
"""
from dataclasses import dataclass

from src.nextgen.acquisition.metadata import AcquisitionConflict, BatchInventory
from src.nextgen.acquisition.transfers import (
    TransferPermit, TransferConflict, STOPPED,
)
from src.nextgen.batch.probe_transfer import (
    verify_probe_snapshot, exact_probe_selection,
    selection_fingerprint, classify_probe_progress,
)
from src.nextgen.history import append_job_history, integer
from src.nextgen.storage.batch_acquisition_candidate import _candidate
from src.nextgen.storage.batch_submission_journal import (
    BatchSubmissionIntent, _intent as submission_intent,
)
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction


@dataclass(frozen=True)
class BatchTransferRecord:
    fingerprint: str
    phase: str
    piece_size: int
    selected_index: int
    transfer_job_id: int
    transfer_attempt: int
    start_job_id: int | None
    start_attempt: int | None
    progress: float
    reason_code: str


def _record(db,intent):
    row=db.execute(
        'SELECT fingerprint,phase,piece_size,selected_index,transfer_job_id,'
        'transfer_attempt,start_job_id,start_attempt,selected_progress,reason_code '
        'FROM batch_torrent_transfers WHERE plan_id=?',
        (intent.candidate.plan_id,)).fetchone()
    if row is None:return None
    if (row[0]!=selection_fingerprint(intent,row[2])
            or row[3]!=intent.candidate.probe_index):
        raise TransferConflict()
    return BatchTransferRecord(*row)


class BatchTransferJournal:
    def __init__(self,database,*,clock=None):
        self.queue=JobQueue(database,clock=clock)

    def _fence(self,db,claim,submission_job_id):
        self.queue._owns(db,claim,self.queue.now())
        if claim.kind!='TRANSFER_BATCH':raise AcquisitionConflict()
        row=db.execute(
            "SELECT submit.created_by_job_id,submit.event_id,submit.kind,submit.state,"
            "op.state FROM jobs transfer "
            "JOIN jobs submit ON submit.job_id=transfer.created_by_job_id "
            "JOIN job_operation_members tm ON tm.job_id=transfer.job_id "
            "JOIN job_operation_members sm ON sm.job_id=submit.job_id "
            "AND sm.operation_id=tm.operation_id "
            "JOIN job_operations op ON op.operation_id=tm.operation_id "
            "WHERE transfer.job_id=? AND submit.job_id=?",
            (claim.job_id,submission_job_id)).fetchone()
        if (row is None or row[1:]!=(claim.event_id,'SUBMIT_BATCH','DONE','ACTIVE')):
            raise AcquisitionConflict()
        reservation_job_id=row[0]
        if type(reservation_job_id) is not int or reservation_job_id<=0:
            raise AcquisitionConflict()
        candidate_row=db.execute(
            'SELECT plan_id,info_hash,info_sha256 FROM batch_torrent_submissions '
            'WHERE first_job_id=? AND reservation_job_id=? AND state=?',
            (submission_job_id,reservation_job_id,'OWNED_STOPPED')).fetchone()
        if candidate_row is None:raise TransferConflict()
        return reservation_job_id,candidate_row

    def _current(self,db,claim,submission_job_id,inventory):
        if type(inventory) is not BatchInventory:
            raise TransferConflict()
        reserved,row=self._fence(db,claim,submission_job_id)
        candidate=_candidate(db,reserved,inventory)
        if (candidate.plan_id,candidate.info_hash,candidate.info_sha256)!=row:
            raise AcquisitionConflict()
        intent=submission_intent(db,candidate,inventory)
        if type(intent) is not BatchSubmissionIntent or intent.state!='OWNED_STOPPED':
            raise TransferConflict()
        return intent,_record(db,intent)

    def source_hash(self,claim,submission_job_id):
        integer(submission_job_id,positive=True)
        with self.queue.connection() as db,transaction(db):
            reserved,row=self._fence(db,claim,submission_job_id)
            self.queue._owns(db,claim,self.queue.now())
            return row[1]

    def load(self,claim,submission_job_id,inventory):
        integer(submission_job_id,positive=True)
        with self.queue.connection() as db,transaction(db):
            result=self._current(db,claim,submission_job_id,inventory)
            self.queue._owns(db,claim,self.queue.now())
            return result

    def select(self,claim,intent,snapshot,submission_job_id):
        with self.queue.connection() as db,transaction(db):
            now=self.queue.now()
            current,record=self._current(db,claim,submission_job_id,intent.inventory)
            if current!=intent:raise TransferConflict()
            if record is not None:return record,None,()
            verify_probe_snapshot(current,snapshot,require_stopped=True)
            fingerprint=selection_fingerprint(current,snapshot.piece_size)
            index=current.candidate.probe_index
            db.execute(
                'INSERT INTO batch_torrent_transfers(plan_id,transfer_job_id,'
                'transfer_attempt,selected_index,piece_size,fingerprint,phase,'
                'reason_code,selected_progress,created_at_ms,updated_at_ms) '
                "VALUES(?,?,?,?,?,?,'SELECT_UNCERTAIN','TORRENT_SELECTION_UNCERTAIN',?,?,?)",
                (current.candidate.plan_id,claim.job_id,claim.attempt,index,
                 snapshot.piece_size,fingerprint,snapshot.progress[index],now,now))
            audit=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_SELECTION_RECORDED',
                reason_code='TORRENT_SELECTION_UNCERTAIN',component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,current),TransferPermit(fingerprint,('skip','wanted')),(audit,)

    def guard(self,claim,intent,record,action,submission_job_id):
        with self.queue.connection() as db,transaction(db):
            current,stored=self._current(db,claim,submission_job_id,intent.inventory)
            if current!=intent or stored is None or stored.fingerprint!=record.fingerprint:
                raise TransferConflict()
            if action in ('skip','wanted'):
                if (stored.phase!='SELECT_UNCERTAIN'
                        or (stored.transfer_job_id,stored.transfer_attempt)
                        !=(claim.job_id,claim.attempt)):
                    raise TransferConflict()
            elif action=='start':
                if (stored.phase!='START_UNCERTAIN'
                        or (stored.start_job_id,stored.start_attempt)
                        !=(claim.job_id,claim.attempt)):
                    raise TransferConflict()
            else:raise TransferConflict()
            self.queue._owns(db,claim,self.queue.now())

    def selected(self,claim,intent,snapshot,submission_job_id):
        with self.queue.connection() as db,transaction(db):
            now=self.queue.now()
            current,record=self._current(db,claim,submission_job_id,intent.inventory)
            if current!=intent or record is None:raise TransferConflict()
            verify_probe_snapshot(current,snapshot,require_stopped=True)
            if snapshot.piece_size!=record.piece_size:raise TransferConflict()
            if record.phase!='SELECT_UNCERTAIN':return record,()
            if not exact_probe_selection(current,snapshot):return record,()
            db.execute(
                "UPDATE batch_torrent_transfers SET phase='SELECTED',"
                "reason_code='TORRENT_SELECTION_CONFIRMED',selected_progress=?,"
                "updated_at_ms=? WHERE plan_id=?",
                (snapshot.progress[record.selected_index],now,current.candidate.plan_id))
            audit=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_SELECTION_CONFIRMED',
                reason_code='TORRENT_SELECTION_CONFIRMED',component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,current),(audit,)

    def start(self,claim,intent,snapshot,submission_job_id):
        with self.queue.connection() as db,transaction(db):
            now=self.queue.now()
            current,record=self._current(db,claim,submission_job_id,intent.inventory)
            if current!=intent or record is None:raise TransferConflict()
            if record.phase!='SELECTED':return record,None,()
            verify_probe_snapshot(current,snapshot,require_stopped=True)
            if (snapshot.piece_size!=record.piece_size
                    or not exact_probe_selection(current,snapshot)):
                raise TransferConflict()
            if snapshot.progress[record.selected_index]==1:
                phase,reason='COMPLETE','TORRENT_DOWNLOAD_COMPLETE'
                permit=None
                db.execute(
                    'UPDATE batch_torrent_transfers SET phase=?,reason_code=?,'
                    'selected_progress=1,updated_at_ms=? WHERE plan_id=?',
                    (phase,reason,now,current.candidate.plan_id))
            else:
                phase,reason='START_UNCERTAIN','TORRENT_START_UNCERTAIN'
                db.execute(
                    'UPDATE batch_torrent_transfers SET phase=?,reason_code=?,'
                    'start_job_id=?,start_attempt=?,updated_at_ms=? WHERE plan_id=?',
                    (phase,reason,claim.job_id,claim.attempt,now,current.candidate.plan_id))
                permit=TransferPermit(record.fingerprint,('start',))
            audit=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_TRANSFER_CHANGED',
                reason_code=reason,component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,current),permit,(audit,)

    def observe(self,claim,intent,snapshot,submission_job_id,*,expected_record=None):
        with self.queue.connection() as db,transaction(db):
            current,record=self._current(db,claim,submission_job_id,intent.inventory)
            if current!=intent or record is None:raise TransferConflict()
            if record.phase=='REVIEW':return record,()
            if expected_record is not None and expected_record!=record:return record,()
            if snapshot is None or record.phase not in ('START_UNCERTAIN','ACTIVE','COMPLETE'):
                return record,()
            verify_probe_snapshot(current,snapshot)
            if snapshot.piece_size!=record.piece_size:raise TransferConflict()
            phase,reason=classify_probe_progress(current,snapshot)
            if record.phase=='COMPLETE' and phase!='COMPLETE':raise TransferConflict()
            now=self.queue.now()
            db.execute(
                'UPDATE batch_torrent_transfers SET phase=?,reason_code=?,'
                'selected_progress=?,updated_at_ms=? WHERE plan_id=?',
                (phase,reason,snapshot.progress[record.selected_index],now,current.candidate.plan_id))
            audit=() if phase==record.phase else (append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_TRANSFER_CHANGED',
                reason_code=reason,component='worker',occurred_at_ms=now,
                details={'attempt':claim.attempt}),)
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,current),audit

    def review(self,claim,intent,submission_job_id,reason):
        if reason not in ('TORRENT_TRANSFER_CONFLICT','TORRENT_PIECE_OVERLAP'):
            raise ValueError('Unknown transfer review reason')
        with self.queue.connection() as db,transaction(db):
            current,record=self._current(db,claim,submission_job_id,intent.inventory)
            if current!=intent or record is None or record.phase=='REVIEW':return ()
            now=self.queue.now()
            db.execute(
                "UPDATE batch_torrent_transfers SET phase='REVIEW',reason_code=?,"
                "updated_at_ms=? WHERE plan_id=?",
                (reason,now,current.candidate.plan_id))
            audit=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_TRANSFER_CHANGED',
                reason_code=reason,component='worker',
                occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            return (audit,)
