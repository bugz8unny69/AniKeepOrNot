"""Strict source loading for an E01 media verifier, with no qB mutations."""
from src.nextgen.acquisition.metadata import BatchInventory
from src.nextgen.acquisition.media import MediaInvalid
from src.nextgen.storage.batch_acquisition_candidate import _candidate
from src.nextgen.storage.batch_submission_journal import _intent
from src.nextgen.storage.batch_transfer_journal import _record
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction
from src.nextgen.history import integer


class BatchProbeSource:
    def __init__(self,database,*,clock=None):
        self.queue=JobQueue(database,clock=clock)

    def _fence(self,db,claim,transfer_job_id):
        self.queue._owns(db,claim,self.queue.now())
        if claim.kind!='VERIFY_BATCH_PROBE':raise MediaInvalid()
        row=db.execute(
            "SELECT transfer.created_by_job_id,transfer.event_id,transfer.state,"
            "transfer.kind,op.state "
            "FROM jobs verifying "
            "JOIN jobs transfer ON transfer.job_id=verifying.created_by_job_id "
            "JOIN job_operation_members vm ON vm.job_id=verifying.job_id "
            "JOIN job_operation_members tm ON tm.job_id=transfer.job_id "
            "AND tm.operation_id=vm.operation_id "
            "JOIN job_operations op ON op.operation_id=vm.operation_id "
            "WHERE verifying.job_id=? AND transfer.job_id=?",
            (claim.job_id,transfer_job_id)).fetchone()
        if row is None or row[1:]!=(claim.event_id,'DONE','TRANSFER_BATCH','ACTIVE'):
            raise MediaInvalid()
        submission_job_id=row[0]
        submission=db.execute(
            "SELECT s.reservation_job_id,s.info_hash,s.info_sha256,s.plan_id "
            "FROM batch_torrent_submissions s WHERE s.first_job_id=? "
            "AND s.state='OWNED_STOPPED'",(submission_job_id,)).fetchone()
        if submission is None:raise MediaInvalid()
        return submission

    def source_hash(self,claim,transfer_job_id):
        integer(transfer_job_id,positive=True)
        with self.queue.connection() as db,transaction(db):
            info=self._fence(db,claim,transfer_job_id)
            self.queue._owns(db,claim,self.queue.now())
            return info[1]

    def load(self,claim,transfer_job_id,inventory):
        if type(inventory) is not BatchInventory:raise MediaInvalid()
        with self.queue.connection() as db,transaction(db):
            reservation,info_hash,sha,plan=self._fence(db,claim,transfer_job_id)
            if (info_hash,sha)!=(inventory.info_hash,inventory.info_sha256):
                raise MediaInvalid()
            candidate=_candidate(db,reservation,inventory)
            if candidate.plan_id!=plan:raise MediaInvalid()
            intent=_intent(db,candidate,inventory)
            if intent is None or intent.state!='OWNED_STOPPED':raise MediaInvalid()
            record=_record(db,intent)
            if (record is None or record.phase!='COMPLETE'
                    or record.progress!=1 or record.transfer_job_id!=transfer_job_id):
                raise MediaInvalid()
            self.queue._owns(db,claim,self.queue.now())
            return intent,record
