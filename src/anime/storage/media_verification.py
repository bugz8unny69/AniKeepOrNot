"""Immutable media evidence in the worker's fenced completion transaction."""
from src.anime.acquisition.media import VerifiedMedia, MediaInvalid
from src.anime.acquisition.transfers import TransferConflict
from src.anime.history import integer
from src.anime.storage.transfers import _current, _record
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import transaction


def current(db,claim,preparation,download):
    integer(preparation,positive=True);integer(download,positive=True)
    row=db.execute("SELECT j.event_id,j.kind,j.state,j.payload_json,m.operation_id FROM jobs j "
        "JOIN job_operation_members m USING(job_id) WHERE j.job_id=?",(download,)).fetchone()
    from src.anime.job_contracts import parse_payload
    operation=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',(claim.job_id,)).fetchone()
    preparation_operation=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',(preparation,)).fetchone()
    if preparation_operation!=operation:raise TransferConflict()
    if (row is None or row[:3]!=(claim.event_id,'DOWNLOAD_TORRENT','DONE')
            or parse_payload(row[3])!={'preparation_job_id':preparation} or operation!=(row[4],)):
        raise TransferConflict()
    intent=_current(db,claim,preparation);record=_record(db,intent)
    if record is None or record.phase!='COMPLETE' or record.progress!=1:raise TransferConflict()
    return intent,record


def load(database,claim,preparation,download):
    queue=JobQueue(database)
    with queue.connection() as db,transaction(db):
        queue._owns(db,claim,queue.now())
        result=current(db,claim,preparation,download)
        queue._owns(db,claim,queue.now())
        return result


def persist(db,claim,value):
    if not db.in_transaction or type(value) is not VerifiedMedia:raise MediaInvalid()
    intent,record=current(db,claim,value.preparation_job_id,value.download_job_id)
    selected=intent.plan.manifest.files[intent.plan.manifest.selected_index]
    if (intent.plan.plan_id!=value.plan_id or intent.fingerprint!=value.submission_fingerprint
            or record.fingerprint!=value.transfer_fingerprint or selected.path!=value.relative_path
            or selected.size!=value.file_signature[2]):raise MediaInvalid()
    expected=(claim.job_id,value.download_job_id,value.submission_fingerprint,value.transfer_fingerprint,value.json())
    prior=db.execute('SELECT job_id,download_job_id,submission_fingerprint,transfer_fingerprint,evidence_json '
                     'FROM media_verifications WHERE plan_id=?',(value.plan_id,)).fetchone()
    if prior is not None:
        if prior!=expected:raise MediaInvalid()
        return
    from time import time_ns
    db.execute('INSERT INTO media_verifications(plan_id,job_id,download_job_id,submission_fingerprint,'
        'transfer_fingerprint,evidence_json,verified_at_ms) VALUES(?,?,?,?,?,?,?)',
        (value.plan_id,*expected,time_ns()//1000000))
