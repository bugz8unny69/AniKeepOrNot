"""One durable Sonarr import per Keep job; external writes consume one intent."""
from dataclasses import asdict
import json
import os
import stat

from src.anime.keep_selection import KeepSelection, load_selection
from src.anime.sonarr import Options, KeepReview
from src.anime.acquisition.cleanup import directory, parent
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.review_decisions import staged, require_allowed
from src.anime.storage.cancellation import operation
from src.anime.storage.sqlite import transaction
from src.anime.checks import here


class Journal:
    def __init__(self,database):self.queue=JobQueue(database)

    def guard(self,db,claim):
        self.queue._owns(db,claim,self.queue.now())
        row=db.execute("SELECT target_job_id FROM management_actions WHERE job_id=? AND action='KEEP'",(claim.job_id,)).fetchone()
        if row is None:raise KeepReview()
        require_allowed(db,staged(db,operation(db,row[0])[0])[0])

    def load(self,claim,target,configuration,*,resolver=None):
        with self.queue.connection() as db,transaction(db):
            self.guard(db,claim)
            row=db.execute('SELECT phase,data_json FROM keep_imports WHERE job_id=?',(claim.job_id,)).fetchone()
            if row:
                data=json.loads(row[1])
                # No external write has been authorized in PLANNED. Allow an
                # explicit retry after correcting settings; keep the file scope.
                if row[0]=='PLANNED':
                    selection=KeepSelection(**data['selection'])
                    options=Options.capture(configuration,selection)
                    from src.anime.staging import disjoint
                    disjoint(options.keep_root,(selection.staging_root,))
                    disjoint(options.library_root,(options.staging_root,))
                    data['options']=options.data()
                    db.execute('UPDATE keep_imports SET data_json=?,updated_at_ms=? WHERE job_id=?',
                               (json.dumps(data,sort_keys=True),self.queue.now(),claim.job_id))
                return row[0],data
            selection=load_selection(db,target,resolver=resolver);options=Options.capture(configuration,selection)
            from src.anime.staging import disjoint
            disjoint(options.keep_root,(selection.staging_root,))
            disjoint(options.library_root,(options.staging_root,))
            source=staged(db,operation(db,target)[0]);plan=json.loads(source[2]);receipts=json.loads(source[3])
            proof=next(p for p in receipts if p['role']=='media')
            data={'selection':asdict(selection),'options':options.data(),'root_identity':plan['root_identity'],
                  'source_file':proof['file'],'source_parent':proof['root']}
            db.execute("INSERT INTO keep_imports(job_id,phase,data_json,updated_at_ms) VALUES(?,'PLANNED',?,?)",
                       (claim.job_id,json.dumps(data,sort_keys=True),self.queue.now()))
            return 'PLANNED',data

    def change(self,claim,prior,phase,data):
        raw=json.dumps(data,sort_keys=True,allow_nan=False)
        if len(raw.encode())>65536:raise KeepReview(failed_check=here('DATA_SIZE_EXCEEDED'))
        with self.queue.connection() as db,transaction(db):
            self.guard(db,claim)
            if db.execute('UPDATE keep_imports SET phase=?,data_json=?,updated_at_ms=? WHERE job_id=? AND phase=?',
                          (phase,raw,self.queue.now(),claim.job_id,prior)).rowcount!=1:
                raise KeepReview(failed_check=here('FENCE_CONFLICT'))

    def fence(self,claim):
        with self.queue.connection() as db,transaction(db):self.guard(db,claim)


def check_source(data):
    selection = KeepSelection(**data['selection'])
    staged_abs = f"{selection.staging_root.rstrip('/')}/{selection.media_relative_path.lstrip('/')}"
    with directory(selection.staging_root) as root:
        with parent(root, selection.media_relative_path) as (folder, name):
            try:
                info = os.stat(name, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                raise KeepReview(failed_check=here('STAGED_FILE_MISSING'),
                                 failed_path=staged_abs) from None
            if not stat.S_ISREG(info.st_mode) or info.st_size != data['source_file'][2]:
                raise KeepReview(failed_check=here('STAGED_FILE_CORRUPT'),
                                 failed_path=staged_abs)
