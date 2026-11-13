"""One durable staged-file batch, processed by the worker with the shared client.

The batch row holds a small JSON cursor/rename receipt for each file. There are
no per-file jobs or history rows. A rename receipt is saved before publishing;
a restart can finish the catalog update without encoding the same file twice.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time

from src.anime.acquisition.cleanup import directory, parent
from src.anime.acquisition.media import signature, media_fingerprint, cancelled
from src.anime.maintenance import MaintenanceGate, MaintenanceBusy
from src.anime.services.transcode import transcode, target_bytes, TranscodeWaiting, TranscodeFailed, log
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import transaction
from src.transcode_agent import encode

COLUMNS = 'batch_id,anidb_id,state,total,done,skipped,failed,failed_files,current_file,created_at_ms,finished_at_ms'


def status(db):
    row = db.execute('SELECT '+COLUMNS+' FROM transcode_batches ORDER BY batch_id DESC LIMIT 1').fetchone()
    if row is None: return None
    value = dict(zip(COLUMNS.split(','), row))
    value['failed_files'] = json.loads(value['failed_files'])
    return value


def staged_files(db, anidb_id=None):
    rows = db.execute(
        "SELECT 'staging_publications',p.job_id,r.anidb_id,p.manifest_json,p.receipts_json "
        "FROM staging_publications p JOIN acquisition_plans a USING(plan_id) "
        "JOIN episode_reservations r USING(reservation_id) WHERE p.state='PUBLISHED' "
        "UNION ALL SELECT 'batch_staging_publications',p.job_id,r.anidb_id,p.manifest_json,p.receipts_json "
        "FROM batch_staging_publications p JOIN batch_episode_reservations r "
        "ON r.plan_id=p.plan_id AND r.file_index=p.file_index WHERE p.state='PUBLISHED'").fetchall()
    files, seen = [], set()
    for table, job, aid, manifest, receipts in rows:
        if anidb_id is not None and aid != anidb_id: continue
        from src.anime.storage.review_decisions import decision
        if decision(db,aid) is not None: continue
        plan = json.loads(manifest)
        receipt = next(r for r in json.loads(receipts) if r['role'] == 'media')
        path = str(Path(plan['root'])/receipt['path'])
        if path in seen: continue
        resource = db.execute("SELECT state FROM resources WHERE kind='STAGED_FILE' AND path=? ORDER BY resource_id DESC LIMIT 1", (path,)).fetchone()
        if resource and resource != ('PRESENT',): continue
        seen.add(path)
        files.append({'table': table, 'job_id': job, 'anidb_id': aid, 'root': plan['root'],
                      'relative': receipt['path'], 'file': receipt['file'], 'duration': plan['source']['duration_ms']/1000,
                      'state': 'pending', 'attempts': 0})
    return files


def dry_run(db, configuration, anidb_id=None):
    result = []
    for item in staged_files(db, anidb_id):
        path = Path(item['root'])/item['relative']
        try: size = path.stat().st_size
        except FileNotFoundError: continue
        target = target_bytes(item['duration'], configuration.value('transcode.target_mb'))
        if size > target: result.append({'path': str(path), 'size': size, 'target': target, 'saved': size-target})
    return result


def enqueue(db, *, anidb_id=None):
    if not db.in_transaction: raise ValueError('Transaction required')
    old = status(db)
    if old and old['state'] in ('QUEUED', 'RUNNING'):
        raise TranscodeWaiting(f"Batch {old['batch_id']} is already running: {old['done']+old['skipped']+old['failed']}/{old['total']}")
    files = staged_files(db, anidb_id)
    return db.execute("INSERT INTO transcode_batches(anidb_id,state,total,created_at_ms,progress_json) VALUES(?,'QUEUED',?,?,?)",
                      (anidb_id, len(files), time.time_ns()//1000000, json.dumps(files))).lastrowid


def _save(db, batch, files):
    done = sum(f['state'] == 'done' for f in files)
    skipped = sum(f['state'] == 'skipped' for f in files)
    failed = [f for f in files if f['state'] == 'failed']
    current = ', '.join(Path(f['relative']).name for f in files if f['state'] in ('encoding', 'publishing'))[:1000]
    db.execute('UPDATE transcode_batches SET progress_json=?,done=?,skipped=?,failed=?,failed_files=?,current_file=? WHERE batch_id=?',
               (json.dumps(files), done, skipped, len(failed), json.dumps([{'file': f['relative'], 'error': f.get('error','')} for f in failed]), current or None, batch))


def _edit(queue, batch, index, **changes):
    with queue.connection() as db, transaction(db):
        files = json.loads(db.execute('SELECT progress_json FROM transcode_batches WHERE batch_id=?', (batch,)).fetchone()[0])
        files[index].update(changes)
        _save(db, batch, files)
        return files[index]


def _catalog(db, item, result):
    # Table names come only from staged_files(), never command/API input.
    if item['table'] not in ('staging_publications', 'batch_staging_publications'): raise ValueError('Invalid publication')
    row = db.execute('SELECT manifest_json,receipts_json FROM '+item['table']+" WHERE job_id=? AND state='PUBLISHED'", (item['job_id'],)).fetchone()
    if row is None: raise TranscodeFailed('Staging record no longer exists')
    plan, receipts = json.loads(row[0]), json.loads(row[1])
    entry = next(e for e in plan['entries'] if e['role'] == 'media')
    receipt = next(e for e in receipts if e['role'] == 'media')
    if receipt['path'] != item['relative']: raise TranscodeFailed('Staging record changed')
    entry.update(path=result['relative'], size=result['file'][2], sha256=result['sha256'])
    receipt.update(path=result['relative'], file=result['file'], sha256=result['sha256'])
    db.execute('UPDATE '+item['table']+' SET manifest_json=?,receipts_json=? WHERE job_id=?',
               (json.dumps(plan, sort_keys=True, separators=(',', ':')), json.dumps(receipts, sort_keys=True, separators=(',', ':')), item['job_id']))
    if receipt['artifact'] is not None:
        db.execute('UPDATE staging_cleanup_artifacts SET file_device=?,file_inode=? WHERE artifact_id=?',
                   (*result['file'][:2], receipt['artifact']))
    old, new = str(Path(item['root'])/item['relative']), str(Path(item['root'])/result['relative'])
    from src.anime.resources import Resources
    Resources(db,item['job_id'],time.time_ns()//1000000).replace_staged(old,new,result['file'][2])


def _guard(db, item):
    # Keep/Reject can happen while an encode is running. Never replace a file after
    # a decision has started using it. Their write transactions serialize with ours.
    from src.anime.storage.review_decisions import decision
    if decision(db,item['anidb_id']) is not None:
        raise TranscodeFailed('A Keep or Reject decision has started for this series')
    operation = db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?', (item['job_id'],)).fetchone()
    if operation:
        if db.execute("SELECT 1 FROM management_actions a JOIN jobs j ON j.job_id=a.job_id "
                      "WHERE a.operation_id=? AND a.action='KEEP' AND j.state NOT IN ('FAILED','CANCELED') LIMIT 1", operation).fetchone():
            raise TranscodeFailed('A Keep or Reject action has started for this file')
        state = db.execute('SELECT state FROM job_operations WHERE operation_id=?', operation).fetchone()
        if state and state[0] in ('CANCELED', 'CANCEL_REQUESTED'): raise TranscodeFailed('Staging operation was canceled')


def _one(queue, batch, index, item, config, secrets, stop, *, client=transcode):
    root, relative = item['root'], item['relative']
    source = Path(root)/relative
    # The .tmp name is deterministic for this batch so a crash leaves a known
    # file, never a half-written staged video. A durable publishing receipt wins.
    temporary = source.with_name(source.name+f'.transcode-{batch}.tmp')
    target = source.with_suffix('.mkv')
    try:
        with queue.connection() as db: _guard(db,item)
        with directory(root) as base, parent(base, relative) as (folder, name):
            if item.get('result'):
                _publish(queue, batch, index, item, temporary, target, folder, stop)
                return
            temporary.unlink(missing_ok=True)
            before = signature(os.stat(name, dir_fd=folder, follow_symlinks=False))
            if before[2:4] != tuple(item['file'][2:4]): raise TranscodeFailed('Staged source changed')
            if before[2] <= target_bytes(item['duration'], config.value('transcode.target_mb')):
                _edit(queue, batch, index, state='skipped')
                return
            cancelled(stop)
            _edit(queue, batch, index, state='encoding')
            def started():
                nonlocal item
                item = _edit(queue, batch, index, attempts=item['attempts']+1)
            result = client(source, temporary, config, secrets, stop, batch=True, started=started, attempts=item['attempts'])
            if not result:
                _edit(queue, batch, index, state='skipped')
                return
            with temporary.open('rb') as stream:
                file = signature(os.fstat(stream.fileno()))
                fingerprint = media_fingerprint(stream.fileno(), file[2], file)
                os.fsync(stream.fileno())
            output = {'relative': str(Path(relative).with_suffix('.mkv')), 'file': list(file), 'sha256': fingerprint}
            item = _edit(queue, batch, index, state='publishing', result=output)
            _publish(queue, batch, index, item, temporary, target, folder, stop)
    except TranscodeWaiting:
        log.info('batch %s: %s: back in the queue (no agent took it)', batch, source.name)
        _edit(queue, batch, index, state='pending')
    except (OSError, ValueError, encode.EncodeError, TranscodeFailed) as exc:
        cancelled(stop)
        log.warning('batch %s: %s: failed: %s', batch, source.name, str(exc)[-400:])
        _edit(queue, batch, index, state='failed', error=str(exc)[-400:])
        item.pop('result',None)
    finally:
        # Retain a complete publishing receipt's file for crash reconciliation.
        if not item.get('result'):
            temporary.unlink(missing_ok=True)
            Path(str(temporary)+'.tmp').unlink(missing_ok=True)


def _publish(queue, batch, index, item, temporary, target, folder, stop):
    cancelled(stop)
    output = item['result']
    source = Path(item['root'])/item['relative']
    with queue.connection() as db, transaction(db):
        _guard(db, item)
        if temporary.exists():
            if list(signature(temporary.stat())) != output['file']: raise TranscodeFailed('Temporary result changed')
            if list(signature(source.stat()))[2:4] != item['file'][2:4]: raise TranscodeFailed('Staged source changed')
            if target != source and target.exists(): raise TranscodeFailed('MKV destination already exists')
            os.replace(temporary, target)
            os.fsync(folder)
        elif not target.exists() or list(signature(target.stat())) != output['file']:
            # ctime changes on rename on some filesystems; compare stable fields below.
            if not target.exists() or list(signature(target.stat()))[:4] != output['file'][:4]:
                raise TranscodeFailed('Published result is missing or changed')
        output['file'] = list(signature(target.stat()))
        _catalog(db, item, output)
        if target != source:
            if source.exists() and list(signature(source.stat()))[2:4] == item['file'][2:4]: source.unlink()
        files = json.loads(db.execute('SELECT progress_json FROM transcode_batches WHERE batch_id=?', (batch,)).fetchone()[0])
        files[index].update(state='done', result=output)
        _save(db, batch, files)


def run_batch(configuration, secrets, stop, *, client=transcode):
    """One background tick. Process every free agent without consuming job slots."""
    if configuration.value('transcode.mode') == 'off' or stop.is_set(): return False
    queue = JobQueue(configuration.value('storage.database_path'))
    gate = MaintenanceGate(queue.database)
    try:
        with gate.operation(), gate._directory() as directory_fd, gate._lock(directory_fd, 'transcode-batch', fcntl.LOCK_EX):
            with queue.connection() as db, transaction(db):
                batch = status(db)
                if not batch or batch['state'] == 'DONE': return False
                # Give newly queued pipeline steps their opportunity to reserve a slot first.
                if db.execute("SELECT 1 FROM jobs WHERE kind IN ('TRANSCODE_MEDIA','STAGE_BATCH_EPISODE') AND state='PENDING' LIMIT 1").fetchone():
                    log.info('batch %s: paused while new intake waits for an agent', batch['batch_id'])
                    return False
                number = batch['batch_id']
                files = json.loads(db.execute('SELECT progress_json FROM transcode_batches WHERE batch_id=?', (number,)).fetchone()[0])
                db.execute("UPDATE transcode_batches SET state='RUNNING' WHERE batch_id=?", (number,))
            count = 1 if configuration.value('transcode.mode') == 'local' else max(1, len(configuration.value('transcode.agents')))
            remaining = [(i, f) for i, f in enumerate(files) if f['state'] not in ('done', 'skipped', 'failed')]
            round_files = remaining[:count]
            log.info('batch %s: round of %d file(s) for %d destination(s), %d left: %s', number, len(round_files), count,
                     len(remaining), ', '.join(Path(f['relative']).name for _, f in round_files))
            began = time.monotonic()
            with ThreadPoolExecutor(max_workers=count, thread_name_prefix='transcode-staged') as pool:
                tasks = [pool.submit(_one, queue, number, i, f, configuration, secrets, stop, client=client) for i, f in round_files]
                for task in tasks: task.result()
            log.info('batch %s: round finished in %.0fs', number, time.monotonic() - began)
            with queue.connection() as db, transaction(db):
                batch = status(db)
                if batch['done'] + batch['skipped'] + batch['failed'] == batch['total']:
                    from src.anime.notifications import notify
                    failed_names = ', '.join(Path(f['file']).name for f in batch['failed_files'])
                    text = f"Transcode finished: {batch['done']} done, {batch['failed']} failed"
                    if failed_names: text += ': ' + failed_names
                    notify(db, {'type': 'TRANSCODE_BATCH_DONE', 'title': 'Staged transcode finished',
                                'message': text[:500], 'anidb_id': batch['anidb_id'],
                                'group_key': f'transcode-batch:{number}'})
                    db.execute("UPDATE transcode_batches SET state='DONE',current_file=NULL,finished_at_ms=? WHERE batch_id=?", (time.time_ns()//1000000, number))
            return True
    except MaintenanceBusy: return False
