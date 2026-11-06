"""Independent owned-download cleanup once the video is staged, or after Keep/Reject.

Staging copies a verified video, retaining qBittorrent's source until the
staging transaction commits. The owned torrent and its payload are then
removed, followed by any remaining pinned ad-* folder. Legacy MOVE plans
remain recoverable. Later Keep/Reject cleanup finds the job already done.
"""
import json

from src.nextgen.acquisition.cleanup import FileCleanup
from src.nextgen.acquisition.owned_cleanup import OwnedCleanup
from src.nextgen.acquisition.qbittorrent import Client
from src.nextgen.acquisition.submission import ClientUnavailable, SubmissionPlan, manifest_from_json
from src.nextgen.async_io import blocking
from src.nextgen.cancellation import CancellationError, CleanupBusy, OperationGate, failure_location
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration
from src.nextgen.messages import DEFAULT_CATALOG
from src.nextgen.storage.cancellation import operation
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.review_decisions import staged
from src.nextgen.storage.sqlite import enqueue_job, transaction
from src.nextgen.storage.submissions import _intent

KIND='CLEANUP_DECIDED_DOWNLOAD'
DEFINITION=HandlerDefinition(KIND,'Remove the finished download from qBittorrent')
STOPPED_JOBS=('NEEDS_REVIEW','BLOCKED')
STAGING_CLEANUP_DELAY_MS=60_000


def status(db,decision):
    rows=db.execute("SELECT job_id,state,last_reason_code FROM jobs WHERE kind=? "
        "AND json_extract(payload_json,'$.decision_job_id')=? ORDER BY job_id",(KIND,decision)).fetchall()
    if not rows:return None
    if len(rows)!=1:raise CancellationError()
    job,state,reason=rows[0]
    return {'job_id':job,'state':state,'status':DEFAULT_CATALOG.job_status(state),'completed':state=='DONE',
        'completion':DEFAULT_CATALOG.reason('DECISION_DOWNLOAD_CLEANUP_COMPLETE') if state=='DONE' else None,
        'reason':None if reason is None else DEFAULT_CATALOG.reason(reason),'status_url':f'/api/v1/jobs/{job}'}


def staging_status(db,stage_job):
    return db.execute("SELECT job_id FROM jobs WHERE kind=? "
        "AND json_extract(payload_json,'$.staging_job_id')=? ORDER BY job_id",(KIND,stage_job)).fetchall()


def enqueue_after_staging(db,claim):
    """Inside STAGE_TRIAL_MEDIA's completion transaction, exactly once."""
    if staging_status(db,claim.job_id):raise CancellationError()
    import time
    # A short pause lets the staging completion (and any Jellyfin scan request)
    # settle before the torrent is touched.
    enqueue_job(db,kind=KIND,payload={'staging_job_id':claim.job_id},component='worker',
        event_id=claim.event_id,created_by_job_id=claim.job_id,
        available_at_ms=time.time_ns()//1_000_000+STAGING_CLEANUP_DELAY_MS)


def enqueue(db,claim,decision):
    # Worker completion persists this inside the same transaction as DONE. No
    # historical decisions are backfilled or given new automatic delete authority.
    integer(decision,positive=True)
    if status(db,decision) is not None:raise CancellationError()
    enqueue_job(db,kind=KIND,payload={'decision_job_id':decision},component='worker',
        event_id=claim.event_id,created_by_job_id=claim.job_id)


class Service(OwnedCleanup):
    cleanup_event='DECISION_DOWNLOAD_CONTROL_RECORDED'
    complete_reason='DECISION_DOWNLOAD_CLEANUP_COMPLETE'
    pending_reason='DECISION_DOWNLOAD_CLEANUP_WAITING'
    purge=True

    def __init__(self,configuration,secrets,*,client_factory=Client,files_factory=FileCleanup):
        self.configuration,self.secrets=configuration,secrets
        self.queue=JobQueue(configuration.value('storage.database_path'))
        self.client_factory,self.files_factory=client_factory,files_factory
        self.component='worker'
        self.legacy_move=False

    def _log(self,records):pass  # Durable history is committed by the shared journal.

    def _staged_facts(self,db):
        """The staging job that published this download's video."""
        row=db.execute('SELECT kind,state,event_id FROM jobs WHERE job_id=?',(self.stage_job,)).fetchone()
        if row!=('STAGE_TRIAL_MEDIA','DONE',self.claim.event_id):raise CancellationError()
        op=operation(db,self.stage_job)
        if op[1]!='STAGED' or operation(db,self.claim.job_id)[0]==op[0]:raise CancellationError()
        origin=db.execute('SELECT created_by_job_id,event_id FROM jobs WHERE job_id=?',(self.claim.job_id,)).fetchone()
        if origin!=(self.stage_job,self.claim.event_id) or staging_status(db,self.stage_job)!=[(self.claim.job_id,)]:
            raise CancellationError()
        source=staged(db,op[0])
        publication=json.loads(source[2])
        keep_root=self.configuration.value('storage.keep_root')
        return op[0],source,publication,[publication['root'],*((keep_root,) if keep_root else ())]

    def facts(self,db):
        self.queue._owns(db,self.claim,self.queue.now())
        if self.stage_job is not None:
            op,source,publication,protected=self._staged_facts(db)
            self.legacy_move=(publication.get('transfer')=='MOVE' and bool(source[3]))
            return self._download(db,op,source,publication,protected)
        row=db.execute("SELECT a.operation_id,a.target_job_id,a.action,j.state,j.event_id "
            "FROM management_actions a JOIN jobs j USING(job_id) WHERE a.job_id=?",(self.decision,)).fetchone()
        if not row or row[2]!='KEEP' or row[3]!='DONE' or row[4]!=self.claim.event_id:raise CancellationError()
        op,target,action,_,event=row
        origin=db.execute('SELECT created_by_job_id,event_id FROM jobs WHERE job_id=?',(self.claim.job_id,)).fetchone()
        if operation(db,target)[:2]!=(op,'STAGED') or operation(db,self.claim.job_id)[0]==op:raise CancellationError()
        source=staged(db,op)
        publication=json.loads(source[2]);protected=[publication['root']]
        keep=db.execute('SELECT phase,data_json FROM keep_imports WHERE job_id=?',(self.decision,)).fetchone()
        if not keep or keep[0]!='COMPLETE':raise CancellationError()
        data=json.loads(keep[1]);cleanup=data.get('post_cleanup',{})
        if cleanup.get('state')!='COMPLETE':raise CancellationError()
        predecessor=cleanup.get('job_id')
        if db.execute('SELECT kind,state,created_by_job_id FROM jobs WHERE job_id=?',(predecessor,)).fetchone()!=('CLEANUP_KEPT_MEDIA','DONE',self.decision):raise CancellationError()
        protected.append(data['options']['keep_root'])
        if origin!=(predecessor,event) or status(db,self.decision)['job_id']!=self.claim.job_id:raise CancellationError()
        self.legacy_move=(publication.get('transfer')=='MOVE' and bool(source[3]))
        return self._download(db,op,source,publication,protected)

    def _download(self,db,op,source,publication,protected):
        if db.execute("SELECT 1 FROM job_operation_members m JOIN jobs j USING(job_id) "
            "WHERE m.operation_id=? AND j.state NOT IN ('DONE','CANCELED') LIMIT 1",(op,)).fetchone():raise CancellationError()
        rows=db.execute('SELECT p.job_id,j.event_id,p.plan_id FROM acquisition_plans p JOIN jobs j ON j.job_id=p.job_id '
            'JOIN job_operation_members m ON m.job_id=p.job_id WHERE m.operation_id=?',(op,)).fetchall()
        if len(rows)!=1 or rows[0][2]!=source[1]:raise CancellationError()
        preparation,event,plan_id=rows[0]
        if event!=self.claim.event_id:raise CancellationError()
        # Read historical ownership only. The acquisition reader deliberately
        # rejects a rejected anime; cleanup must never relax that send fence.
        receipt=db.execute("SELECT p.submission_marker,p.info_hash,p.info_sha256,p.manifest_json,r.evaluation_job_id "
            "FROM acquisition_plans p JOIN episode_reservations r USING(reservation_id) JOIN jobs j ON j.job_id=p.job_id "
            "WHERE p.plan_id=? AND p.state='PREPARED' AND r.state='ACTIVE' AND j.state='DONE' "
            "AND j.kind='PREPARE_ACQUISITION' AND j.event_id=?",(plan_id,event)).fetchone()
        if receipt is None:raise CancellationError()
        plan=SubmissionPlan(preparation,plan_id,receipt[4],event,receipt[0],manifest_from_json(*receipt[1:4]))
        shared=db.execute("SELECT 1 FROM jobs j JOIN job_operation_members m USING(job_id) WHERE m.operation_id<>? AND "
            "((j.kind IN ('SUBMIT_TORRENT','DOWNLOAD_TORRENT') AND json_extract(j.payload_json,'$.preparation_job_id')=?) OR "
            "(j.kind IN ('RESERVE_EPISODE','PREPARE_ACQUISITION') AND json_extract(j.payload_json,'$.evaluation_job_id')=?)) LIMIT 1",
            (op,preparation,plan.evaluation_job_id)).fetchone()
        intent=_intent(db,plan)
        if shared or intent is None or intent.state!='OWNED_STOPPED':raise CancellationError()
        selected=plan.manifest.files[plan.manifest.selected_index]
        if publication['source_root']!=intent.options.download_root or publication['source']['path']!=selected.path or publication['source']['file'][2]!=selected.size:raise CancellationError()
        return op,intent,tuple(protected),publication['source']['root']

    def _capture(self,cleaner):
        captured=cleaner.capture()
        if captured['root']!=self.source_root_identity:raise CancellationError()
        return captured

    def _pin(self,cleaner):
        # The original qBittorrent folder, or already gone.
        pinned=cleaner.root_only()
        if pinned['root'] not in (None,self.source_root_identity):raise CancellationError()
        return pinned

    def _fence(self,db=None):
        if self.cancelled.is_set():raise CancellationError()
        if db is not None:self.facts(db)
        else:
            with self.queue.connection() as connection,transaction(connection):self.facts(connection)

    def run(self,claim,decision,cancelled,*,stage_job=None):
        self.claim,self.decision,self.cancelled,self.stage_job=claim,decision,cancelled,stage_job
        self.guard=self._fence
        with self.queue.connection() as db,transaction(db):op,intent,protected,self.source_root_identity=self.facts(db)
        with OperationGate(self.queue.database).hold(op,cleanup=True):
            self._fence()
            factory=self.files_factory
            self.files_factory=lambda target,roots:factory(target,(*roots,*protected))
            return self._clean_target(op,intent)


def _persist(db,claim,value):
    if type(value) is tuple:   # ('staging', STAGE_TRIAL_MEDIA job)
        row=db.execute('SELECT c.phase FROM cancellation_cleanup c JOIN staging_publications s USING(plan_id) '
            'WHERE s.job_id=?',(value[1],)).fetchall()
        if row!=[('COMPLETE',)]:raise CompletionRejected(Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW'))
        return
    row=db.execute('SELECT c.phase FROM cancellation_cleanup c JOIN staging_publications s USING(plan_id) '
        'JOIN job_operation_members m ON m.job_id=s.job_id JOIN management_actions a ON a.operation_id=m.operation_id '
        'WHERE a.job_id=? AND c.operation_id=a.operation_id',(value,)).fetchall()
    if row!=[('COMPLETE',)]:raise CompletionRejected(Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW'))


def registration(secrets,*,service_factory=Service):
    async def execute(context):
        try:
            payload=context.data()
            if set(payload)=={'staging_job_id'}:
                decision,stage_job=None,integer(payload['staging_job_id'],positive=True)
            elif set(payload)=={'decision_job_id'}:
                decision,stage_job=integer(payload['decision_job_id'],positive=True),None
            else:raise ValueError()
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        try:done=await blocking(service_factory(context.configuration,secrets).run,context.claim,decision,
                                context.cancelled,stage_job=stage_job)
        except CleanupBusy:return Outcome('defer','DECISION_DOWNLOAD_CLEANUP_WAITING',delay_ms=1000)
        except ClientUnavailable:return Outcome('retry','DECISION_DOWNLOAD_CLEANUP_UNAVAILABLE')
        except OSError:return Outcome('retry','DECISION_DOWNLOAD_CLEANUP_UNAVAILABLE')
        except CancellationError as exc:
            check,path=failure_location(exc)
            facts={}
            if check:facts['failed_check']=check
            if path:facts['failed_path']=path
            return Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW',facts=facts if facts else None)
        except (ValueError,KeyError,TypeError):return Outcome('review','DECISION_DOWNLOAD_CLEANUP_REVIEW')
        if not done:return Outcome('defer','DECISION_DOWNLOAD_CLEANUP_WAITING',delay_ms=30000)
        return Outcome(value=decision if stage_job is None else ('staging',stage_job))
    return Registration(KIND,DEFINITION,execute,persist=_persist)
