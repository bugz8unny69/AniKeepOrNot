"""Shared one-shot owned torrent and manifest-file cleanup journal."""
import json
from src.nextgen.acquisition.cleanup import CleanupPermit, owned
from src.nextgen.acquisition.transfers import STOPPED, CHECKING, ACTIVE
from src.nextgen.cancellation import CancellationError
from src.nextgen.history import job_attempt, append_job_history
from src.nextgen.storage.sqlite import transaction


class OwnedCleanup:
    cleanup_event='CANCELLATION_CONTROL_RECORDED'
    complete_reason='CANCELLATION_COMPLETE'
    pending_reason='CANCELLATION_PENDING'
    # Post-staging cleanups delete the whole owned ad-<marker> folder once the
    # torrent is confirmed gone; cancellations keep the exact-file removal.
    purge=False

    def _capture(self,cleaner):return cleaner.capture()
    def _pin(self,cleaner):return cleaner.root_only()

    def _journal(self,op,intent):
        with self.queue.connection() as db,transaction(db):
            self._fence(db)
            row=db.execute('SELECT operation_id,fingerprint,files_json,phase FROM cancellation_cleanup WHERE plan_id=?',
                           (intent.plan.plan_id,)).fetchone()
            if row is not None:
                if row[:2]!=(op,intent.fingerprint):raise CancellationError()
                return json.loads(row[2]),row[3]
            now=self.queue.now()
            db.execute("INSERT INTO cancellation_cleanup VALUES(?,?,?,?,'PREPARED',?,?)",
                       (intent.plan.plan_id,op,intent.fingerprint,'{}',now,now))
            return {},'PREPARED'
    def _advance(self,intent,prior,phase,files=None):
        with self.queue.connection() as db,transaction(db):
            self._fence(db)
            now=self.queue.now()
            if files is None:
                changed=db.execute('UPDATE cancellation_cleanup SET phase=?,updated_at_ms=? WHERE plan_id=? AND phase=?',
                    (phase,now,intent.plan.plan_id,prior)).rowcount
            else:
                raw=json.dumps(files,sort_keys=True,separators=(',',':'),allow_nan=False)
                if len(raw.encode())>1048576:raise CancellationError()
                changed=db.execute('UPDATE cancellation_cleanup SET phase=?,files_json=?,updated_at_ms=? WHERE plan_id=? AND phase=?',
                    (phase,raw,now,intent.plan.plan_id,prior)).rowcount
            if changed!=1:raise CancellationError()
            receipt=append_job_history(db,job_id=intent.plan.preparation_job_id,event_code=self.cleanup_event,
                reason_code=self.complete_reason if phase=='COMPLETE' else self.pending_reason,
                component=self.component,occurred_at_ms=now,details={'attempt':job_attempt(db,intent.plan.preparation_job_id)})
        self._log((receipt,))
    def _clean_target(self,op,intent):
        files,phase=self._journal(op,intent)
        if phase=='COMPLETE':return True
        credentials=self.secrets.qbittorrent_credentials()
        client=self.client_factory(intent.options,credentials);client.login()
        found=client.inspect(intent.plan.manifest.info_hash)
        cleaner=self.files_factory(intent,(self.configuration.value('storage.trial_root'),self.configuration.value('storage.keep_root')))
        if phase in ('PREPARED','STOP_UNCERTAIN'):
            current=owned(intent,found)
            if current.state in CHECKING:return False
            if current.state not in STOPPED:
                # Stopping is repeatable. After restart, each control rechecks
                # exact ownership rather than trusting a prior HTTP response.
                # Reject unexpected client states instead of guessing.
                if current.state not in ACTIVE:raise CancellationError()
                if phase=='PREPARED':
                    self._advance(intent,phase,'STOP_UNCERTAIN');phase='STOP_UNCERTAIN'
                # The operation gate excludes other handlers while this check
                # runs; cleanup_control revalidates the exact owned torrent
                # immediately before each stop request.
                self._fence()
                client.cleanup_control(intent,intent.fingerprint,CleanupPermit(intent.fingerprint,'stop'),'stop')
                found=client.inspect(intent.plan.manifest.info_hash)
                current=owned(intent,found)
                if current.state not in STOPPED:return False
            files=self._pin(cleaner) if self.purge else self._capture(cleaner)
            self._advance(intent,phase,'REMOVE_UNCERTAIN',files);phase='REMOVE_UNCERTAIN'
        if phase=='REMOVE_UNCERTAIN':
            # A lost removal response is not proof of removal. The qBittorrent
            # command deletes only its *torrent record* (deleteFiles=false);
            # local files are separately protected after absence is confirmed.
            if found is not None:
                current=owned(intent,found)
                if current.state in CHECKING:return False
                if current.state not in STOPPED:
                    if current.state not in ACTIVE:raise CancellationError()
                    # A client restart or another actor may have resumed it.
                    # Never remove an active torrent: stop, then verify.
                    self._fence()
                    client.cleanup_control(intent,intent.fingerprint,
                        CleanupPermit(intent.fingerprint,'stop'),'stop')
                    found=client.inspect(intent.plan.manifest.info_hash)
                    current=owned(intent,found)
                    if current.state not in STOPPED:return False
                # Repeat only after verifying the exact persisted identity,
                # stopped state, current lease and operation ownership. The
                # client independently validates identity immediately before
                # every command. No filesystem deletion is retried here.
                self._fence()
                client.cleanup_control(intent,intent.fingerprint,
                    CleanupPermit(intent.fingerprint,'remove'),'remove')
                found=client.inspect(intent.plan.manifest.info_hash)
                if found is not None:
                    owned(intent,found)
                    return False
            self._advance(intent,phase,'FILES');phase='FILES'
        if phase=='FILES':
            # Reappearance prevents file deletion, even after an earlier absence.
            if found is not None:raise CancellationError()
            self._fence()
            if self.purge:cleaner.purge(files)
            elif self.guard is None:cleaner.remove(files)
            else:cleaner.remove(files,guard=self._fence)
            self._advance(intent,phase,'COMPLETE')
        return True
