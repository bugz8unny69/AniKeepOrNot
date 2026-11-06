"""API-ready cancellation library; invoke only after management authorization.

HTTP callers submit control jobs. Only a job in a distinct control operation
may invoke cleanup under existing maintenance admission, with a lease guard.
Cleanup always requires exclusive drain protection for the target operation.
"""
from contextlib import nullcontext
from src.nextgen.acquisition.qbittorrent import Client
from src.nextgen.acquisition.submission import ClientUnavailable, ClientInvalid, ClientAuthenticationFailed, ClientUnsupported
from src.nextgen.cancellation import CancellationError, CleanupBusy, OperationGate
from src.nextgen.diagnostics import log_history
from src.nextgen.history import job_attempt, append_job_history
from src.nextgen.maintenance import MaintenanceGate
from src.nextgen.storage.cancellation import CancellationStore, operation, cancel_job
from src.nextgen.storage.sqlite import transaction


class CancellationService:
    def __init__(self,configuration,secrets,*,client_factory=Client,files_factory=None,logger=None,admitted=False,guard=None,component="api"):
        if type(admitted) is not bool or admitted and not callable(guard):raise CancellationError()
        from src.nextgen.history import component_name
        self.component=component_name(component)
        self.configuration,self.secrets=configuration,secrets
        self.database=configuration.value('storage.database_path')
        self.store=CancellationStore(self.database)
        self.queue=self.store.queue
        self.client_factory,self.files_factory,self.logger=client_factory,files_factory,logger
        self.gate=MaintenanceGate(self.database)
        self.admitted,self.guard=admitted,guard
    def _admission(self):
        return nullcontext() if self.admitted else self.gate.operation()
    def _fence(self,db=None):
        if self.guard is not None:self.guard(db)
    def _log(self,records):
        if self.logger is not None:
            for record in records:log_history(self.logger,record)
    def status(self,job_id):
        with self._admission():return self.store.status(job_id)
    def request(self,job_id):
        with self._admission():
            op,receipts=self.store.request(job_id)
            self._log(receipts)
            return op
    def cancel(self,job_id):
        self.request(job_id)
        return self.cleanup(job_id)
    def _state(self,job_id,state,reason,*,cause=None,check=None,path=None):
        with self.store.queue.connection() as db,transaction(db):
            self._fence(db)
            op=operation(db,job_id)
            if op[1] not in ('CANCEL_REQUESTED','CANCELED'):raise CancellationError()
            now=self.store.queue.now()
            db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state=?,reason_code=?,updated_at_ms=? WHERE operation_id=?",
                       (state,reason,now,op[0]))
            # A repeated review still records which check failed this time.
            records=() if (op[2],op[3])==(state,reason) and check is None else (append_job_history(db,job_id=job_id,
                event_code='CANCELLATION_CLEANUP_CHANGED',reason_code=reason,component=self.component,occurred_at_ms=now,
                details={**({} if cause is None else {'cause_reason_code':cause}),
                         **({} if check is None else {'failed_check':check}),
                         **({} if path is None else {'failed_path':path}),
                         'attempt':job_attempt(db,job_id)}),)
            if state=='COMPLETE':
                from src.nextgen.storage.reservation_release import cleanup_completed
                records+=cleanup_completed(db,op[0],job_id,now,component=self.component,
                                           previous_cleanup_state=op[2])
        self._log(records)
    def cleanup(self,job_id,*,max_targets=1):
        """Delete what the canceled work owns (see cancel_cleanup), then free its slots.

        qBittorrent not answering waits and tries again; a wrong password or a
        file that cannot be deleted stops for review naming the exact cause.
        """
        from src.nextgen import cancel_cleanup as owned
        from src.nextgen.acquisition.settings import DownloadOptions
        with self._admission():
            status=self.store.status(job_id)
            if not status['can_cleanup']:return status
            operation_id=status['operation_id']
            try:
                with OperationGate(self.database).hold(operation_id,cleanup=True):
                    with self.store.queue.connection() as db,transaction(db):
                        self._fence(db)
                        receipts=[]
                        for (member,) in db.execute('SELECT job_id FROM job_operation_members WHERE operation_id=?',(operation_id,)).fetchall():
                            receipts.extend(cancel_job(db,member,self.store.queue.now(),self.component))
                        torrents=owned.owned_torrents(db,operation_id)
                        files,series=owned.staging_files(db,operation_id)
                    self._log(receipts)
                    credentials=self.secrets.qbittorrent_credentials() if torrents else None
                    clients={}
                    for info_hash,marker,base_url,save_path,category,timeout in torrents:
                        self._fence()
                        if base_url not in clients:
                            client=self.client_factory(DownloadOptions(base_url,save_path,category,timeout),credentials)
                            client.login();clients[base_url]=client
                        found=clients[base_url].delete_owned(info_hash,marker,save_path)
                        if found=='moved':
                            # Tag says ours but qBittorrent keeps it elsewhere: delete nothing.
                            raise CancellationError('TORRENT_MOVED',save_path)
                        if found in ('absent','deleted'):
                            owned.remove_download_folder(save_path,marker)
                        # 'foreign': same hash, not ours. Its folder is left alone too.
                    self._fence()
                    owned.remove_staging(files,series,keep_root=self.configuration.value('storage.keep_root'))
                    with self.store.queue.connection() as db,transaction(db):
                        self._fence(db)
                        db.execute("UPDATE staging_cleanup_artifacts SET phase='REMOVED' WHERE operation_id=?",(operation_id,))
                    self._state(job_id,'COMPLETE','CANCELLATION_COMPLETE')
            except CleanupBusy:
                # Handlers are still stopping; try again shortly.
                return self.store.status(job_id)
            except (ClientUnavailable,ClientInvalid):
                self._state(job_id,'PENDING','CANCELLATION_PENDING',cause='SERVICE_UNAVAILABLE')
            except (ClientAuthenticationFailed,ClientUnsupported,OSError,CancellationError,ValueError) as exc:
                from src.nextgen.reasons import get_reason
                from src.nextgen.cancellation import failure_location
                cause=getattr(exc,'reason_code','STORAGE_UNAVAILABLE' if isinstance(exc,OSError) else 'CANCELLATION_REVIEW')
                check,path=failure_location(exc)
                if self.logger is not None:
                    try:self.logger.warning('Cancellation cleanup for job %s stopped at %s%s',job_id,
                                            check or 'an unknown check',' path='+repr(path) if path else '')
                    except Exception:pass  # A broken log sink never changes cleanup state.
                self._state(job_id,'REVIEW','CANCELLATION_REVIEW',cause=cause if get_reason(cause) is not None else 'CANCELLATION_REVIEW',
                            check=check,path=path)
                return {**self.store.status(job_id),'failed_check':check,'failed_path':path}
            return self.store.status(job_id)
