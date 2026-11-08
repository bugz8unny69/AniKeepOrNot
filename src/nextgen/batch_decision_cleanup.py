"""Conservative batch owned-torrent cleanup after completed Keep/Reject.

Reuse the existing single-use stop/remove and fd-relative manifest-only file
cleaner, but with its own durable v38 journal keyed by batch plan. The
adapter below is constructed exclusively from verified DB and torrent facts.
"""
import json
from types import SimpleNamespace

from src.nextgen.acquisition.cleanup import FileCleanup
from src.nextgen.acquisition.owned_cleanup import OwnedCleanup
from src.nextgen.torrent_client.qbittorrent import Client
from src.nextgen.cancellation import CancellationError, OperationGate
from src.nextgen.history import append_job_history, job_attempt
from src.nextgen.storage.batch_acquisition_candidate import _candidate
from src.nextgen.storage.batch_submission_journal import _intent, stored_inventory
from src.nextgen.storage.batch_keep_imports import batch_publications
from src.nextgen.storage.cancellation import operation
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction


def batch_cleanup_intent(intent):
    """Trusted, immutable-looking adapter for existing safe cleanup APIs."""
    return SimpleNamespace(
        options=intent.options,fingerprint=intent.fingerprint,
        plan=SimpleNamespace(
            plan_id=intent.candidate.plan_id,
            preparation_job_id=intent.candidate.reservation_job_id,
            marker=intent.marker,
            manifest=SimpleNamespace(
                info_hash=intent.inventory.info_hash,
                files=intent.inventory.files)))


def protected_staging_roots(publications):
    """Use pinned staging paths, not only the worker's current settings."""
    roots=set()
    try:
        for row in publications:
            plan=json.loads(row[2])
            if (plan.get('transfer')!='COPY'
                    or type(plan.get('root')) is not str):
                raise CancellationError('STAGING_TRANSFER_INVALID')
            roots.add(plan['root'])
    except (TypeError,ValueError,KeyError,IndexError):
        raise CancellationError('STAGING_MANIFEST_INVALID') from None
    if not roots:
        raise CancellationError('STAGING_ROOTS_EMPTY')
    return tuple(sorted(roots))


class BatchDecisionCleanup(OwnedCleanup):
    cleanup_event='DECISION_DOWNLOAD_CONTROL_RECORDED'
    complete_reason='DECISION_DOWNLOAD_CLEANUP_COMPLETE'
    pending_reason='DECISION_DOWNLOAD_CLEANUP_WAITING'
    purge=True

    def __init__(self,configuration,secrets,*,client_factory=Client,
                 files_factory=FileCleanup):
        self.configuration,self.secrets=configuration,secrets
        self.queue=JobQueue(configuration.value('storage.database_path'))
        self.client_factory,self.files_factory=client_factory,files_factory
        self.component='worker'
        self.guard=self._fence
        self.claim=None
        self.decision=None
        self.cancelled=None
        self.series_plan_id=None

    def _log(self,records):
        pass  # Audit receipts are committed by the shared journal.

    def facts(self,db):
        self.queue._owns(db,self.claim,self.queue.now())
        if self.series_plan_id is not None:
            return self._series_facts(db)
        row=db.execute(
            "SELECT a.operation_id,a.target_job_id,a.action,j.state,j.event_id "
            "FROM management_actions a JOIN jobs j USING(job_id) "
            "WHERE a.job_id=?",(self.decision,)).fetchone()
        if row is None or row[2]!='KEEP' or row[3]!='DONE':
            raise CancellationError('DECISION_ACTION_INVALID')
        op,target,action,_,event=row
        parent=db.execute(
            'SELECT created_by_job_id,event_id,kind FROM jobs WHERE job_id=?',
            (self.claim.job_id,)).fetchone()
        if event!=self.claim.event_id or parent[1:]!=(event,'CLEANUP_BATCH_DOWNLOAD'):
            raise CancellationError('PARENT_JOB_INVALID')
        prior=db.execute(
            "SELECT b.phase,j.kind,j.state,b.decision_job_id "
            "FROM batch_keep_cleanup b JOIN jobs j ON j.job_id=b.job_id "
            "WHERE b.job_id=?",(parent[0],)).fetchone()
        if prior!=('COMPLETE','CLEANUP_BATCH_KEPT_MEDIA','DONE',self.decision):
            raise CancellationError('PRIOR_CLEANUP_INCOMPLETE')
        if operation(db,target)[:2]!=(op,'STAGED'):
            raise CancellationError('OPERATION_NOT_STAGED')
        staged=batch_publications(db,op)
        states=db.execute(
            'SELECT file_index,phase FROM batch_keep_imports '
            'WHERE job_id=? ORDER BY file_index',(self.decision,)).fetchall()
        if states!=[(item[1],'COMPLETE') for item in staged]:
            raise CancellationError('BATCH_IMPORTS_INCOMPLETE')
        protected=protected_staging_roots(staged)
        plan=staged[0][0]
        source=db.execute(
            "SELECT s.reservation_job_id,s.info_hash,s.info_sha256,s.state "
            "FROM batch_torrent_submissions s WHERE s.plan_id=?",
            (plan,)).fetchone()
        if source is None or source[3]!='OWNED_STOPPED':raise CancellationError('SOURCE_NOT_STOPPED')
        return op,plan,source[0],source[1],source[2],protected

    def _series_facts(self, db):
        """A rejected series' pinned batch plan; not a new torrent identity."""
        decision = db.execute(
            "SELECT d.anidb_id,d.cleanup_state,j.kind "
            "FROM anime_review_decisions d JOIN jobs j ON j.job_id=d.job_id "
            "WHERE d.job_id=? AND d.decision='REJECT'", (self.decision,)).fetchone()
        if decision is None:
            return self._staged_facts(db)
        if decision[1:] != ('PENDING', 'REJECT_SERIES'):
            raise CancellationError('SERIES_DECISION_INVALID')
        source = db.execute(
            "SELECT s.job_id,m.operation_id FROM batch_staging_publications s "
            "JOIN job_operation_members m ON m.job_id=s.job_id "
            "WHERE s.plan_id=? AND s.state='PUBLISHED'",
            (self.series_plan_id,)).fetchall()
        operations = {op for _, op in source}
        if len(operations) != 1:
            raise CancellationError('MULTIPLE_OPERATIONS_FOUND')
        op = operations.pop()
        staged = batch_publications(db, op)
        if staged[0][0] != self.series_plan_id or any(r[4] != decision[0] for r in staged):
            raise CancellationError('SERIES_PLAN_MISMATCH')
        if operation(db, source[0][0])[:2] != (op, 'STAGED'):
            raise CancellationError('OPERATION_NOT_STAGED')
        if db.execute("SELECT 1 FROM management_actions WHERE operation_id=? "
                      "AND action='KEEP'", (op,)).fetchone():
            raise CancellationError('KEEP_ACTION_EXISTS')
        payload = db.execute('SELECT payload_json FROM jobs WHERE job_id=?',
                             (self.decision,)).fetchone()
        import json as _json
        if payload is None or op not in _json.loads(payload[0]).get('operation_ids', ()):
            raise CancellationError('PAYLOAD_OPERATION_MISMATCH')
        parent = db.execute('SELECT created_by_job_id,kind FROM jobs WHERE job_id=?',
                            (self.claim.job_id,)).fetchone()
        if parent != (self.decision, 'CLEANUP_BATCH_DOWNLOAD'):
            raise CancellationError('PARENT_JOB_MISMATCH')
        protected = protected_staging_roots(staged)
        source_receipt = db.execute(
            "SELECT reservation_job_id,info_hash,info_sha256,state "
            "FROM batch_torrent_submissions WHERE plan_id=?",
            (self.series_plan_id,)).fetchone()
        if source_receipt is None or source_receipt[3] != 'OWNED_STOPPED':
            raise CancellationError('SOURCE_NOT_STOPPED')
        return (op, self.series_plan_id, source_receipt[0], source_receipt[1],
                source_receipt[2], protected)

    def _staged_facts(self, db):
        """Pack fully staged: decision is the last STAGE_BATCH_EPISODE job."""
        job = db.execute('SELECT kind,state FROM jobs WHERE job_id=?',
                         (self.decision,)).fetchone()
        if job != ('STAGE_BATCH_EPISODE', 'DONE'):
            raise CancellationError('STAGE_JOB_NOT_DONE')
        source = db.execute(
            "SELECT s.job_id,m.operation_id,s.state FROM batch_staging_publications s "
            "JOIN job_operation_members m ON m.job_id=s.job_id WHERE s.plan_id=?",
            (self.series_plan_id,)).fetchall()
        selected = db.execute('SELECT count(*) FROM batch_user_evaluation_files WHERE plan_id=?',
                              (self.series_plan_id,)).fetchone()[0]
        operations = {op for _, op, _ in source}
        if (len(operations) != 1 or len(source) != selected
                or any(state != 'PUBLISHED' for *_, state in source)
                or self.decision not in {job for job, _, _ in source}):
            raise CancellationError('PUBLICATIONS_INCOMPLETE')
        op = operations.pop()
        if operation(db, self.decision)[:2] != (op, 'STAGED'):
            raise CancellationError('OPERATION_NOT_STAGED')
        parent = db.execute('SELECT created_by_job_id,kind FROM jobs WHERE job_id=?',
                            (self.claim.job_id,)).fetchone()
        if parent != (self.decision, 'CLEANUP_BATCH_DOWNLOAD'):
            raise CancellationError('PARENT_JOB_MISMATCH')
        staged = batch_publications(db, op)
        source_receipt = db.execute(
            "SELECT reservation_job_id,info_hash,info_sha256,state "
            "FROM batch_torrent_submissions WHERE plan_id=?",
            (self.series_plan_id,)).fetchone()
        if source_receipt is None or source_receipt[3] != 'OWNED_STOPPED':
            raise CancellationError('SOURCE_NOT_STOPPED')
        return (op, self.series_plan_id, source_receipt[0], source_receipt[1],
                source_receipt[2], protected_staging_roots(staged))

    def _fence(self,db=None):
        if self.cancelled is not None and self.cancelled.is_set():
            raise CancellationError('CLEANUP_CANCELLED')
        if db is not None:
            return self.facts(db)
        with self.queue.connection() as connection,transaction(connection):
            return self.facts(connection)

    def _journal(self,op,intent):
        with self.queue.connection() as db,transaction(db):
            self._fence(db)
            row=db.execute(
                'SELECT operation_id,decision_job_id,fingerprint,files_json,phase '
                'FROM batch_download_cleanup WHERE plan_id=?',
                (intent.plan.plan_id,)).fetchone()
            if row is not None:
                if row[:3]!=(op,self.decision,intent.fingerprint):
                    raise CancellationError('JOURNAL_RECORD_MISMATCH')
                return json.loads(row[3]),row[4]
            now=self.queue.now()
            db.execute(
                "INSERT INTO batch_download_cleanup(plan_id,decision_job_id,"
                "operation_id,fingerprint,files_json,phase,created_at_ms,updated_at_ms)"
                " VALUES(?,?,?,?,?,'PREPARED',?,?)",
                (intent.plan.plan_id,self.decision,op,intent.fingerprint,'{}',now,now))
            return {},'PREPARED'

    def _advance(self,intent,prior,phase,files=None):
        with self.queue.connection() as db,transaction(db):
            self._fence(db)
            now=self.queue.now()
            if files is None:
                changed=db.execute(
                    'UPDATE batch_download_cleanup SET phase=?,updated_at_ms=? '
                    'WHERE plan_id=? AND decision_job_id=? AND phase=?',
                    (phase,now,intent.plan.plan_id,self.decision,prior)).rowcount
            else:
                raw=json.dumps(files,sort_keys=True,separators=(',',':'),allow_nan=False)
                if len(raw.encode())>1048576:raise CancellationError('DATA_SIZE_EXCEEDED')
                changed=db.execute(
                    'UPDATE batch_download_cleanup SET phase=?,files_json=?,updated_at_ms=? '
                    'WHERE plan_id=? AND decision_job_id=? AND phase=?',
                    (phase,raw,now,intent.plan.plan_id,self.decision,prior)).rowcount
            if changed!=1:raise CancellationError('FENCE_CONFLICT')
            audit=append_job_history(
                db,job_id=self.claim.job_id,event_code=self.cleanup_event,
                reason_code=self.complete_reason if phase=='COMPLETE' else self.pending_reason,
                component='worker',occurred_at_ms=now,
                details={'attempt':job_attempt(db,self.claim.job_id)})
        self._log((audit,))

    def run(self,claim,decision,cancelled,*,plan_id=None):
        self.claim,self.decision,self.cancelled=claim,decision,cancelled
        self.series_plan_id=plan_id
        if cancelled.is_set():raise CancellationError('CLEANUP_CANCELLED')
        with self.queue.connection() as db,transaction(db):
            op,plan,reservation,digest,sha,protected=self.facts(db)
            inventory=stored_inventory(db,plan,digest,sha)
            candidate=_candidate(db,reservation,inventory,cleanup=True)
            if candidate.plan_id!=plan:raise CancellationError('PLAN_MISMATCH')
            source=_intent(db,candidate,inventory)
            if source is None or source.state!='OWNED_STOPPED':
                raise CancellationError('SOURCE_NOT_STOPPED')
            adapter=batch_cleanup_intent(source)
        # Mirror the single-file cleanup gate: a recovering attempt must not
        # overtake external qBittorrent I/O from an expired earlier lease.
        with OperationGate(self.queue.database).hold(op,cleanup=True):
            self._fence()
            factory=self.files_factory
            self.files_factory=lambda target,roots:factory(target,(*roots,*protected))
            try:
                return self._clean_target(op,adapter)
            finally:
                self.files_factory=factory
