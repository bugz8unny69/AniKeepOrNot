"""Lease-fenced transcode attempts and output; original verification stays immutable."""
import json
import os
from pathlib import Path

from src.anime.acquisition.cleanup import directory, parent
from src.anime.acquisition.media import signature, media_fingerprint, cancelled
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import transaction
from src.anime.staging import StagingReview
from src.anime.services.transcode import transcode
from src.transcode_agent import encode


def apply(db, verification, intent, evidence):
    """Use a completed output, preserving the torrent intent for cleanup checks."""
    row = db.execute('SELECT output_root,evidence_json FROM transcode_media WHERE verification_job_id=?', (verification,)).fetchone()
    if row is None or row[1] is None: return intent.options.download_root, evidence
    return row[0], json.loads(row[1])


def prepare(context, verification, secrets, *, batch=False, client=transcode):
    """Idempotent step also used by Retry on already-existing staging jobs."""
    config, claim, flag = context.configuration, context.claim, context.cancelled
    if config.value('transcode.mode') == 'off': return
    queue = JobQueue(config.value('storage.database_path'))
    def update(sql, values):
        with queue.connection() as db, transaction(db):
            queue._owns(db, claim, queue.now())
            db.execute(sql, values)
    with queue.connection() as db, transaction(db):
        queue._owns(db, claim, queue.now())
        if batch:
            from src.anime.storage.batch_staging import facts
            _, _, intent, evidence, _ = facts(db, claim, verification)
        else:
            from src.anime.storage.staging import facts
            intent, evidence, _ = facts(db, claim, verification)
        row = db.execute('SELECT attempts,evidence_json,retry_history_id FROM transcode_media WHERE verification_job_id=?', (verification,)).fetchone()
        if row and row[1] is not None: return
        retry = db.execute("SELECT coalesce(max(history_id),0) FROM job_history WHERE job_id=? AND event_code='RETRY_REQUESTED'", (claim.job_id,)).fetchone()[0]
        attempts = row[0] if row and row[2] == retry else 0
        db.execute('INSERT INTO transcode_media(verification_job_id,job_id,attempts,retry_history_id) VALUES(?,?,?,?) '
                   'ON CONFLICT(verification_job_id) DO UPDATE SET job_id=excluded.job_id,attempts=excluded.attempts,retry_history_id=excluded.retry_history_id',
                   (verification, claim.job_id, attempts, retry))
        probe = db.execute('SELECT probe_json FROM media_probe_details WHERE verification_job_id=?', (verification,)).fetchone()
    source_root = intent.options.download_root
    # Reuse the existing safe, no-symlink descriptor path validation.
    with directory(source_root) as root:
        with parent(root, evidence['path']) as (folder, name):
            fd = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=folder)
            try:
                before = signature(os.fstat(fd))
                if before[2:4] != tuple(evidence['file'][2:4]): raise StagingReview()
                source = Path(f'/proc/{os.getpid()}/fd/{fd}')
                info = json.loads(probe[0]) if probe else None
                if info is not None:
                    info = {'duration': float(info['format']['duration']), 'streams': info['streams']}
                # Child processes can open the parent's pinned descriptor path.
                output_root = config.value('storage.user_evaluation_root')
                if not output_root: raise StagingReview('Configure the staging folder before transcoding')
                work = Path(output_root)/'.transcode'
                if work.is_symlink(): raise StagingReview()
                work.mkdir(exist_ok=True, mode=0o700)
                output = work/f'{verification}.mkv'
                def report(step):
                    cancelled(flag)
                    update('UPDATE transcode_media SET step=? WHERE verification_job_id=?', (step[:500], verification))
                def started():
                    cancelled(flag)
                    update('UPDATE transcode_media SET attempts=attempts+1 WHERE verification_job_id=?', (verification,))
                if not client(source, output, config, secrets, flag, info=info, report=report, started=started, attempts=attempts):
                    return
                if signature(os.fstat(fd)) != before: raise StagingReview('Source changed during transcode')
            finally: os.close(fd)
    with output.open('rb') as result:
        stat = signature(os.fstat(result.fileno()))
        fingerprint = media_fingerprint(result.fileno(), stat[2], stat)
    copied = dict(evidence, path='.transcode/'+output.name, file=list(stat), sha256=fingerprint)
    with directory(output_root) as root:
        stat_root = os.fstat(root)
        copied['root'] = [stat_root.st_dev, stat_root.st_ino]
    update('UPDATE transcode_media SET output_root=?,evidence_json=?,step=? WHERE verification_job_id=?',
           (output_root, json.dumps(copied, sort_keys=True, separators=(',', ':')), 'Transcode finished', verification))


def cleanup_outputs(database):
    """Discard only signature-matching intermediate outputs after publication."""
    queue = JobQueue(database)
    with queue.connection() as db:
        rows = db.execute("SELECT t.output_root,t.evidence_json FROM transcode_media t WHERE t.evidence_json IS NOT NULL AND ("
                          "EXISTS(SELECT 1 FROM staging_publications s WHERE s.verification_job_id=t.verification_job_id AND s.state='PUBLISHED') OR "
                          "EXISTS(SELECT 1 FROM batch_staging_publications s WHERE s.verification_job_id=t.verification_job_id AND s.state='PUBLISHED'))").fetchall()
    for root, raw in rows:
        evidence = json.loads(raw)
        try:
            with directory(root) as base, parent(base,evidence['path']) as (folder,name):
                current = signature(os.stat(name,dir_fd=folder,follow_symlinks=False))
                if list(current) == evidence['file']: os.unlink(name,dir_fd=folder)
        except FileNotFoundError: pass
