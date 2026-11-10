"""Immutable per-file audio and content evidence from completed batch unlock."""
import json
import time

from src.anime.acquisition.media import VerifiedMedia, MediaInvalid


def persist_batch_episode_media(db,claim,file_index,media):
    if (not db.in_transaction or claim.kind!='VERIFY_BATCH_EPISODE'
            or type(media) is not VerifiedMedia or type(file_index) is not int
            or file_index<0):
        raise MediaInvalid()
    now=time.time_ns()//1000000
    row=db.execute(
        'SELECT state,attempt_count,lease_token,lease_expires_at_ms,'
        'created_by_job_id,event_id,payload_json FROM jobs WHERE job_id=?',
        (claim.job_id,)).fetchone()
    if (row is None or row[:3]!=('RUNNING',claim.attempt,claim.token)
            or row[3] is None or row[3]<=now or row[5]!=claim.event_id):
        raise MediaInvalid()
    try:
        payload=json.loads(row[6])
        if payload!={'unlock_job_id':row[4],'file_index':file_index}:
            raise MediaInvalid()
    except (TypeError,ValueError):
        raise MediaInvalid() from None
    unlock=db.execute(
        "SELECT u.state,u.kind,u.event_id,t.plan_id,t.phase,t.fingerprint,"
        "s.reservation_job_id,s.fingerprint,s.state "
        "FROM jobs u JOIN batch_torrent_unlocks t ON t.unlock_job_id=u.job_id "
        "JOIN batch_torrent_submissions s USING(plan_id) "
        "JOIN job_operation_members um ON um.job_id=u.job_id "
        "JOIN job_operation_members vm ON vm.job_id=? AND vm.operation_id=um.operation_id "
        "JOIN job_operations op ON op.operation_id=um.operation_id AND op.state='ACTIVE' "
        "WHERE u.job_id=?",(claim.job_id,row[4])).fetchone()
    if (unlock is None or unlock[:3]!=('DONE','UNLOCK_BATCH',claim.event_id)
            or unlock[4]!='COMPLETE'
            or (media.plan_id,media.download_job_id,media.preparation_job_id,
                media.transfer_fingerprint,media.submission_fingerprint)!=(
                unlock[3],row[4],unlock[6],unlock[5],unlock[7])
            or unlock[8]!='OWNED_STOPPED'):
        raise MediaInvalid()
    file=db.execute(
        "SELECT f.file_path,f.file_size_bytes,r.owner_job_id "
        "FROM batch_user_evaluation_files t JOIN batch_verified_files f "
        "ON f.file_index=t.file_index "
        "JOIN batch_user_evaluation_plans p ON p.plan_id=t.plan_id AND f.batch_id=p.batch_id "
        "JOIN batch_episode_reservations r ON r.plan_id=t.plan_id AND r.file_index=t.file_index "
        "WHERE t.plan_id=? AND t.file_index=? AND r.state='ACTIVE'",
        (media.plan_id,file_index)).fetchone()
    if (file is None or file!=(media.relative_path,media.file_signature[2],
                              media.preparation_job_id)):
        raise MediaInvalid()
    if media.probe_info is not None:
        db.execute('INSERT OR IGNORE INTO media_probe_details VALUES(?,?)',(claim.job_id,json.dumps(media.probe_info)))
    evidence=media.json()
    if len(evidence.encode())>65536:raise MediaInvalid()
    old=db.execute(
        'SELECT verification_job_id,unlock_job_id,submission_fingerprint,'
        'transfer_fingerprint,evidence_json FROM batch_episode_media '
        'WHERE plan_id=? AND file_index=?',(media.plan_id,file_index)).fetchone()
    expected=(claim.job_id,media.download_job_id,media.submission_fingerprint,
              media.transfer_fingerprint,evidence)
    if old is not None:
        if old!=expected:raise MediaInvalid()
        return
    db.execute(
        'INSERT INTO batch_episode_media(plan_id,file_index,verification_job_id,'
        'unlock_job_id,submission_fingerprint,transfer_fingerprint,evidence_json,'
        'verified_at_ms) VALUES(?,?,?,?,?,?,?,?)',
        (media.plan_id,file_index,*expected,now))
