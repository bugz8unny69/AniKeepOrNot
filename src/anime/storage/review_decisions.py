"""Explicit rejected-anime decisions and selected staging cleanup."""
import json
import os
import stat
import hashlib

from src.anime.acquisition.cleanup import directory, parent
from src.anime.acquisition.metadata import AcquisitionConflict
from src.anime.staging import StagingReview, disjoint


class AnimeRejected(AcquisitionConflict):
    reason_code='ANIME_REJECTED'
    def __init__(self):ValueError.__init__(self,'This anime was rejected; automatic acquisition is stopped.')


def require_allowed(db,anidb_id):
    if db.execute("SELECT 1 FROM anime_review_decisions WHERE anidb_id=? AND decision='REJECT'",(anidb_id,)).fetchone():raise AnimeRejected()


def staged(db,operation_id):
    rows=db.execute("SELECT r.anidb_id,s.plan_id,s.manifest_json,s.receipts_json FROM staging_publications s "
        "JOIN acquisition_plans p USING(plan_id) JOIN episode_reservations r USING(reservation_id) "
        "JOIN job_operation_members m ON m.job_id=s.job_id WHERE m.operation_id=? AND s.state='PUBLISHED'",
        (operation_id,)).fetchall()
    if len(rows)!=1:raise StagingReview()
    return rows[0]


def remove_selected(manifest,receipts,guard,keep_root):
    """Delete only this decision's video and episode NFO; leave series metadata."""
    plan=json.loads(manifest);proofs=json.loads(receipts)
    disjoint(plan['root'],(keep_root,))
    entries=plan['entries']
    # Preflight the entire proven publication before the first unlink.
    # zip() alone silently truncates missing or extra receipts.
    if (type(entries) is not list or type(proofs) is not list
            or not entries or len(entries)!=len(proofs)):
        raise StagingReview()
    for entry,proof in zip(entries,proofs):
        if (type(entry) is not dict or type(proof) is not dict
                or entry.get('role') not in ('media','episode','series')
                or entry.get('role')!=proof.get('role')
                or entry.get('path')!=proof.get('path')):
            raise StagingReview()
    for entry,proof in zip(entries,proofs):
        if entry['role']=='series':continue
        guard()
        with directory(plan['root']) as root:
            with parent(root,entry['path']) as (folder,name):
                try:
                    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=folder)
                except FileNotFoundError:
                    os.fsync(folder);continue
                try:
                    stat_info = os.fstat(fd)
                    if not stat.S_ISREG(stat_info.st_mode) or stat_info.st_size != entry['size']:
                        raise StagingReview()
                    # NFOs are small: verify the exact content that was published.
                    if entry['role'] == 'episode':
                        digest = hashlib.sha256()
                        while True:
                            chunk = os.read(fd, 65536)
                            if not chunk: break
                            digest.update(chunk)
                        if digest.hexdigest() != entry['sha256']:
                            raise StagingReview()
                finally:
                    os.close(fd)
                guard()
                current = os.stat(name, dir_fd=folder, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or current.st_size != entry['size']:
                    raise StagingReview()
                os.unlink(name, dir_fd=folder); os.fsync(folder)


def _prune_series(manifests,guard):
    """After a whole-series Reject, drop empty season folders, our own
    tvshow.nfo and the then-empty series folder. Never recursive: any file we
    did not write keeps its folders in place."""
    import errno
    from pathlib import PurePosixPath
    targets={}
    for manifest in manifests:
        plan=json.loads(manifest)
        for entry in plan['entries']:
            if entry.get('role')=='series':
                targets.setdefault((plan['root'],entry['path']),set()).add(entry['sha256'])
    for (root_path,nfo_path),hashes in targets.items():
        if len(hashes)!=1:continue  # Disagreeing publication content is not a deletion permit.
        sha=next(iter(hashes))
        guard()
        nfo=PurePosixPath(nfo_path)
        if len(nfo.parts)!=2 or nfo.name!='tvshow.nfo':raise StagingReview()
        with directory(root_path) as root:
            try:series=os.open(nfo.parts[0],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=root)
            except FileNotFoundError:continue
            try:
                for name in os.listdir(series):
                    if name.startswith('Season '):
                        try:os.rmdir(name,dir_fd=series)
                        except OSError as error:
                            if error.errno not in (errno.ENOTEMPTY,errno.EEXIST,errno.ENOTDIR,errno.ENOENT):raise
                if os.listdir(series)==['tvshow.nfo']:
                    fd=os.open('tvshow.nfo',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=series)
                    try:
                        info=os.fstat(fd)
                        same=(info.st_size<1048576 and hashlib.sha256(os.read(fd,info.st_size+1)).hexdigest()==sha)
                    finally:os.close(fd)
                    if same:
                        guard()
                        os.unlink('tvshow.nfo',dir_fd=series);os.fsync(series)
                if os.listdir(series):continue
            finally:os.close(series)
            try:os.rmdir(nfo.parts[0],dir_fd=root);os.fsync(root)
            except OSError as error:
                if error.errno not in (errno.ENOTEMPTY,errno.EEXIST,errno.ENOENT):raise


def remove_series(publications,guard,keep_root):
    """Series Reject: remove every staged video and episode NFO, then the
    empty Season folders, our tvshow.nfo and the empty series folder."""
    for manifest,receipts in publications:
        remove_selected(manifest,receipts,guard,keep_root)
    _prune_series(tuple(manifest for manifest,_ in publications),guard)
