"""Read-only, durable batch acquisition candidate loader.

Rechecks completed reservation, authoritative provider facts, torrent hash,
every selected file size and index and active operation ownership. The result
is NOT a torrent submission permit or proof of download/audio/staging.
"""
from dataclasses import dataclass
import sqlite3

from src.anime.acquisition.metadata import BatchInventory, ObservedBatchInventory, AcquisitionConflict
from src.anime.history import integer
from src.anime.storage.job_queue import QueueUnavailable
from src.anime.storage.migrations import LATEST_VERSION, MigrationError, _open


@dataclass(frozen=True)
class BatchAcquisitionCandidate:
    reservation_job_id: int
    plan_id: int
    info_hash: str
    info_sha256: str | None
    probe_index: int
    planned_episodes: tuple[tuple[int,int],...]
    skipped_indices: tuple[int,...]
    planned_size_bytes: int


def _candidate(db,reservation_job_id,inventory,*,cleanup=False):
    owner_state=("AND j.state='DONE' AND o.state IN ('ACTIVE','STAGED') "
                 if cleanup else "AND j.state='DONE' AND o.state='ACTIVE' ")
    rows=db.execute(
        "SELECT r.plan_id,r.file_index,r.tvdb_episode,b.info_hash,b.info_sha256,"
        "p.anchor_episode,p.planned_size_bytes,f.file_path,f.file_size_bytes "
        "FROM batch_episode_reservations r JOIN batch_user_evaluation_plans p USING(plan_id) "
        "JOIN batch_verifications b USING(batch_id) "
        "JOIN batch_verified_files f ON f.batch_id=b.batch_id AND f.file_index=r.file_index "
        "JOIN batch_user_evaluation_files t ON t.plan_id=p.plan_id AND t.file_index=r.file_index "
        "AND t.tvdb_episode=r.tvdb_episode AND t.selected_size_bytes=f.file_size_bytes "
        "JOIN jobs j ON j.job_id=r.owner_job_id "
        "JOIN job_operation_members m ON m.job_id=j.job_id "
        "JOIN job_operations o USING(operation_id) "
        "WHERE r.owner_job_id=? AND r.state='ACTIVE' AND j.kind='RESERVE_BATCH' "
        + owner_state +
        "ORDER BY r.tvdb_episode,r.file_index",
        (reservation_job_id,)).fetchall()
    if not rows:raise AcquisitionConflict()
    plan,_,_,info_hash,sha,anchor,total,_,_=rows[0]
    if (info_hash,sha)!=(inventory.info_hash,inventory.info_sha256) or anchor!=1:
        raise AcquisitionConflict()
    count=db.execute(
        "SELECT count(*) FROM batch_user_evaluation_files WHERE plan_id=?",(plan,)).fetchone()[0]
    if len(rows)!=count or len(rows)>1000:
        raise AcquisitionConflict()
    if db.execute(
        "SELECT 1 FROM acquisition_plans WHERE info_hash=? AND state='PREPARED' LIMIT 1",
        (info_hash,)).fetchone():
        raise AcquisitionConflict()
    if db.execute(
        "SELECT 1 FROM batch_episode_reservations r "
        "JOIN batch_user_evaluation_plans p USING(plan_id) JOIN batch_verifications b USING(batch_id) "
        "WHERE r.state='ACTIVE' AND b.info_hash=? AND r.plan_id<>? LIMIT 1",
        (info_hash,plan)).fetchone():
        raise AcquisitionConflict()
    from src.anime.storage.review_decisions import require_allowed, AnimeRejected
    aid=db.execute("SELECT b.anidb_id FROM batch_user_evaluation_plans p "
                   "JOIN batch_verifications b USING(batch_id) WHERE p.plan_id=?",
                   (plan,)).fetchone()
    if aid is None:raise AcquisitionConflict()
    if not cleanup:
        try:require_allowed(db,aid[0])
        except AnimeRejected:raise AcquisitionConflict() from None
    selected,indices,probe,budget=[],set(),None,0
    for current_plan,index,episode,current_hash,current_sha,current_anchor,current_total,path,size in rows:
        if ((current_plan,current_hash,current_sha,current_anchor,current_total)!=(plan,info_hash,sha,anchor,total)
                or type(index) is not int or index<0 or index>=len(inventory.files)
                or index in indices or episode<1):
            raise AcquisitionConflict()
        actual=inventory.files[index]
        if actual.path!=path or actual.size!=size or actual.size<=0:
            raise AcquisitionConflict()
        indices.add(index)
        selected.append((episode,index))
        budget+=size
        if episode==anchor:
            if probe is not None:raise AcquisitionConflict()
            probe=index
    if (probe is None or total!=budget
            or len({episode for episode,index in selected})!=len(selected)):
        raise AcquisitionConflict()
    return BatchAcquisitionCandidate(
        reservation_job_id,plan,info_hash,sha,probe,tuple(selected),
        tuple(file.index for file in inventory.files if file.index not in indices),budget)


def load_batch_acquisition_candidate(database,reservation_job_id,inventory):
    """Inspect a newly re-fetched hash-bound torrent without any qB mutation."""
    integer(reservation_job_id,positive=True)
    if type(inventory) not in (BatchInventory,ObservedBatchInventory):raise AcquisitionConflict()
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:
            raise QueueUnavailable()
        return _candidate(db,reservation_job_id,inventory)
    except (OSError,sqlite3.Error,MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()
