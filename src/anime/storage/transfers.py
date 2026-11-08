"""Lease-fenced selection/start journal and client-reported transfer progress."""
from dataclasses import dataclass

from src.anime.acquisition.metadata import AcquisitionConflict
from src.anime.acquisition.transfers import (TransferPermit, TransferConflict, STOPPED,
    verify_snapshot, exact_selection, selection_fingerprint, transfer_state)
from src.anime.history import append_job_history
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import transaction
from src.anime.storage.submissions import _plan, _intent


@dataclass(frozen=True)
class TransferRecord:
    fingerprint: str
    phase: str
    piece_size: int
    selected_index: int
    start_job_id: int | None
    start_attempt: int | None
    progress: float
    reason_code: str


def _record(db,intent):
    row=db.execute('SELECT fingerprint,phase,piece_size,selected_index,start_job_id,start_attempt,selected_progress,reason_code '
                   'FROM torrent_transfers WHERE plan_id=?',(intent.plan.plan_id,)).fetchone()
    if row is None:return None
    if row[0]!=selection_fingerprint(intent,row[2]) or row[3]!=intent.plan.manifest.selected_index:raise TransferConflict()
    return TransferRecord(*row)


def _current(db,claim,preparation_job_id):
    plan=_plan(db,preparation_job_id,claim.event_id)
    intent=_intent(db,plan)
    if intent is None or intent.state!='OWNED_STOPPED':raise TransferConflict()
    return intent


def _audit(db,claim,code,reason,now):
    return append_job_history(db,job_id=claim.job_id,event_code=code,reason_code=reason,
                              component='worker',occurred_at_ms=now,details={'attempt':claim.attempt})


class TransferJournal:
    def __init__(self,database,*,clock=None):self.queue=JobQueue(database,clock=clock)
    def _fence(self,db,claim,intent=None,preparation=None):
        self.queue._owns(db,claim,self.queue.now())
        current=_current(db,claim,preparation if preparation is not None else intent.plan.preparation_job_id)
        if intent is not None and current.fingerprint!=intent.fingerprint:raise AcquisitionConflict()
        return current,_record(db,current)

    def load(self,claim,preparation):
        with self.queue.connection() as db,transaction(db):
            result=self._fence(db,claim,preparation=preparation)
            self.queue._owns(db,claim,self.queue.now())
            return result

    def select(self,claim,intent,snapshot):
        with self.queue.connection() as db,transaction(db):
            current,record=self._fence(db,claim,intent)
            if record is not None:return record,None,()
            verify_snapshot(current,snapshot)
            if snapshot.observation.state not in STOPPED:raise TransferConflict()
            now=self.queue.now()
            fingerprint=selection_fingerprint(current,snapshot.piece_size)
            progress=snapshot.progress[current.plan.manifest.selected_index]
            db.execute("INSERT INTO torrent_transfers(plan_id,selection_job_id,selection_attempt,selected_index,piece_size,"
                "fingerprint,phase,reason_code,selected_progress,created_at_ms,updated_at_ms) "
                "VALUES(?,?,?,?,?,?,'SELECT_UNCERTAIN','TORRENT_SELECTION_UNCERTAIN',?,?,?)",
                (current.plan.plan_id,claim.job_id,claim.attempt,current.plan.manifest.selected_index,
                 snapshot.piece_size,fingerprint,progress,now,now))
            receipt=_audit(db,claim,'TORRENT_SELECTION_RECORDED','TORRENT_SELECTION_UNCERTAIN',now)
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,current),TransferPermit(fingerprint,('skip','wanted')),(receipt,)

    def guard(self,claim,intent,record,action):
        with self.queue.connection() as db,transaction(db):
            current,stored=self._fence(db,claim,intent)
            if stored is None or stored.fingerprint!=record.fingerprint:raise TransferConflict()
            row=db.execute('SELECT selection_job_id,selection_attempt FROM torrent_transfers WHERE plan_id=?',
                           (current.plan.plan_id,)).fetchone()
            if action=='start':
                if stored.phase!='START_UNCERTAIN' or (stored.start_job_id,stored.start_attempt)!=(claim.job_id,claim.attempt):raise TransferConflict()
            elif action in ('skip','wanted'):
                if stored.phase!='SELECT_UNCERTAIN' or row!=(claim.job_id,claim.attempt):raise TransferConflict()
            else:raise TransferConflict()

    def selected(self,claim,intent,snapshot):
        with self.queue.connection() as db,transaction(db):
            current,record=self._fence(db,claim,intent)
            if record is None:raise TransferConflict()
            verify_snapshot(current,snapshot)
            if snapshot.piece_size!=record.piece_size:raise TransferConflict()
            if record.phase!='SELECT_UNCERTAIN':return record,()
            if snapshot.observation.state not in STOPPED:raise TransferConflict()
            if not exact_selection(current,snapshot):return record,()
            now=self.queue.now()
            db.execute("UPDATE torrent_transfers SET phase='SELECTED',reason_code='TORRENT_SELECTION_CONFIRMED',"
                       'selected_progress=?,updated_at_ms=? WHERE plan_id=?',
                       (snapshot.progress[record.selected_index],now,current.plan.plan_id))
            receipt=_audit(db,claim,'TORRENT_SELECTION_CONFIRMED','TORRENT_SELECTION_CONFIRMED',now)
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,current),(receipt,)

    def start(self,claim,intent,snapshot):
        with self.queue.connection() as db,transaction(db):
            current,record=self._fence(db,claim,intent)
            if record is None:raise TransferConflict()
            if record.phase!='SELECTED':return record,None,()
            verify_snapshot(current,snapshot)
            if snapshot.piece_size!=record.piece_size or snapshot.observation.state not in STOPPED or not exact_selection(current,snapshot):raise TransferConflict()
            now=self.queue.now()
            if snapshot.progress[record.selected_index]==1:
                phase,reason='COMPLETE','TORRENT_DOWNLOAD_COMPLETE'
                db.execute('UPDATE torrent_transfers SET phase=?,reason_code=?,selected_progress=1,updated_at_ms=? WHERE plan_id=?',
                           (phase,reason,now,current.plan.plan_id))
                permit=None
            else:
                if record.start_job_id is not None:raise TransferConflict()
                phase,reason='START_UNCERTAIN','TORRENT_START_UNCERTAIN'
                db.execute('UPDATE torrent_transfers SET start_job_id=?,start_attempt=?,phase=?,reason_code=?,updated_at_ms=? WHERE plan_id=?',
                           (claim.job_id,claim.attempt,phase,reason,now,current.plan.plan_id))
                permit=TransferPermit(record.fingerprint,('start',))
            receipt=_audit(db,claim,'TORRENT_TRANSFER_CHANGED',reason,now)
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,current),permit,(receipt,)

    def observe(self,claim,intent,snapshot,*,expected_record=None):
        with self.queue.connection() as db,transaction(db):
            current,record=self._fence(db,claim,intent)
            if record is None:raise TransferConflict()
            if record.phase=='REVIEW':return record,()
            # Another job may have advanced the journal after this observation
            # began. Retain its newer result instead of writing a stale snapshot.
            if expected_record is not None and record!=expected_record:return record,()
            if snapshot is None:
                if record.phase=='COMPLETE':raise TransferConflict()
                return record,()
            verify_snapshot(current,snapshot)
            if snapshot.piece_size!=record.piece_size:raise TransferConflict()
            if record.phase not in ('START_UNCERTAIN','ACTIVE','COMPLETE'):return record,()
            phase,reason=transfer_state(current,snapshot)
            progress=snapshot.progress[record.selected_index]
            if record.start_job_id is None and phase!='COMPLETE':raise TransferConflict()
            if record.phase=='COMPLETE' and phase!='COMPLETE':raise TransferConflict()
            now=self.queue.now()
            db.execute('UPDATE torrent_transfers SET phase=?,reason_code=?,selected_progress=?,updated_at_ms=? WHERE plan_id=?',
                       (phase,reason,progress,now,current.plan.plan_id))
            receipts=() if record.phase==phase else (_audit(db,claim,'TORRENT_TRANSFER_CHANGED',reason,now),)
            self.queue._owns(db,claim,self.queue.now())
            return _record(db,current),receipts

    def review(self,claim,intent,reason):
        if reason not in ('TORRENT_TRANSFER_CONFLICT','TORRENT_PIECE_OVERLAP'):raise ValueError('registered transfer review required')
        with self.queue.connection() as db,transaction(db):
            current,record=self._fence(db,claim,intent)
            if record is None or record.phase=='REVIEW':return ()
            now=self.queue.now()
            db.execute("UPDATE torrent_transfers SET phase='REVIEW',reason_code=?,updated_at_ms=? WHERE plan_id=?",
                       (reason,now,current.plan.plan_id))
            receipt=_audit(db,claim,'TORRENT_TRANSFER_CHANGED',reason,now)
            self.queue._owns(db,claim,self.queue.now())
            return (receipt,)
