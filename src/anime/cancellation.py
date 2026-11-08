"""Cancellation capabilities and per-operation runtime drain; no transport/UI."""
from contextlib import contextmanager
import fcntl
from src.anime.history import integer
from src.anime.maintenance import MaintenanceGate, MaintenanceBusy


class CancellationError(ValueError):
    reason_code='CANCELLATION_REVIEW'
    def __init__(self,check=None,path=None):
        super().__init__('Cancellation or cleanup needs review.')
        # Optional name of the failed rule (e.g. TORRENT_MOVED) and its path,
        # recorded in History. Never decides anything.
        self.check,self.path=check,path


def failure_location(exc):
    """Name the check that stopped cleanup: 'file.py:LINE function', plus the
    file path when the root cause was a filesystem error. No raw messages.

    Follows the exception chain (including 'from None') to the original raise.
    """
    import os
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    chain=exc;seen=set()
    while (chain.__cause__ or chain.__context__) is not None and id(chain) not in seen:
        seen.add(id(chain));chain=chain.__cause__ or chain.__context__
    check=None;tb=chain.__traceback__
    while tb is not None:
        try:relative=Path(tb.tb_frame.f_code.co_filename).resolve().relative_to(root).as_posix()
        except ValueError:relative=None
        if relative is not None and relative.startswith('src/anime/'):
            check=f'{relative}:{tb.tb_lineno} {tb.tb_frame.f_code.co_name}'
        tb=tb.tb_next
    named=next((e for e in (exc,chain) if isinstance(e,CancellationError) and e.check),None)
    if check is not None and named is not None:
        check+=' '+named.check
    elif check is not None and isinstance(chain,OSError) and chain.errno is not None:
        import errno as _errno
        check+=' '+_errno.errorcode.get(chain.errno,'EUNKNOWN')
    path=next((e.path for e in (exc,chain) if isinstance(e,CancellationError)
               and type(e.path) is str and e.path.startswith('/')),None)
    if path is not None:pass
    elif isinstance(chain,OSError) and type(chain.filename) is str and chain.filename.startswith('/'):
        path=chain.filename
    elif isinstance(chain,OSError) and type(chain.filename) is bytes:
        path=os.fsdecode(chain.filename)
    return check,path


class CancellationRequested(RuntimeError):
    reason_code='JOB_CANCELED'
    def __init__(self):super().__init__('This work has been canceled.')


class CleanupBusy(CancellationError):
    reason_code='CANCELLATION_DRAINING'
    def __init__(self):ValueError.__init__(self,'Waiting for active work to stop before cleanup.')


class OperationGate:
    """All workers hold shared drain protection across the whole handler attempt.

    Cleanup holds exclusive protection across external and file operations. Only
    this operation drains; ordinary maintenance admission is held separately.
    A process crash drops its lock. Expired SQLite leases alone do not prove drain.
    """
    def __init__(self,database):self.gate=MaintenanceGate(database)
    @contextmanager
    def hold(self,operation_id,*,cleanup=False):
        integer(operation_id,positive=True)
        with self.gate._directory() as directory:
            try:
                with self.gate._lock(directory,'operation-'+str(operation_id),fcntl.LOCK_EX if cleanup else fcntl.LOCK_SH):
                    yield
            except MaintenanceBusy:
                if cleanup:raise CleanupBusy() from None
                raise CancellationRequested() from None
