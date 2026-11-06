"""Lease-fenced staging intents and publication proof; no filesystem copies in SQL."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import PurePosixPath
import stat

from src.nextgen.anime_titles import title_for
from src.nextgen.acquisition.media import VerifiedMedia
from src.nextgen.history import integer
from src.nextgen.maintenance import MaintenanceGate
from src.nextgen.staging import (StagingReview, StagingConfiguration, StagingBusy, specification,
                                  encoded, disjoint, root_identity)
from src.nextgen.staging_artifacts import record_temporary, publication
from src.nextgen.storage.cancellation import mark_staged, operation
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.media_verification import current as media_current
from src.nextgen.storage.sqlite import transaction


def facts(db,claim,verification):
    row=db.execute("SELECT v.plan_id,v.download_job_id,v.submission_fingerprint,v.transfer_fingerprint,v.evidence_json,"
        "p.job_id,j.event_id,j.state,j.kind,m.operation_id FROM media_verifications v "
        "JOIN acquisition_plans p USING(plan_id) JOIN jobs j ON j.job_id=v.job_id "
        "JOIN job_operation_members m ON m.job_id=j.job_id WHERE v.job_id=?",(verification,)).fetchone()
    if row is None or row[6:9]!=(claim.event_id,'DONE','VERIFY_MEDIA') or row[9]!=operation(db,claim.job_id)[0]:raise StagingReview()
    intent,record=media_current(db,claim,row[5],row[1])
    if intent.fingerprint!=row[2] or record.fingerprint!=row[3]:raise StagingReview()
    try:
        evidence=json.loads(row[4])
        value=VerifiedMedia(row[5],row[1],row[0],row[2],row[3],evidence['path'],tuple(evidence['root']),
            tuple(evidence['file']),evidence['sha256'],evidence['duration_ms'],tuple(evidence['languages']))
        if value.json()!=row[4]:raise StagingReview()
    except (ValueError,KeyError,TypeError):raise StagingReview() from None
    identity_row=db.execute('SELECT r.anidb_id,r.anidb_episode_id,r.tvdb_series_id,r.tvdb_season,r.tvdb_episode,r.tvdb_episode_id,a.title,'
        '(SELECT v.anidb_episode_number FROM release_verifications v WHERE v.anidb_id=r.anidb_id AND v.anidb_episode_id=r.anidb_episode_id LIMIT 1) '
        'FROM acquisition_plans p JOIN episode_reservations r USING(reservation_id) '
        'JOIN identified_anime a ON a.anidb_id=r.anidb_id WHERE p.plan_id=?',(row[0],)).fetchone()
    if identity_row is None:raise StagingReview()
    aid,eid,tid,season,episode,tveid,title,anidb_number=identity_row
    effective_season = 1 if season is None else season
    effective_number = anidb_number if episode is None else episode
    if effective_number is None: effective_number = 1
    identity = (aid,eid,tid,effective_season,effective_number,tveid,title)
    return intent,evidence,identity


class Journal:
    def __init__(self,database):self.queue=JobQueue(database)

    @contextmanager
    def lock(self,claim):
        # An expired attempt can still have a draining I/O thread; serialize
        # publication attempts for this exact job until that thread exits.
        with MaintenanceGate(self.queue.database)._directory() as folder:
            fd=os.open('.nextgen-stage-'+str(claim.job_id)+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,0o600,dir_fd=folder)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink!=1:raise StagingReview()
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise StagingBusy() from None
                yield
            finally:os.close(fd)

    def guard(self,claim):
        with self.queue.connection() as db,transaction(db):self.queue._owns(db,claim,self.queue.now())

    def prepare(self,claim,verification,configuration):
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            intent,evidence,identity=facts(db,claim,verification)
            prior=db.execute('SELECT job_id,verification_job_id,manifest_json FROM staging_publications WHERE plan_id=?',
                             (intent.plan.plan_id,)).fetchone()
            if prior is not None:
                if prior[:2]!=(claim.job_id,verification):raise StagingReview()
                plan=json.loads(prior[2])
                # Pinned transfer mode and destinations survive later changes.
                if plan['source']!=evidence or plan['source_root']!=intent.options.download_root:raise StagingReview()
                return plan
            root=configuration.value('storage.user_evaluation_root')
            disjoint(root,(intent.options.download_root,configuration.value('storage.download_root'),
                configuration.value('storage.keep_root'),configuration.value('storage.database_path')))
            shown=title_for(db,identity[0],identity[6],configuration.value('anime.title_language'))
            plan=specification(intent,evidence,identity,root,configuration.desired_revision,
                               display_title=shown)
            plan['root_identity']=root_identity(root)
            raw=encoded(plan)
            if len(raw.encode())>65536:raise StagingReview()
            db.execute('INSERT INTO staging_publications(plan_id,job_id,verification_job_id,manifest_json,state,created_at_ms) '
                "VALUES(?,?,?,?,'PLANNED',?)",(intent.plan.plan_id,claim.job_id,verification,raw,self.queue.now()))
            self.queue._owns(db,claim,self.queue.now())
            return plan

    def artifact(self,claim,root,name):
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            row=db.execute('SELECT artifact_id,job_id,operation_id,root_device,root_inode,file_device,file_inode,phase '
                'FROM staging_cleanup_artifacts WHERE root_path=? AND relative_path=?',(root,name)).fetchone()
            if row is None:return None
            if row[1:3]!=(claim.job_id,operation(db,claim.job_id)[0]):raise StagingReview()
            return {'id':row[0],'root':list(row[3:5]),'file':list(row[5:7]),'phase':row[7]}

    def register(self,claim,root,name):
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            record_temporary(db,claim,root,name)
            self.queue._owns(db,claim,self.queue.now())
        return self.artifact(claim,root,name)

    def register_move(self,claim,root,name,source):
        # A durable receipt can name the source inode before it is renamed into
        # the planned destination. A missing temp is not deletion authority.
        from src.nextgen.staging_artifacts import _stage_claim
        import re
        if not re.fullmatch(r'\.ad-[a-f0-9]{32}\.partial',name):raise StagingReview()
        destination=root_identity(root)
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now());op=_stage_claim(db,claim)
            db.execute('INSERT INTO staging_cleanup_artifacts(operation_id,job_id,root_path,relative_path,root_device,root_inode,'
                "file_device,file_inode,phase) VALUES(?,?,?,?,?,?,?,?,'TEMP')",
                (op,claim.job_id,root,name,*destination,*source[:2]))
            self.queue._owns(db,claim,self.queue.now())
        return self.artifact(claim,root,name)

    def intent(self,claim,artifact):
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now());publication(db,claim,artifact)
            self.queue._owns(db,claim,self.queue.now())

    def confirm(self,claim,artifact):
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now());publication(db,claim,artifact,confirmed=True)
            self.queue._owns(db,claim,self.queue.now())


def persist(db,claim,value):
    if not db.in_transaction or type(value) is not dict or set(value)!={'verification_job_id','receipts'}:raise StagingReview()
    verification=integer(value['verification_job_id'],positive=True)
    intent,evidence,identity=facts(db,claim,verification)
    row=db.execute('SELECT job_id,verification_job_id,manifest_json,state FROM staging_publications WHERE plan_id=?',
                   (intent.plan.plan_id,)).fetchone()
    if row is None or row[:2]!=(claim.job_id,verification) or row[3]!='PLANNED':raise StagingReview()
    plan=json.loads(row[2]);receipts=value['receipts']
    if type(receipts) is not list or len(receipts)!=3:raise StagingReview()
    for entry,receipt in zip(plan['entries'],receipts):
        if (set(receipt)!={'role','path','root','file','sha256','hash_checked','artifact'} or receipt['role']!=entry['role']
                or receipt['hash_checked'] is not (entry['role']!='media') or receipt['path']!=entry['path'] or receipt['sha256']!=entry['sha256']
                or type(receipt['file']) is not list or len(receipt['file'])!=5 or receipt['file'][2]!=entry['size']):raise StagingReview()
        if receipt['artifact'] is None:
            if entry['role']!='series':raise StagingReview()
        else:
            artifact=db.execute('SELECT job_id,operation_id,root_path,relative_path,phase '
                'FROM staging_cleanup_artifacts WHERE artifact_id=?',(receipt['artifact'],)).fetchone()
            expected=(claim.job_id,operation(db,claim.job_id)[0],plan['root']+'/'+str(PurePosixPath(entry['path']).parent),
                entry['temporary'],'PUBLISHED')
            if artifact!=expected:raise StagingReview()
    db.execute("UPDATE staging_publications SET state='PUBLISHED',receipts_json=?,published_at_ms=? WHERE plan_id=?",
               (encoded(receipts),__import__('time').time_ns()//1000000,intent.plan.plan_id))
    mark_staged(db,claim)
    from src.nextgen.storage.release_candidates import mark_published
    mark_published(db,event_id=claim.event_id,info_hash=intent.plan.manifest.info_hash)
    from src.nextgen.storage.episode_reservations import cancel_staged_waiters
    return cancel_staged_waiters(db,intent.plan.plan_id,__import__('time').time_ns()//1000000)
