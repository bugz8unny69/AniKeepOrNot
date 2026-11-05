"""Per-episode Sonarr Keep journal for a staged batch operation.

Implements the trusted keep_service.Service journal interface, but writes one
durable external-command phase per canonical episode instead of treating an
entire season torrent as one episode.
"""
from dataclasses import asdict
import json

from src.nextgen.history import integer
from src.nextgen.keep_selection import KeepSelection
from src.nextgen.sonarr import KeepReview, Options
from src.nextgen.staging import disjoint
from src.nextgen.storage.cancellation import operation
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.review_decisions import require_allowed
from src.nextgen.storage.sqlite import transaction


def batch_publications(db,operation_id):
    rows=db.execute(
        "SELECT p.plan_id,p.file_index,p.manifest_json,p.receipts_json,"
        "r.anidb_id,r.anidb_episode_id,r.tvdb_series_id,r.tvdb_episode_id,"
        "r.tvdb_season,r.tvdb_episode,p.job_id "
        "FROM batch_staging_publications p "
        "JOIN batch_episode_reservations r ON r.plan_id=p.plan_id "
        "AND r.file_index=p.file_index AND r.state='ACTIVE' "
        "JOIN job_operation_members m ON m.job_id=p.job_id "
        "WHERE m.operation_id=? AND p.state='PUBLISHED' "
        "ORDER BY p.file_index",(operation_id,)).fetchall()
    if not rows or len(rows)>32 or len({r[0] for r in rows})!=1:
        raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:33 batch_publications COUNT_INVALID')
    count=db.execute(
        'SELECT count(*) FROM batch_trial_files WHERE plan_id=?',
        (rows[0][0],)).fetchone()[0]
    if len(rows)!=count:
        raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:37 batch_publications MISMATCH_COUNT')
    return rows


class BatchKeepJournal:
    def __init__(self,database,file_index):
        integer(file_index)
        self.queue=JobQueue(database)
        self.file_index=file_index

    def guard(self,db,claim):
        self.queue._owns(db,claim,self.queue.now())
        if claim.kind!='KEEP_BATCH':
            raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:49 guard KIND_NOT_KEEP_BATCH')
        action=db.execute(
            "SELECT a.target_job_id,a.operation_id,a.action "
            "FROM management_actions a WHERE a.job_id=?",
            (claim.job_id,)).fetchone()
        if action is None or action[2]!='KEEP':
            raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:54 guard ACTION_NOT_KEEP')
        if operation(db,action[0])[:2]!=(action[1],'STAGED'):
            raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:56 guard OPERATION_NOT_STAGED')
        rows=batch_publications(db,action[1])
        selected=[row for row in rows if row[1]==self.file_index]
        if len(selected)!=1:
            raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:59 guard FILE_NOT_FOUND')
        row=selected[0]
        require_allowed(db,row[4])
        return action[0],row

    def load(self,claim,target,configuration,*,resolver=None):
        with self.queue.connection() as db,transaction(db):
            parent,row=self.guard(db,claim)
            if parent!=target:
                raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:67 load PARENT_MISMATCH')
            plan,index,manifest,proofs,*rest=row
            old=db.execute(
                'SELECT phase,data_json FROM batch_keep_imports '
                'WHERE job_id=? AND plan_id=? AND file_index=?',
                (claim.job_id,plan,index)).fetchone()
            if old is not None:
                data=json.loads(old[1])
                if old[0]=='PLANNED':
                    selection=KeepSelection(**data['selection'])
                    options=Options.capture(configuration,selection)
                    disjoint(options.keep_root,(selection.staging_root,))
                    disjoint(options.library_root,(options.staging_root,))
                    data['options']=options.data()
                    db.execute(
                        'UPDATE batch_keep_imports SET data_json=?,updated_at_ms=? '
                        'WHERE job_id=? AND file_index=?',
                        (json.dumps(data,sort_keys=True),self.queue.now(),
                         claim.job_id,index))
                return old[0],data
            try:
                recorded=json.loads(manifest)
                receipts=json.loads(proofs)
                media=[e for e in recorded['entries'] if e['role']=='media']
                checked=[e for e in receipts if e['role']=='media']
                if (len(media)!=1 or len(checked)!=1
                        or media[0]['path']!=checked[0]['path']
                        or recorded.get('transfer')!='COPY'):
                    raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:95 load MEDIA_RECEIPT_INVALID')
                identity=tuple(row[4:10])
                selection=KeepSelection(
                    *identity,plan,recorded['root'],media[0]['path'])
                options=Options.capture(configuration,selection)
                disjoint(options.keep_root,(selection.staging_root,))
                disjoint(options.library_root,(options.staging_root,))
                data={
                    'selection':asdict(selection),'options':options.data(),
                    'root_identity':recorded['root_identity'],
                    'source_file':checked[0]['file'],
                    'source_parent':checked[0]['root'],
                }
            except (KeyError,ValueError,TypeError):
                raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:109 load DATA_FORMAT_INVALID') from None
            db.execute(
                "INSERT INTO batch_keep_imports(job_id,plan_id,file_index,phase,"
                "data_json,updated_at_ms) VALUES(?,?,?,'PLANNED',?,?)",
                (claim.job_id,plan,index,json.dumps(data,sort_keys=True),
                 self.queue.now()))
            return 'PLANNED',data

    def change(self,claim,prior,phase,data):
        raw=json.dumps(data,sort_keys=True,allow_nan=False)
        if len(raw.encode())>65536:
            raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:119 change DATA_SIZE_EXCEEDED')
        with self.queue.connection() as db,transaction(db):
            self.guard(db,claim)
            if db.execute(
                'UPDATE batch_keep_imports SET phase=?,data_json=?,updated_at_ms=? '
                'WHERE job_id=? AND file_index=? AND phase=?',
                (phase,raw,self.queue.now(),claim.job_id,self.file_index,prior)
            ).rowcount!=1:
                raise KeepReview(failed_check='src/nextgen/storage/batch_keep_imports.py:126 change FENCE_CONFLICT')

    def fence(self,claim):
        with self.queue.connection() as db,transaction(db):
            self.guard(db,claim)
