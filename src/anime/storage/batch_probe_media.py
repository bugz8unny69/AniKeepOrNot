"""Durable real JP+EN probe proof, fenced to a completed batch download job."""
import time

from src.anime.acquisition.media import VerifiedMedia, MediaInvalid
from src.anime.history import integer


def _proof_source(db,claim,verified):
    row=db.execute(
        "SELECT download.event_id,download.kind,download.state,"
        "download.created_by_job_id,p.batch_id,b.reservation_job_id,b.state,"
        "m.operation_id,b.plan_id "
        "FROM jobs download "
        "JOIN batch_torrent_submissions b ON b.first_job_id=download.created_by_job_id "
        "JOIN batch_user_evaluation_plans p ON p.plan_id=b.plan_id "
        "JOIN job_operation_members m ON m.job_id=download.job_id "
        "JOIN job_operation_members vm ON vm.job_id=? AND vm.operation_id=m.operation_id "
        "WHERE download.job_id=?",
        (claim.job_id,verified.download_job_id)).fetchone()
    if (row is None or row[:3]!=(claim.event_id,'TRANSFER_BATCH','DONE')
            or row[5]!=verified.preparation_job_id or row[6]!='OWNED_STOPPED'
            or row[8]!=verified.plan_id or verified.plan_id<=0):
        raise MediaInvalid()
    op=db.execute('SELECT state FROM job_operations WHERE operation_id=?',
                  (row[7],)).fetchone()
    if op!=('ACTIVE',):raise MediaInvalid()
    if db.execute(
        'SELECT operation_id FROM job_operation_members WHERE job_id=?',
        (row[5],)).fetchone()!=(row[7],):
        raise MediaInvalid()
    candidate_file=db.execute(
        'SELECT f.file_index,f.file_path,f.file_size_bytes '
        'FROM batch_verified_files f '
        'JOIN batch_user_evaluation_plans p ON p.batch_id=f.batch_id '
        'JOIN batch_user_evaluation_files t ON t.plan_id=p.plan_id AND t.file_index=f.file_index '
        'WHERE p.plan_id=? AND t.tvdb_episode=p.anchor_episode',
        (verified.plan_id,)).fetchone()
    if (candidate_file is None or candidate_file[1]!=verified.relative_path
            or candidate_file[2]!=verified.file_signature[2]):
        raise MediaInvalid()
    transfer=db.execute(
        'SELECT phase,selected_index,piece_size,fingerprint,selected_progress '
        'FROM batch_torrent_transfers WHERE plan_id=?',(verified.plan_id,)).fetchone()
    if (transfer is None or transfer[0]!='COMPLETE'
            or transfer[1]!=candidate_file[0] or transfer[3]!=verified.transfer_fingerprint
            or transfer[4]!=1):
        raise MediaInvalid()
    submission=db.execute(
        'SELECT fingerprint,info_hash,info_sha256 FROM batch_torrent_submissions '
        'WHERE plan_id=?',(verified.plan_id,)).fetchone()
    if submission is None or submission[0]!=verified.submission_fingerprint:
        raise MediaInvalid()
    parent=db.execute(
        "SELECT created_by_job_id,event_id,kind FROM jobs WHERE job_id=?",
        (claim.job_id,)).fetchone()
    if parent!=(verified.download_job_id,claim.event_id,'VERIFY_BATCH_PROBE'):
        raise MediaInvalid()
    return candidate_file


def persist_batch_probe_media(db,claim,verified):
    """Inside the existing fenced VERIFY_BATCH_PROBE completion transaction."""
    if not db.in_transaction or type(verified) is not VerifiedMedia:
        raise MediaInvalid()
    integer(claim.job_id,positive=True)
    now=time.time_ns()//1000000
    lease=db.execute(
        'SELECT kind,state,attempt_count,lease_token,lease_expires_at_ms '
        'FROM jobs WHERE job_id=?',(claim.job_id,)).fetchone()
    if (lease is None or lease[:3]!=('VERIFY_BATCH_PROBE','RUNNING',claim.attempt)
            or lease[3]!=claim.token or lease[4] is None or lease[4]<=now):
        raise MediaInvalid()
    _proof_source(db,claim,verified)
    encoded=verified.json()
    if len(encoded.encode('utf-8'))>65536:raise MediaInvalid()
    previous=db.execute(
        'SELECT verification_job_id,transfer_job_id,submission_fingerprint,'
        'transfer_fingerprint,evidence_json FROM batch_probe_media WHERE plan_id=?',
        (verified.plan_id,)).fetchone()
    expected=(claim.job_id,verified.download_job_id,
              verified.submission_fingerprint,verified.transfer_fingerprint,encoded)
    if previous is not None:
        if previous!=expected:raise MediaInvalid()
        return
    db.execute(
        'INSERT INTO batch_probe_media(plan_id,verification_job_id,transfer_job_id,'
        'submission_fingerprint,transfer_fingerprint,evidence_json,verified_at_ms) '
        'VALUES(?,?,?,?,?,?,?)',(verified.plan_id,*expected,now))
