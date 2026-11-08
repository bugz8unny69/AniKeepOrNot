"""Durable one-shot submission authorization and fenced read-only reconciliation.

Calls run inside an already admitted worker attempt; never nest maintenance
admission. No network call or application log is made inside these transactions.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re

from src.nextgen.acquisition.metadata import AcquisitionConflict, ObservedInventory
from src.nextgen.acquisition.settings import DownloadOptions, SizeEvidenceMissing
from src.nextgen.acquisition.submission import (SubmissionPlan, Intent, SendPermit, manifest_from_json, classify)
from src.nextgen.history import append_job_history, integer
from src.nextgen.storage.acquisition import _source, validate_declaration
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction


def _plan(db,preparation_job_id,event_id,*,cleanup=False):
    row=db.execute("SELECT p.plan_id,p.reservation_id,p.info_hash,p.info_sha256,p.manifest_json,p.submission_marker,"
        "p.selected_size_bytes,r.evaluation_job_id,r.plan_id FROM acquisition_plans p "
        "JOIN jobs j ON j.job_id=p.job_id JOIN episode_reservations r ON r.reservation_id=p.reservation_id "
        "WHERE p.job_id=? AND j.event_id=? AND j.kind='PREPARE_ACQUISITION' AND j.state='DONE' AND p.state='PREPARED' AND r.state='ACTIVE'",
        (preparation_job_id,event_id)).fetchone()
    if row is None:raise AcquisitionConflict()
    source=_source(db,row[7],event_id,cleanup=cleanup)
    manifest=manifest_from_json(row[2],row[3],row[4])
    validate_declaration(source,manifest)
    if source.reservation_id!=row[1] or source.info_hash!=manifest.info_hash or not re.fullmatch('[a-f0-9]{32}',row[5]):raise AcquisitionConflict()
    budget=db.execute('SELECT max_episode_bytes,max_plan_bytes FROM acquisition_budgets WHERE user_evaluation_plan_id=?',(row[8],)).fetchone()
    sizes=db.execute('SELECT p.selected_size_bytes FROM acquisition_plans p JOIN episode_reservations r '
                     "ON r.reservation_id=p.reservation_id WHERE r.plan_id=? AND r.state='ACTIVE' AND p.state='PREPARED'",(row[8],)).fetchall()
    if (row[6]!=manifest.files[manifest.selected_index].size or budget is None or any(n[0] is None for n in sizes)):
        raise SizeEvidenceMissing()
    if row[6]>budget[0] or sum(n[0] for n in sizes)>budget[1]:raise AcquisitionConflict()
    return SubmissionPlan(preparation_job_id,row[0],row[7],event_id,row[5],manifest)


def _fingerprint(plan,options,api_major):
    value={'plan_id':plan.plan_id,'hash':plan.manifest.info_hash,'info_sha256':plan.manifest.info_sha256,
           'inventory_sha256':hashlib.sha256(plan.manifest.json().encode()).hexdigest(),
           'endpoint':options.base_url,'save_path':options.download_root,'category':options.category,
           'timeout':options.timeout_seconds,'marker':plan.marker,'api_major':api_major,
           'stopped':True,'auto_tmm':False,'use_download_path':False,'content_layout':'Original','skip_checking':False}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _intent(db,plan):
    row=db.execute('SELECT base_url,save_path,category,timeout_seconds,state,fingerprint,api_major '
                   'FROM torrent_submissions WHERE plan_id=?',(plan.plan_id,)).fetchone()
    if row is None:return None
    options=DownloadOptions(*row[:4])
    if row[6] not in (4,5) or row[5]!=_fingerprint(plan,options,row[6]):raise AcquisitionConflict()
    return Intent(plan,options,row[4],row[5],row[6])


class SubmissionJournal:
    def __init__(self,database,*,clock=None):self.queue=JobQueue(database,clock=clock)

    def load(self,claim,preparation_job_id):
        integer(preparation_job_id,positive=True)
        with self.queue.connection() as db, transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            plan=_plan(db,preparation_job_id,claim.event_id)
            intent=_intent(db,plan)
            self.queue._owns(db,claim,self.queue.now())
            return plan,intent

    def handed_off(self,claim,intent):
        """Submission is a historical stopped receipt once transfer takes ownership."""
        with self.queue.connection() as db, transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            plan=_plan(db,intent.plan.preparation_job_id,claim.event_id)
            current=_intent(db,plan)
            if current is None or current.fingerprint!=intent.fingerprint:raise AcquisitionConflict()
            result=current.state=='OWNED_STOPPED' and db.execute(
                'SELECT 1 FROM torrent_transfers WHERE plan_id=?',(plan.plan_id,)).fetchone() is not None
            self.queue._owns(db,claim,self.queue.now())
            return result

    def authorize(self,claim,plan,options,api_major,present,configuration_revision):
        if type(present) is not bool or type(api_major) is not int or api_major not in (4,5):raise ValueError('validated inventory/client version required')
        integer(configuration_revision,positive=True)
        with self.queue.connection() as db, transaction(db):
            now=self.queue.now()
            self.queue._owns(db,claim,now)
            if _plan(db,plan.preparation_job_id,claim.event_id)!=plan:raise AcquisitionConflict()
            prior=_intent(db,plan)
            if prior is not None:
                if prior.options!=options:raise AcquisitionConflict()
                return prior,None,()
            if type(plan.manifest) is ObservedInventory:
                # Observations describe an existing admission, never a new send.
                raise AcquisitionConflict()
            fingerprint=_fingerprint(plan,options,api_major)
            state,reason=('EXTERNAL','TORRENT_ALREADY_PRESENT') if present else ('UNCERTAIN','TORRENT_SUBMISSION_UNCERTAIN')
            db.execute('INSERT INTO torrent_submissions(plan_id,first_job_id,first_attempt,base_url,save_path,category,timeout_seconds,'
                       'api_major,fingerprint,configuration_revision,state,reason_code,created_at_ms,updated_at_ms) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (plan.plan_id,claim.job_id,claim.attempt,options.base_url,options.download_root,options.category,
                        options.timeout_seconds,api_major,fingerprint,configuration_revision,state,reason,now,now))
            record=append_job_history(db,job_id=claim.job_id,event_code='TORRENT_SUBMISSION_RECORDED',reason_code=reason,
                                      component='worker',occurred_at_ms=now,details={'attempt':claim.attempt})
            self.queue._owns(db,claim,self.queue.now())
            intent=Intent(plan,options,state,fingerprint,api_major)
            # A crash after this commit loses the permit forever. Absence cannot rearm it.
            return intent,None if present else SendPermit(fingerprint),(record,)

    def guard_send(self,claim,intent):
        with self.queue.connection() as db, transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            plan=_plan(db,intent.plan.preparation_job_id,claim.event_id)
            current=_intent(db,plan)
            if current is None or current.fingerprint!=intent.fingerprint or current.state!='UNCERTAIN':raise AcquisitionConflict()

    def observe(self,claim,intent,observation):
        with self.queue.connection() as db, transaction(db):
            now=self.queue.now()
            self.queue._owns(db,claim,now)
            plan=_plan(db,intent.plan.preparation_job_id,claim.event_id)
            current=_intent(db,plan)
            if current is None or current.fingerprint!=intent.fingerprint:raise AcquisitionConflict()
            if current.state in ('EXTERNAL','REVIEW'):
                return current.state,'TORRENT_ALREADY_PRESENT' if current.state=='EXTERNAL' else 'TORRENT_OWNERSHIP_CONFLICT',()
            if current.state=='OWNED_STOPPED' and db.execute(
                    'SELECT 1 FROM torrent_transfers WHERE plan_id=?',(plan.plan_id,)).fetchone() is not None:
                return 'OWNED_STOPPED','TORRENT_STOPPED_CONFIRMED',()
            # A concurrent reconciler may already have confirmed this intent
            # after our client read. Do not overwrite its newer state.
            if current.state!=intent.state:
                return current.state,('TORRENT_STOPPED_CONFIRMED' if current.state=='OWNED_STOPPED' else 'TORRENT_SUBMISSION_UNCERTAIN'),()
            state,reason=classify(current,observation)
            records=()
            if state!=current.state:
                db.execute('UPDATE torrent_submissions SET state=?,reason_code=?,updated_at_ms=? WHERE plan_id=?',
                           (state,reason,now,plan.plan_id))
                records=(append_job_history(db,job_id=claim.job_id,event_code='TORRENT_RECONCILED',reason_code=reason,
                    component='worker',occurred_at_ms=now,details={'attempt':claim.attempt}),)
                if state=='OWNED_STOPPED':
                    _record_torrent(db,claim.job_id,now,observation)
            self.queue._owns(db,claim,self.queue.now())
            return state,reason,records


def target_options(options,plan):
    return DownloadOptions(options.base_url,str(PurePosixPath(options.download_root)/('ad-'+plan.marker)),
                           options.category,options.timeout_seconds)


def _record_torrent(db,job_id,now,observation):
    """Resources (JD-1c): the confirmed torrent, its folder and the files it lists."""
    from src.nextgen.resources import Resources
    resources=Resources(db,job_id,now)
    save_path=observation.save_path.rstrip('/') or '/'
    torrent=resources.torrent(observation.info_hash,save_path=save_path)
    for item in observation.files:
        resources.downloaded(torrent,save_path+"/"+item.path,item.size)
