"""Staging receipts: which temporary files a staging job wrote (used by cancel cleanup)."""
import os
import re
from src.nextgen.acquisition.cleanup import directory, fingerprint
from src.nextgen.cancellation import CancellationError
from src.nextgen.storage.cancellation import operation


def _stage_claim(db,claim):
    import time
    row=db.execute('SELECT state,lease_token,attempt_count,lease_expires_at_ms FROM jobs WHERE job_id=?',(claim.job_id,)).fetchone()
    if (not db.in_transaction or claim.kind not in ('STAGE_USER_EVALUATION_MEDIA','STAGE_BATCH_EPISODE') or row is None
            or row[:3]!=('RUNNING',claim.token,claim.attempt) or row[3]<=time.time_ns()//1000000):raise CancellationError()
    op=operation(db,claim.job_id)
    if op[1]!='ACTIVE':raise CancellationError()
    return op[0]


def record_temporary(db,claim,root,name):
    """After exclusive temp-file creation, before copying; caller holds operation gate.

    A crash before this receipt is durable leaves an unowned file for review,
    never permission to sweep a folder. Receipt binds directory and file inodes.
    """
    op=_stage_claim(db,claim)
    if type(name) is not str or not re.fullmatch(r'\.ad-[a-f0-9]{32}\.partial',name):raise CancellationError()
    with directory(root) as fd:
        info=os.stat(name,dir_fd=fd,follow_symlinks=False)
        fingerprint(info)
        folder=os.fstat(fd)
        return db.execute('INSERT INTO staging_cleanup_artifacts(operation_id,job_id,root_path,relative_path,root_device,root_inode,'
            "file_device,file_inode,phase) VALUES(?,?,?,?,?,?,?,?,'TEMP')",
            (op,claim.job_id,root,name,folder.st_dev,folder.st_ino,info.st_dev,info.st_ino)).lastrowid


def publication(db,claim,artifact_id,*,confirmed=False):
    """Record intent before rename, confirmed only after verified publication.

    The future staging library owns filesystem publication/proof. This helper
    grants no rename or deletion authority and is not a public API action.
    """
    op=_stage_claim(db,claim)
    if type(confirmed) is not bool:raise CancellationError()
    old,new=('PUBLISH_UNCERTAIN','PUBLISHED') if confirmed else ('TEMP','PUBLISH_UNCERTAIN')
    changed=db.execute('UPDATE staging_cleanup_artifacts SET phase=? WHERE artifact_id=? AND operation_id=? AND job_id=? AND phase=?',
                      (new,artifact_id,op,claim.job_id,old)).rowcount
    if changed!=1:raise CancellationError()
