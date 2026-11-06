"""One-shot, lease-fenced batch torrent submission journal.

Never sends network requests. Records UNCERTAIN *before* an external add
attempt, returning a process-local one-use permit only on first insertion.
A restart can only reconcile the same record: it can never rearm a send.
"""
from dataclasses import dataclass,field
from pathlib import PurePosixPath
import hashlib
import json
import secrets
import threading

from src.nextgen.acquisition.metadata import BatchInventory, ObservedBatchInventory, TorrentFile, AcquisitionConflict
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.acquisition.submission import Observation, ClientInvalid
from src.nextgen.acquisition.transfers import STOPPED, CHECKING
from src.nextgen.history import append_job_history, integer
from src.nextgen.storage.batch_acquisition_candidate import (
    BatchAcquisitionCandidate, _candidate,
)
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction


@dataclass(frozen=True)
class BatchSubmissionIntent:
    candidate: BatchAcquisitionCandidate = field(repr=False)
    inventory: BatchInventory | ObservedBatchInventory = field(repr=False)
    options: DownloadOptions = field(repr=False)
    marker: str = field(repr=False)
    state: str
    fingerprint: str
    api_major: int
    def __post_init__(self):
        if (type(self.candidate) is not BatchAcquisitionCandidate
                or type(self.inventory) not in (BatchInventory,ObservedBatchInventory)
                or type(self.options) is not DownloadOptions
                or type(self.marker) is not str or len(self.marker)!=32
                or any(x not in '0123456789abcdef' for x in self.marker)
                or self.state not in ('UNCERTAIN','EXTERNAL','REVIEW','OWNED_STOPPED')
                or type(self.fingerprint) is not str or len(self.fingerprint)!=64
                or any(x not in '0123456789abcdef' for x in self.fingerprint)
                or type(self.api_major) is not int or self.api_major not in (4,5)):
            raise ClientInvalid()
        if (self.candidate.info_hash,self.candidate.info_sha256)!=(
                self.inventory.info_hash,self.inventory.info_sha256):
            raise AcquisitionConflict()


class BatchSendPermit:
    """Single use in one process; never reconstructed from SQLite on restart."""
    def __init__(self,fingerprint):
        self.fingerprint=fingerprint
        self._lock=threading.Lock()
        self._used=False

    def take(self,intent):
        with self._lock:
            if (self._used or type(intent) is not BatchSubmissionIntent
                    or intent.fingerprint!=self.fingerprint
                    or intent.state!='UNCERTAIN'):
                raise ClientInvalid()
            self._used=True


def _target(options,marker):
    if type(options) is not DownloadOptions:raise AcquisitionConflict()
    return DownloadOptions(options.base_url,
                           str(PurePosixPath(options.download_root)/('ad-'+marker)),
                           options.category,options.timeout_seconds)


def _fingerprint(candidate,inventory,options,marker,major):
    return hashlib.sha256(json.dumps({
        'plan':candidate.plan_id,'reservation_job':candidate.reservation_job_id,
        'info_hash':inventory.info_hash,'info_sha256':inventory.info_sha256,
        'files_sha256':hashlib.sha256(inventory.json().encode('utf-8')).hexdigest(),
        'planned_episodes':candidate.planned_episodes,
        'probe':candidate.probe_index,
        'marker':marker,'base_url':options.base_url,
        'save_path':options.download_root,'category':options.category,
        'timeout':options.timeout_seconds,'api_major':major,
        'stopped':True,'auto_tmm':False,'skip_checking':False,
        'content_layout':'Original','use_download_path':False,
    },sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()

def stored_inventory(db,plan_id,info_hash,info_sha256):
    """Recover the immutable, canonical torrent file list saved at submission.

    This is evidence of the original manifest, not permission to send or delete.
    The existing candidate, submission fingerprint and qB ownership checks
    remain mandatory before any external cleanup.
    """
    row=db.execute(
        "SELECT inventory_json FROM batch_submission_inventories "
        "WHERE plan_id=?",(plan_id,)).fetchone()
    if row is None:raise AcquisitionConflict()
    raw=row[0]
    try:
        def unique(pairs):
            out={}
            for key,value in pairs:
                if key in out:raise AcquisitionConflict()
                out[key]=value
            return out
        document=json.loads(raw,object_pairs_hook=unique)
        if (type(document) is not dict or set(document)!={'files'}
                or type(document['files']) is not list):
            raise AcquisitionConflict()
        inventory=(BatchInventory(info_hash,info_sha256,
                        tuple(TorrentFile(**f) for f in document['files']))
                   if info_sha256 is not None else ObservedBatchInventory(info_hash,
                        tuple(TorrentFile(**f) for f in document['files'])))
        if inventory.json()!=raw:raise AcquisitionConflict()
        return inventory
    except (TypeError,KeyError,ValueError):
        raise AcquisitionConflict() from None


def _intent(db,candidate,inventory):
    row=db.execute(
        'SELECT reservation_job_id,info_hash,info_sha256,ownership_marker,'
        'base_url,save_path,category,timeout_seconds,api_major,fingerprint,state '
        'FROM batch_torrent_submissions WHERE plan_id=?',(candidate.plan_id,)).fetchone()
    if row is None:return None
    reservation,info_hash,sha,marker,*rest=row
    options=DownloadOptions(*rest[:4])
    major,fingerprint,state=rest[4:]
    if ((reservation,info_hash,sha)!=(candidate.reservation_job_id,
                                      inventory.info_hash,inventory.info_sha256)
            or _fingerprint(candidate,inventory,options,marker,major)!=fingerprint):
        raise AcquisitionConflict()
    return BatchSubmissionIntent(candidate,inventory,options,marker,state,fingerprint,major)


def _reason(state):
    return {
        'UNCERTAIN':'TORRENT_SUBMISSION_UNCERTAIN',
        'EXTERNAL':'TORRENT_ALREADY_PRESENT',
        'REVIEW':'TORRENT_PROVENANCE_CONFLICT',
        'OWNED_STOPPED':'TORRENT_STOPPED_CONFIRMED',
    }[state]


def _classify(intent,observation):
    if observation is None:return 'UNCERTAIN','TORRENT_SUBMISSION_UNCERTAIN'
    if type(observation) is not Observation:raise ClientInvalid()
    if (observation.info_hash!=intent.inventory.info_hash
            or observation.save_path.rstrip('/')!=intent.options.download_root
            or observation.category!=intent.options.category
            or observation.auto_tmm is not False
            or 'ad-'+intent.marker not in observation.tags
            or any(t.startswith('ad-') and t!='ad-'+intent.marker
                   for t in observation.tags)):
        return 'REVIEW','TORRENT_PROVENANCE_CONFLICT'
    if observation.state in CHECKING or not observation.files:
        return 'UNCERTAIN','TORRENT_SUBMISSION_UNCERTAIN'
    if observation.files!=intent.inventory.files or observation.state not in STOPPED:
        return 'REVIEW','TORRENT_PROVENANCE_CONFLICT'
    return 'OWNED_STOPPED','TORRENT_STOPPED_CONFIRMED'


class BatchSubmissionJournal:
    def __init__(self,database,*,clock=None):
        self.queue=JobQueue(database,clock=clock)

    def _owns(self,db,claim,reservation_job_id):
        if claim.kind!='SUBMIT_BATCH':raise AcquisitionConflict()
        self.queue._owns(db,claim,self.queue.now())
        # The submit worker must be the direct successor of the reservation,
        # belong to the same event and active cancellation operation, and
        # never act using another operation's reservation permit.
        row=db.execute(
            "SELECT 1 FROM jobs submit "
            "JOIN job_operation_members sm ON sm.job_id=submit.job_id "
            "JOIN job_operation_members rm ON rm.operation_id=sm.operation_id "
            "JOIN jobs reserved ON reserved.job_id=rm.job_id "
            "JOIN job_operations op ON op.operation_id=sm.operation_id "
            "WHERE submit.job_id=? AND submit.kind='SUBMIT_BATCH' "
            "AND submit.created_by_job_id=? AND reserved.job_id=? "
            "AND submit.event_id=reserved.event_id AND op.state='ACTIVE' "
            "LIMIT 1",
            (claim.job_id,reservation_job_id,reservation_job_id)).fetchone()
        if row is None:raise AcquisitionConflict()

    def source_hash(self,claim,reservation_job_id):
        """Hash-only DB lookup. Never accept an arbitrary URL or torrent ID."""
        integer(reservation_job_id,positive=True)
        with self.queue.connection() as db,transaction(db):
            self._owns(db,claim,reservation_job_id)
            rows=db.execute(
                "SELECT DISTINCT b.info_hash FROM batch_episode_reservations r "
                "JOIN batch_user_evaluation_plans p ON p.plan_id=r.plan_id "
                "JOIN batch_verifications b USING(batch_id) "
                "JOIN jobs j ON j.job_id=r.owner_job_id "
                "WHERE r.owner_job_id=? AND r.state='ACTIVE' "
                "AND j.kind='RESERVE_BATCH' AND j.state='DONE' "
                "AND j.event_id=?",
                (reservation_job_id,claim.event_id)).fetchall()
            if len(rows)!=1:raise AcquisitionConflict()
            self._owns(db,claim,reservation_job_id)
            return rows[0][0]

    def load(self,claim,reservation_job_id,inventory):
        integer(reservation_job_id,positive=True)
        if type(inventory) not in (BatchInventory,ObservedBatchInventory):raise AcquisitionConflict()
        with self.queue.connection() as db,transaction(db):
            self._owns(db,claim,reservation_job_id)
            candidate=_candidate(db,reservation_job_id,inventory)
            intent=_intent(db,candidate,inventory)
            self._owns(db,claim,reservation_job_id)
            return candidate,intent

    def authorize(self,claim,candidate,inventory,options,api_major,present,revision):
        if (type(candidate) is not BatchAcquisitionCandidate
                or type(inventory) not in (BatchInventory,ObservedBatchInventory)
                or type(options) is not DownloadOptions
                or type(api_major) is not int or api_major not in (4,5)
                or type(present) is not bool):
            raise ValueError('Trusted batch source, qB client and observation required')
        integer(revision,positive=True)
        with self.queue.connection() as db,transaction(db):
            self._owns(db,claim,candidate.reservation_job_id)
            now=self.queue.now()
            if _candidate(db,candidate.reservation_job_id,inventory)!=candidate:
                raise AcquisitionConflict()
            prior=_intent(db,candidate,inventory)
            if prior is not None:
                if prior.options!=_target(options,prior.marker) or prior.api_major!=api_major:
                    raise AcquisitionConflict()
                return prior,None,()
            # Forbid a second journal for this hash even if an earlier
            # journal is in an uncertain or reviewed state.
            if db.execute('SELECT 1 FROM batch_torrent_submissions WHERE info_hash=?',
                          (inventory.info_hash,)).fetchone():
                raise AcquisitionConflict()
            # Legacy batches were admitted in VERIFY_RELEASE before provider
            # verification. Atomically transfer ownership of that stopped,
            # hash-bound, journaled torrent rather than submitting it twice.
            from src.nextgen.storage.batch_metadata_admission import _select, _inventory
            early=_select(db,info_hash=inventory.info_hash)
            if early is not None:
                verification=db.execute(
                    'SELECT b.job_id FROM batch_user_evaluation_plans p '
                    'JOIN batch_verifications b USING(batch_id) WHERE p.plan_id=?',
                    (candidate.plan_id,)).fetchone()
                old_inventory=_inventory(early)
                if (early.state!='OWNED_STOPPED'
                        or early.event_id!=claim.event_id
                        or verification!=(early.job_id,)
                        or early.api_major!=api_major
                        or early.options.base_url!=options.base_url
                        or early.options.category!=options.category
                        or early.options.timeout_seconds!=options.timeout_seconds
                        or not present or old_inventory is None
                        or old_inventory.files!=inventory.files):
                    raise AcquisitionConflict()
                marker=early.marker
                target=early.options
                state,reason='OWNED_STOPPED','TORRENT_STOPPED_CONFIRMED'
            else:
                marker=secrets.token_hex(16)
                target=_target(options,marker)
                state,reason=(('EXTERNAL','TORRENT_ALREADY_PRESENT') if present
                              else ('UNCERTAIN','TORRENT_SUBMISSION_UNCERTAIN'))
            fingerprint=_fingerprint(candidate,inventory,target,marker,api_major)
            db.execute(
                'INSERT INTO batch_torrent_submissions('
                'plan_id,reservation_job_id,first_job_id,first_attempt,info_hash,info_sha256,'
                'ownership_marker,base_url,save_path,category,timeout_seconds,api_major,'
                'fingerprint,configuration_revision,state,reason_code,created_at_ms,updated_at_ms)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (candidate.plan_id,candidate.reservation_job_id,claim.job_id,claim.attempt,
                 inventory.info_hash,inventory.info_sha256,marker,target.base_url,
                 target.download_root,target.category,target.timeout_seconds,api_major,
                 fingerprint,revision,state,reason,now,now))
            raw_inventory=inventory.json()
            if len(raw_inventory.encode('utf-8'))>1048576:
                raise AcquisitionConflict()
            db.execute(
                'INSERT INTO batch_submission_inventories(plan_id,inventory_json,created_at_ms) '
                'VALUES(?,?,?)',
                (candidate.plan_id,raw_inventory,now))
            if early is not None:
                db.execute(
                    "UPDATE batch_metadata_admissions SET state='ADOPTED',updated_at_ms=? "
                    "WHERE event_id=? AND state='OWNED_STOPPED'",
                    (now,early.event_id))
            receipt=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_INTENT_RECORDED',
                reason_code=reason,component='worker',occurred_at_ms=now,
                details={'attempt':claim.attempt})
            self._owns(db,claim,candidate.reservation_job_id)
            intent=BatchSubmissionIntent(candidate,inventory,target,marker,state,fingerprint,api_major)
            return intent,(None if early is not None or present else BatchSendPermit(fingerprint)),(receipt,)

    def guard_send(self,claim,intent):
        if type(intent) is not BatchSubmissionIntent:raise AcquisitionConflict()
        with self.queue.connection() as db,transaction(db):
            self._owns(db,claim,intent.candidate.reservation_job_id)
            candidate=_candidate(db,intent.candidate.reservation_job_id,intent.inventory)
            current=_intent(db,candidate,intent.inventory)
            if (current!=intent or current.state!='UNCERTAIN'
                    or db.execute('SELECT first_job_id,first_attempt FROM batch_torrent_submissions '
                                  'WHERE plan_id=?',(candidate.plan_id,)).fetchone()
                    !=(claim.job_id,claim.attempt)):
                raise AcquisitionConflict()
            self._owns(db,claim,intent.candidate.reservation_job_id)

    def observe(self,claim,intent,observation):
        if type(intent) is not BatchSubmissionIntent:raise AcquisitionConflict()
        with self.queue.connection() as db,transaction(db):
            self._owns(db,claim,intent.candidate.reservation_job_id)
            now=self.queue.now()
            candidate=_candidate(db,intent.candidate.reservation_job_id,intent.inventory)
            current=_intent(db,candidate,intent.inventory)
            if current is None or current.fingerprint!=intent.fingerprint:
                raise AcquisitionConflict()
            if current.state in ('EXTERNAL','REVIEW'):
                return current.state,_reason(current.state),()
            if current.state!=intent.state:
                return current.state,_reason(current.state),()
            state,reason=_classify(current,observation)
            if current.state=='OWNED_STOPPED' and state!='OWNED_STOPPED':
                # Ownership changed after adoption: fail closed. Never
                # silently treat a once-owned torrent as permanently valid.
                state,reason='REVIEW','TORRENT_PROVENANCE_CONFLICT'
            receipt=()
            if state!=current.state:
                db.execute('UPDATE batch_torrent_submissions SET state=?,reason_code=?,'
                           'updated_at_ms=? WHERE plan_id=?',
                           (state,reason,now,candidate.plan_id))
                receipt=(append_job_history(
                    db,job_id=claim.job_id,event_code='TORRENT_RECONCILED',
                    reason_code=reason,component='worker',occurred_at_ms=now,
                    details={'attempt':claim.attempt}),)
            self._owns(db,claim,intent.candidate.reservation_job_id)
            return state,reason,receipt
