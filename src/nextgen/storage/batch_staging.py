"""Durable per-episode batch staging using the existing no-replace Publisher.

Separate publication rows share the existing inode-pinned artifact journal.
Media is COPY-only; no source video is removed from the qBittorrent root.
"""
import json
import os
import fcntl
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath
from types import SimpleNamespace

from src.nextgen.anime_titles import title_for
from src.nextgen.staging import (
    StagingReview, disjoint, specification, root_identity, encoded,
)
from src.nextgen.storage.batch_unlock_journal import _decode_media
from src.nextgen.storage.cancellation import operation, mark_staged
from src.nextgen.storage.staging import Journal
from src.nextgen.storage.sqlite import transaction


def facts(db,claim,verification_job_id):
    row=db.execute(
        "SELECT v.plan_id,v.file_index,v.unlock_job_id,v.submission_fingerprint,"
        "v.transfer_fingerprint,v.evidence_json,j.state,j.kind,j.event_id,"
        "m.operation_id,s.save_path,s.state,"
        "r.anidb_id,r.anidb_episode_id,r.tvdb_series_id,r.tvdb_season,"
        "r.tvdb_episode,r.tvdb_episode_id,a.title,r.state,"
        "j.job_id "
        "FROM batch_episode_media v "
        "JOIN jobs j ON j.job_id=v.verification_job_id "
        "JOIN job_operation_members m ON m.job_id=j.job_id "
        "JOIN batch_torrent_submissions s ON s.plan_id=v.plan_id "
        "JOIN batch_episode_reservations r "
        "ON r.plan_id=v.plan_id AND r.file_index=v.file_index "
        "JOIN identified_anime a ON a.anidb_id=r.anidb_id "
        "WHERE v.verification_job_id=?",
        (verification_job_id,)).fetchone()
    if (row is None or row[6:9]!=('DONE','VERIFY_BATCH_EPISODE',claim.event_id)
            or row[9]!=operation(db,claim.job_id)[0]
            or row[11]!='OWNED_STOPPED' or row[19]!='ACTIVE'):
        raise StagingReview()
    child=db.execute('SELECT created_by_job_id,kind,event_id FROM jobs WHERE job_id=?',
                     (claim.job_id,)).fetchone()
    if child!=(verification_job_id,'STAGE_BATCH_EPISODE',claim.event_id):
        raise StagingReview()
    op=operation(db,claim.job_id)
    if op[1]!='ACTIVE':raise StagingReview()
    try:
        owner=db.execute(
            'SELECT reservation_job_id FROM batch_torrent_submissions WHERE plan_id=?',
            (row[0],)).fetchone()
        if owner is None:raise StagingReview()
        source=_decode_media(row[5],owner[0],row[2],row[0],row[3],row[4])
        if source.relative_path!=json.loads(row[5])['path']:
            raise StagingReview()
        evidence=json.loads(source.json())
    except (TypeError,ValueError,KeyError,IndexError):
        raise StagingReview() from None
    planned=db.execute(
        "SELECT f.file_path,f.file_size_bytes,r.tvdb_episode "
        "FROM batch_user_evaluation_files t JOIN batch_user_evaluation_plans p USING(plan_id) "
        "JOIN batch_verified_files f ON f.batch_id=p.batch_id AND f.file_index=t.file_index "
        "JOIN batch_episode_reservations r ON r.plan_id=t.plan_id "
        "AND r.file_index=t.file_index AND r.state='ACTIVE' "
        "WHERE t.plan_id=? AND t.file_index=?",
        (row[0],row[1])).fetchone()
    if planned!=(source.relative_path,source.file_signature[2],row[16]):
        raise StagingReview()
    # Never overwrite an already staged single-episode release or another pack
    # for the same canonical placement.
    if db.execute(
        "SELECT 1 FROM staging_publications p "
        "JOIN acquisition_plans a ON a.plan_id=p.plan_id "
        "JOIN episode_reservations r ON r.reservation_id=a.reservation_id "
        "WHERE p.state='PUBLISHED' AND r.tvdb_series_id=? "
        "AND r.tvdb_season=? AND r.tvdb_episode=? LIMIT 1",
        (row[14],row[15],row[16])).fetchone():
        raise StagingReview()
    if db.execute(
        "SELECT 1 FROM batch_staging_publications p "
        "JOIN batch_episode_reservations r ON r.plan_id=p.plan_id AND r.file_index=p.file_index "
        "WHERE p.state='PUBLISHED' AND r.tvdb_series_id=? "
        "AND r.tvdb_season=? AND r.tvdb_episode=? "
        "AND (p.plan_id<>? OR p.file_index<>?) LIMIT 1",
        (row[14],row[15],row[16],row[0],row[1])).fetchone():
        raise StagingReview()
    identity=tuple(row[12:19])
    intent=SimpleNamespace(options=SimpleNamespace(download_root=row[10]))
    return row[0],row[1],intent,evidence,identity


class BatchStagingJournal(Journal):
    @contextmanager
    def lock(self,claim):
        # One operation-scoped lock serializes sibling episode publications.
        # The last completing job can therefore safely mark the whole
        # operation STAGED after all siblings have completed.
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            opid=operation(db,claim.job_id)[0]
        from src.nextgen.maintenance import MaintenanceGate
        from src.nextgen.staging import StagingBusy
        with MaintenanceGate(self.queue.database)._directory() as directory_fd:
            fd=os.open('.nextgen-batch-stage-'+str(opid)+'.lock',
                       os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,
                       0o600,dir_fd=directory_fd)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                    raise StagingReview()
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise StagingBusy() from None
                yield
            finally:os.close(fd)

    def prepare(self,claim,verification_job_id,configuration):
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            plan_id,index,intent,evidence,identity=facts(
                db,claim,verification_job_id)
            old=db.execute(
                'SELECT job_id,verification_job_id,manifest_json '
                'FROM batch_staging_publications WHERE plan_id=? AND file_index=?',
                (plan_id,index)).fetchone()
            if old is not None:
                if old[:2]!=(claim.job_id,verification_job_id):
                    raise StagingReview()
                plan=json.loads(old[2])
                if (plan.get('transfer')!='COPY'
                        or plan['source']!=evidence
                        or plan['source_root']!=intent.options.download_root):
                    raise StagingReview()
                return plan
            root=configuration.value('storage.trial_root')
            disjoint(root,(intent.options.download_root,
                           configuration.value('storage.download_root'),
                           configuration.value('storage.keep_root'),
                           configuration.value('storage.database_path')))
            shown=title_for(db,identity[0],identity[6],configuration.value('anime.title_language'))
            plan=specification(intent,evidence,identity,root,
                               configuration.desired_revision,display_title=shown)
            plan['transfer']='COPY'
            plan['root_identity']=root_identity(root)
            raw=encoded(plan)
            if len(raw.encode())>65536:raise StagingReview()
            db.execute(
                'INSERT INTO batch_staging_publications(plan_id,file_index,job_id,'
                'verification_job_id,manifest_json,state,created_at_ms) '
                "VALUES(?,?,?,?,?,'PLANNED',?)",
                (plan_id,index,claim.job_id,verification_job_id,raw,self.queue.now()))
            self.queue._owns(db,claim,self.queue.now())
            return plan


def persist_batch_stage(db,claim,value):
    if (not db.in_transaction or type(value) is not dict
            or set(value)!={'verification_job_id','receipts'}):
        raise StagingReview()
    from src.nextgen.history import integer
    verification=integer(value['verification_job_id'],positive=True)
    plan_id,index,intent,evidence,identity=facts(db,claim,verification)
    row=db.execute(
        'SELECT job_id,verification_job_id,manifest_json,state '
        'FROM batch_staging_publications WHERE plan_id=? AND file_index=?',
        (plan_id,index)).fetchone()
    if row is None or row[:2]!=(claim.job_id,verification) or row[3]!='PLANNED':
        raise StagingReview()
    plan=json.loads(row[2])
    if plan.get('transfer')!='COPY':raise StagingReview()
    receipts=value['receipts']
    if type(receipts) is not list or len(receipts)!=len(plan['entries']):
        raise StagingReview()
    for entry,receipt in zip(plan['entries'],receipts):
        if (type(receipt) is not dict
                or set(receipt)!={'role','path','root','file','sha256',
                                 'hash_checked','artifact'}
                or receipt['role']!=entry['role']
                or receipt['hash_checked'] is not (entry['role']!='media')
                or receipt['path']!=entry['path']
                or receipt['sha256']!=entry['sha256']
                or type(receipt['file']) is not list or len(receipt['file'])!=5
                or receipt['file'][2]!=entry['size']):
            raise StagingReview()
        if receipt['artifact'] is None:
            if entry['role']!='series':raise StagingReview()
        else:
            artifact=db.execute(
                'SELECT job_id,operation_id,root_path,relative_path,phase '
                'FROM staging_cleanup_artifacts WHERE artifact_id=?',
                (receipt['artifact'],)).fetchone()
            expected=(claim.job_id,operation(db,claim.job_id)[0],
                      plan['root']+'/'+str(PurePosixPath(entry['path']).parent),
                      entry['temporary'],'PUBLISHED')
            if artifact!=expected:raise StagingReview()
    changed=db.execute(
        "UPDATE batch_staging_publications SET state='PUBLISHED',"
        "receipts_json=?,published_at_ms=? "
        "WHERE plan_id=? AND file_index=? AND state='PLANNED'",
        (encoded(receipts),__import__('time').time_ns()//1000000,
         plan_id,index)).rowcount
    if changed!=1:raise StagingReview()
    # Last stage job marks the whole operation STAGED; earlier siblings
    # preserve ACTIVE so retry/cancellation semantics stay meaningful.
    completed=db.execute(
        "SELECT count(*) FROM batch_staging_publications "
        "WHERE plan_id=? AND state='PUBLISHED'",(plan_id,)).fetchone()[0]
    selected=db.execute(
        "SELECT count(*) FROM batch_user_evaluation_files WHERE plan_id=?",
        (plan_id,)).fetchone()[0]
    if completed==selected:
        mark_staged(db,claim)
        # Every selected episode is staged: the torrent has nothing left to
        # give. Remove it and its whole ad-* folder, like single episodes.
        from src.nextgen.jobs.cleanup_batch_download_job import enqueue_after_staging
        enqueue_after_staging(db,claim,plan_id)
    return ()
