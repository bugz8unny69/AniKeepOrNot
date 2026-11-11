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
        require_allowed(db,staged(db,operation(db,row[0])[0])[0],keep=True)

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
            if phase=='ADD_UNCERTAIN':only_adder(db,claim.job_id,data)
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


class SeriesAddBusy(RuntimeError):
    """Another running Keep job is adding the same series to Sonarr; wait for it."""


# Keep jobs that may still act. A job stopped for review no longer holds the add.
ACTIVE=('PENDING','RUNNING','RETRY_WAIT')
UNFINISHED=('PENDING','RUNNING','RETRY_WAIT','ON_HOLD','NEEDS_REVIEW','BLOCKED')


def only_adder(db,job_id,data):
    """One Keep job adds a series at a time (work item P1 1.2).

    Called in the transaction that records ADD_UNCERTAIN, before the POST. The
    other job then finds the series on its next attempt instead of adding it.
    """
    tvdb=data['selection']['tvdb_series_id']
    for table in ('keep_imports','batch_keep_imports'):
        if db.execute(f"SELECT 1 FROM {table} k JOIN jobs j USING(job_id) WHERE k.job_id<>? AND k.phase='ADD_UNCERTAIN' "
                      "AND json_extract(k.data_json,'$.selection.tvdb_series_id')=? "
                      f"AND j.state IN {ACTIVE}",(job_id,tvdb)).fetchone():
            raise SeriesAddBusy()


def highest_regular_episode(db,anidb_id):
    """The highest regular AniDB episode number known locally; never a request.

    The cached AniDB record decides when it is still cached. Otherwise the
    highest episode AniKeepOrNot verified for this anime is used.
    """
    from src.anime.providers.parsing import episode_catalog
    from src.anime.providers.errors import ProviderInvalidResponse
    row=db.execute("SELECT payload FROM provider_public_cache WHERE provider='anidb' AND cache_key=?",
                   (f'anime:{anidb_id}',)).fetchone()
    if row is not None:
        try:
            catalog=episode_catalog(row[0],anidb_id)
            if catalog:return max(catalog)
        except (ProviderInvalidResponse,ValueError):pass
    return db.execute("SELECT max(n) FROM (SELECT max(anidb_episode_number) AS n FROM release_verifications WHERE anidb_id=? "
                      "UNION ALL SELECT max(anidb_episode_number) FROM recognized_episodes WHERE anidb_id=?)",
                      (anidb_id,anidb_id)).fetchone()[0]


def monitor_mode(highest,options):
    """Sonarr's addOptions.monitor for a new series (work item P1 1.4)."""
    threshold=options.long_running_threshold
    if threshold==0 or options.backfill_long_running or highest is None or highest<threshold:return 'all'
    return 'future'


def _other_keep_unfinished(db,job_id,anidb_id):
    """Another Keep job of this anime has not finished its imports yet."""
    rows=db.execute(
        "SELECT j.job_id,j.kind,a.operation_id FROM jobs j JOIN management_actions a ON a.job_id=j.job_id AND a.action='KEEP' "
        f"WHERE j.job_id<>? AND j.state IN {UNFINISHED} AND ("
        " EXISTS(SELECT 1 FROM job_operation_members m JOIN staging_publications s ON s.job_id=m.job_id "
        "  JOIN acquisition_plans p ON p.plan_id=s.plan_id JOIN episode_reservations r ON r.reservation_id=p.reservation_id "
        "  WHERE m.operation_id=a.operation_id AND r.anidb_id=?)"
        " OR EXISTS(SELECT 1 FROM job_operation_members m JOIN batch_staging_publications s ON s.job_id=m.job_id "
        "  JOIN batch_episode_reservations r ON r.plan_id=s.plan_id AND r.file_index=s.file_index "
        "  WHERE m.operation_id=a.operation_id AND r.anidb_id=?))",(job_id,anidb_id,anidb_id)).fetchall()
    for other,kind,operation_id in rows:
        # A job whose imports are all complete is only finishing up.
        if kind=='KEEP_BATCH':
            done=db.execute("SELECT count(*) FROM batch_keep_imports WHERE job_id=? AND phase='COMPLETE'",(other,)).fetchone()[0]
            total=db.execute("SELECT count(*) FROM batch_staging_publications s JOIN job_operation_members m ON m.job_id=s.job_id "
                             "WHERE m.operation_id=? AND s.state='PUBLISHED'",(operation_id,)).fetchone()[0]
            if total==0 or done<total:return True
        elif db.execute("SELECT phase FROM keep_imports WHERE job_id=?",(other,)).fetchone()!=('COMPLETE',):
            return True
    return False


def take_search(queue,claim,anidb_id):
    """Record the one missing-episode search before it is sent (P1 1.3).

    True only for the Keep job that finishes the series' last import, once per
    series: the time is stored on the series' ACCEPT decision first, so a lost
    reply is never followed by a second search.
    """
    from src.anime.storage.cancellation import operation
    with queue.connection() as db,transaction(db):
        queue._owns(db,claim,queue.now())
        if _other_keep_unfinished(db,claim.job_id,anidb_id):return False
        decision=db.execute('SELECT decision,sonarr_search_sent_at_ms FROM anime_review_decisions WHERE anidb_id=?',
                            (anidb_id,)).fetchone()
        now=queue.now()
        if decision is None:
            db.execute("INSERT INTO anime_review_decisions(anidb_id,operation_id,job_id,decision,cleanup_state,"
                       "decided_at_ms,sonarr_search_sent_at_ms) VALUES(?,?,?,'ACCEPT','COMPLETE',?,?)",
                       (anidb_id,operation(db,claim.job_id)[0],claim.job_id,now,now))
            return True
        if decision!=('ACCEPT',None):return False
        db.execute('UPDATE anime_review_decisions SET sonarr_search_sent_at_ms=? WHERE anidb_id=?',(now,anidb_id))
        return True
