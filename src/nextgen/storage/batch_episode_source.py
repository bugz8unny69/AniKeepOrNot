"""Fenced, hash-only proof loader for one completed batch-trial episode."""
from src.nextgen.acquisition.metadata import BatchInventory
from src.nextgen.acquisition.media import MediaInvalid
from src.nextgen.history import integer
from src.nextgen.storage.batch_acquisition_candidate import _candidate
from src.nextgen.storage.batch_submission_journal import _intent
from src.nextgen.storage.batch_unlock_journal import _record, _decode_media
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction


class BatchEpisodeSource:
    def __init__(self,database,*,clock=None):
        self.queue=JobQueue(database,clock=clock)

    def _fence(self,db,claim,unlock_job_id,file_index):
        self.queue._owns(db,claim,self.queue.now())
        if claim.kind!='VERIFY_BATCH_EPISODE':
            raise MediaInvalid()
        row=db.execute(
            "SELECT s.reservation_job_id,s.plan_id,s.info_hash,s.info_sha256 "
            "FROM jobs verifying "
            "JOIN jobs u ON u.job_id=verifying.created_by_job_id "
            "JOIN batch_torrent_unlocks t ON t.unlock_job_id=u.job_id "
            "JOIN batch_torrent_submissions s USING(plan_id) "
            "JOIN batch_user_evaluation_files f ON f.plan_id=s.plan_id AND f.file_index=? "
            "JOIN job_operation_members vm ON vm.job_id=verifying.job_id "
            "JOIN job_operation_members um ON um.job_id=u.job_id "
            "AND um.operation_id=vm.operation_id "
            "JOIN job_operation_members rm ON rm.job_id=s.reservation_job_id "
            "AND rm.operation_id=vm.operation_id "
            "JOIN job_operations op ON op.operation_id=vm.operation_id "
            "WHERE verifying.job_id=? AND u.job_id=? AND u.kind='UNLOCK_BATCH' "
            "AND u.state='DONE' AND t.phase='COMPLETE' AND op.state='ACTIVE' "
            "AND verifying.event_id=u.event_id AND verifying.event_id=?",
            (file_index,claim.job_id,unlock_job_id,claim.event_id)).fetchone()
        if row is None:raise MediaInvalid()
        return row

    def source_hash(self,claim,unlock_job_id,file_index):
        integer(unlock_job_id,positive=True)
        integer(file_index)
        with self.queue.connection() as db,transaction(db):
            row=self._fence(db,claim,unlock_job_id,file_index)
            return row[2]

    def load(self,claim,unlock_job_id,file_index,inventory):
        if type(inventory) is not BatchInventory:raise MediaInvalid()
        with self.queue.connection() as db,transaction(db):
            reservation,plan,digest,sha=self._fence(db,claim,unlock_job_id,file_index)
            if (inventory.info_hash,inventory.info_sha256)!=(digest,sha):
                raise MediaInvalid()
            candidate=_candidate(db,reservation,inventory)
            if candidate.plan_id!=plan or file_index not in {
                    idx for _,idx in candidate.planned_episodes}:
                raise MediaInvalid()
            intent=_intent(db,candidate,inventory)
            if intent is None or intent.state!='OWNED_STOPPED':
                raise MediaInvalid()
            row=db.execute(
                'SELECT m.transfer_job_id,m.submission_fingerprint,'
                'm.transfer_fingerprint,m.evidence_json '
                'FROM batch_probe_media m WHERE m.plan_id=?',(plan,)).fetchone()
            if row is None:raise MediaInvalid()
            media=_decode_media(row[3],reservation,row[0],plan,row[1],row[2])
            record=_record(db,intent,media)
            if record is None or record.phase!='COMPLETE' or record.first_job_id!=unlock_job_id:
                raise MediaInvalid()
            self.queue._owns(db,claim,self.queue.now())
            return intent,record
