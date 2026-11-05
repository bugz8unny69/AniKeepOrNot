"""Read-only diagnostics for `adm report job`: why a job is where it is.

Everything here is a SELECT against persisted state plus the documented
resume paths in the code (named below). Nothing is written, retried, released
or called. When the data cannot answer a question the result says so with
NOT_RECORDED / UNKNOWN instead of guessing.

Documented resume paths this module reasons about:
- storage/episode_reservations.skip_staged_waiters: when an owner's single-episode
  acquisition publishes to staging, held exact duplicates are closed DONE /
  SKIPPED_DUPLICATE.
- storage/reservation_release.release_cancelled -> wake_waiters: when an owner's
  operation is CANCELED with cleanup COMPLETE, its single-episode reservations
  are released and held duplicates whose own operation is ACTIVE go to PENDING.
- storage/job_queue.recover_expired: expired RUNNING leases are retried by the worker.
- storage/review_retry: Retry covers NEEDS_REVIEW (and a few BLOCKED search reasons).
Nothing releases a reservation on time, restart or owner completion.
"""
import json
import time

NOT_RECORDED = 'Not recorded'
NOT_CHECKED = 'Not checked'
UNKNOWN = 'Unknown'
LIVE = ('PENDING', 'RUNNING', 'RETRY_WAIT')
STOPPED = ('NEEDS_REVIEW', 'BLOCKED', 'ON_HOLD')
FINISHED = ('DONE', 'CANCELED', 'FAILED')
MAX_HOPS = 6          # Dependency chain bound.
MAX_MATCHES = 20      # Reservation rows shown per namespace.
DEFAULT_MAX_RETRIES = 3  # job_contracts.Policy default; the running worker's value is not persisted.


def when(ms):
    from datetime import datetime
    return NOT_RECORDED if ms is None else datetime.fromtimestamp(ms / 1000).strftime('%Y-%m-%d %H:%M:%S')


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _one(db, sql, args=()):
    cur = db.execute(sql, args)
    row = cur.fetchone()
    return None if row is None else dict(zip([c[0] for c in cur.description], row))


def _all(db, sql, args=()):
    cur = db.execute(sql, args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _job(db, job_id):
    return _one(db, 'SELECT job_id,kind,state,last_reason_code,activity_code,attempt_count,created_by_job_id,'
                    'available_at_ms,lease_expires_at_ms,updated_at_ms,event_id,payload_json FROM jobs WHERE job_id=?',
                (job_id,))


def _operation(db, job_id):
    return _one(db, 'SELECT o.operation_id,o.state,o.cleanup_state,o.reason_code FROM job_operations o '
                    'JOIN job_operation_members m USING(operation_id) WHERE m.job_id=?', (job_id,))


def _members(db, operation_id):
    return _all(db, 'SELECT j.job_id,j.kind,j.state,j.last_reason_code,j.activity_code,j.created_by_job_id,'
                    'j.updated_at_ms FROM jobs j JOIN job_operation_members m USING(job_id) '
                    'WHERE m.operation_id=? ORDER BY j.job_id', (operation_id,))


def _label(job):
    if job is None: return 'missing job'
    text = f'Job #{job["job_id"]} {job["kind"]} {job["state"]}'
    return text + (f' ({job["last_reason_code"]})' if job.get('last_reason_code') else '')


def _status(job):
    return 'missing job' if job is None else _label(job).split(' ', 2)[2]


def _payload(job):
    try: return json.loads(job['payload_json'] or '{}')
    except (TypeError, ValueError): return {}


# ---------------------------------------------------------------- reservations

def _episode(db, job, have):
    """Episode identity this RESERVE_EPISODE job matches on, from its evaluation."""
    evaluation = _payload(job).get('evaluation_job_id')
    if type(evaluation) is not int or not {'trial_evaluations', 'release_verifications'} <= have:
        return None
    return _one(db, 'SELECT t.selected,r.anidb_id,r.anidb_episode_id,r.tvdb_series_id,r.tvdb_season,'
                    'r.tvdb_episode,r.tvdb_episode_id FROM trial_evaluations t '
                    'JOIN release_verifications r ON r.verification_id=t.verification_id WHERE t.job_id=?',
                (evaluation,))


def _matches(db, table, job_id, ep):
    rows = _all(db, f'SELECT * FROM {table} WHERE owner_job_id<>? AND (anidb_episode_id=? OR tvdb_episode_id=? '
                    'OR (tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=?)) '
                    f'ORDER BY state=\'ACTIVE\' DESC,reservation_id DESC LIMIT {MAX_MATCHES}',
                (job_id, ep['anidb_episode_id'], ep['tvdb_episode_id'],
                 ep['tvdb_series_id'], ep['tvdb_season'], ep['tvdb_episode']))
    for r in rows:
        r['matched_on'] = [name for name, hit in (
            ('anidb_episode_id', r['anidb_episode_id'] == ep['anidb_episode_id']),
            ('tvdb_episode_id', r['tvdb_episode_id'] == ep['tvdb_episode_id']),
            ('placement', (r['tvdb_series_id'], r['tvdb_season'], r['tvdb_episode']) ==
             (ep['tvdb_series_id'], ep['tvdb_season'], ep['tvdb_episode']))) if hit]
    return rows


def _published(db, reservation_id, have):
    if not {'acquisition_plans', 'staging_publications'} <= have: return None
    return _one(db, "SELECT s.job_id,s.published_at_ms FROM acquisition_plans p JOIN staging_publications s "
                    "ON s.plan_id=p.plan_id WHERE p.reservation_id=? AND s.state='PUBLISHED'", (reservation_id,))


def _assess(db, res, batch, have):
    """Validity of one ACTIVE reservation by the lifecycle rules, never by 'owner not running'."""
    owner = _job(db, res['owner_job_id'])
    op = _operation(db, res['owner_job_id']) if owner else None
    out = {'owner_job': owner, 'owner_operation': op}
    no_wake = (' Held single-episode jobs are not woken when a batch reservation is released, '
               'and batch staging does not close them.') if batch else ''
    if owner is None:
        return {**out, 'validity': 'UNCERTAIN', 'detail': f'Owner Job #{res["owner_job_id"]} not found.',
                'resolves_when': UNKNOWN, 'automatic': 'No known mechanism.',
                'manual': 'Yes — the reservation references a missing job; investigate.'}
    if op is None:
        return {**out, 'validity': 'UNCERTAIN', 'detail': 'Owner job belongs to no operation; '
                'release only happens through operation cancellation.', 'resolves_when': UNKNOWN,
                'automatic': 'No known mechanism.', 'manual': 'Yes — investigate the owner.'}
    published = None if batch else _published(db, res['reservation_id'], have)
    if published:
        return {**out, 'validity': 'VALID', 'detail': f'Owner staged this episode (Job #{published["job_id"]}, '
                f'published {when(published["published_at_ms"])}).',
                'resolves_when': 'Held duplicates are closed as SKIPPED_DUPLICATE when the owner publishes; '
                'that already happened without closing this job.',
                'automatic': 'No — the publishing event has passed; nothing later closes this job.',
                'manual': 'Yes — this job is a duplicate of staged work; cancel it.'}
    if op['state'] in ('CANCEL_REQUESTED', 'CANCELED'):
        if op['cleanup_state'] == 'COMPLETE':
            return {**out, 'validity': 'UNCERTAIN', 'detail': f'Owner Operation #{op["operation_id"]} cancellation '
                    'completed but this reservation was not released (potentially orphaned).',
                    'resolves_when': 'Release by the cancellation cleanup.',
                    'automatic': 'No — cleanup already completed.',
                    'manual': 'Yes — run Clean up on the owner\'s work again; it releases remaining reservations.'}
        return {**out, 'validity': 'VALID', 'detail': f'Owner Operation #{op["operation_id"]} is being canceled '
                f'(cleanup {op["cleanup_state"]}).',
                'resolves_when': 'Owner cancellation cleanup completes and releases the reservation.',
                'automatic': ('No.' + no_wake) if batch else ('Yes — completed cleanup wakes this job.'
                             if op['cleanup_state'] == 'PENDING' else 'Not while cleanup needs review.'),
                'manual': 'Yes — resolve the owner\'s cleanup review.' if op['cleanup_state'] == 'REVIEW' else 'No.'}
    members = _members(db, op['operation_id'])
    live = [m for m in members if m['state'] in LIVE]
    stuck = [m for m in members if m['state'] in ('NEEDS_REVIEW', 'BLOCKED', 'ON_HOLD')]
    resolves = ('Owner publishes this episode to staging (closes this job as a duplicate), or the owner\'s '
                'operation is canceled and its cleanup completes (wakes this job).')
    if batch:
        resolves = 'Owner batch work ends; no event then resumes this job.'
    if live:
        return {**out, 'validity': 'VALID', 'detail': 'Owner work is in progress: ' + _label(live[0]) + '.',
                'resolves_when': resolves, 'blocking_member': live[0],
                'automatic': ('No.' + no_wake) if batch else 'Yes — see "resolves when".', 'manual': 'No.'}
    if stuck:
        return {**out, 'validity': 'VALID', 'detail': 'Reservation is ACTIVE under the lifecycle rules, but the '
                'owner\'s work is stopped: ' + _label(stuck[0]) + '.', 'resolves_when': resolves,
                'blocking_member': stuck[0], 'automatic': 'Not until the owner\'s work moves again.' + no_wake,
                'manual': f'Yes — resolve Job #{stuck[0]["job_id"]} (Retry if available) or cancel owner '
                          f'Operation #{op["operation_id"]}.'}
    last = max(members, key=lambda m: (m['updated_at_ms'] or 0, m['job_id'])) if members else None
    return {**out, 'validity': 'VALID', 'detail': f'Reservation is ACTIVE under the lifecycle rules, but owner '
            f'Operation #{op["operation_id"]} ({op["state"]}) has no unfinished job and nothing was published. '
            + (f'Its last step was {_label(last)}; it created no successor.' if last and not _successors(db, last['job_id'])
               else '') + ' Suspected stranded owner.',
            'resolves_when': resolves, 'blocking_member': last,
            'automatic': 'No known mechanism.' + no_wake,
            'manual': f'Yes — cancel owner Operation #{op["operation_id"]} (completed cleanup releases the '
                      'reservation and wakes this job), or find why its pipeline stopped.'}


def _wake_checks(db, job, own_op):
    """Conditions wake_waiters applies before it resumes a held job."""
    checks = [('Own operation is ACTIVE', bool(own_op) and own_op['state'] == 'ACTIVE',
               f'operation state {own_op["state"] if own_op else "none"}')]
    try:
        from src.nextgen.storage.episode_reservations import _facts
        facts = _facts(db, _payload(job).get('evaluation_job_id'), job['event_id'])
        checks.append(('Verified episode facts re-check', True, 'consistent'))
        checks.append(('Evaluation selected the episode', facts.selected, 'selected' if facts.selected else 'outside window'))
    except Exception as exc:  # ReservationConflict or missing tables: the wake would skip this job.
        checks.append(('Verified episode facts re-check', False, exc.__class__.__name__))
    return [{'check': c, 'ok': ok, 'detail': d} for c, ok, d in checks]


def reservation(db, job, have):
    if job['kind'] != 'RESERVE_EPISODE' or 'episode_reservations' not in have: return None
    ep = _episode(db, job, have)
    own = _one(db, 'SELECT * FROM episode_reservations WHERE owner_job_id=?', (job['job_id'],))
    out = {'episode': ep, 'own': own, 'conflicts': [], 'released': [], 'limitations': []}
    held = job['state'] == 'ON_HOLD' and job['last_reason_code'] == 'EPISODE_RESERVED'
    if held:
        out['limitations'].append('The ON_HOLD event does not record which reservation blocked this job; '
                                  'the blocker below is re-derived from current reservations.')
    if ep is None:
        out['limitations'].append('Episode identity not recorded (no completed evaluation/verification found '
                                  'for the payload\'s evaluation_job_id).')
        return out
    for table, batch in (('episode_reservations', False), ('batch_episode_reservations', True)):
        if table not in have: continue
        for row in _matches(db, table, job['job_id'], ep):
            row['batch'] = batch
            if row['state'] == 'ACTIVE':
                row.update(_assess(db, row, batch, have)); out['conflicts'].append(row)
            else:
                out['released'].append(row)
    if held:
        out['wake_checks'] = _wake_checks(db, job, _operation(db, job['job_id']))
        if not out['conflicts']:
            last = out['released'][0] if out['released'] else None
            out['finding'] = ('No ACTIVE matching reservation exists now. '
                              + (f'Matching {"batch " if last["batch"] else ""}reservation #{last["reservation_id"]} '
                                 f'was released {when(last["ended_at_ms"])} by Job #{last["ended_by_job_id"]}, '
                                 'but this job was not resumed.' if last else 'No matching reservation is recorded at all.')
                              + ' See the wake checks below.')
    return out


# --------------------------------------------------------- download / staging

def _successors(db, job_id):
    return _all(db, 'SELECT job_id,kind,state,last_reason_code FROM jobs WHERE created_by_job_id=? ORDER BY job_id',
                (job_id,))


def downloads(db, members, have):
    if not members: return None
    marks = ','.join('?' * len(members))
    out = {'plans': [], 'batches': []}
    if 'acquisition_plans' in have:
        for p in _all(db, f'SELECT plan_id,job_id,reservation_id,info_hash,state,prepared_at_ms FROM acquisition_plans '
                          f'WHERE job_id IN ({marks}) OR reservation_id IN (SELECT reservation_id FROM '
                          f'episode_reservations WHERE owner_job_id IN ({marks}))', (*members, *members)):
            item = {'plan': p}
            for key, table, cols in (
                    ('submission', 'torrent_submissions', 'state,reason_code,save_path,first_job_id,updated_at_ms'),
                    ('transfer', 'torrent_transfers', 'phase,reason_code,selected_progress,start_job_id,updated_at_ms'),
                    ('verification', 'media_verifications', 'job_id,download_job_id,verified_at_ms'),
                    ('staging', 'staging_publications', 'job_id,state,manifest_json,published_at_ms,created_at_ms')):
                item[key] = _one(db, f'SELECT {cols} FROM {table} WHERE plan_id=?', (p['plan_id'],)) if table in have else None
            out['plans'].append(item)
    if 'batch_torrent_submissions' in have:
        for s in _all(db, f'SELECT plan_id,reservation_job_id,info_hash,state,reason_code,save_path,updated_at_ms '
                          f'FROM batch_torrent_submissions WHERE reservation_job_id IN ({marks}) OR first_job_id IN ({marks})',
                      (*members, *members)):
            item = {'submission': s, 'transfer': None, 'staging': []}
            if 'batch_torrent_transfers' in have:
                item['transfer'] = _one(db, 'SELECT * FROM batch_torrent_transfers WHERE plan_id=?', (s['plan_id'],))
            if 'batch_staging_publications' in have:
                item['staging'] = _all(db, 'SELECT file_index,job_id,state,published_at_ms FROM batch_staging_publications '
                                           'WHERE plan_id=? ORDER BY file_index', (s['plan_id'],))
            out['batches'].append(item)
    return out if out['plans'] or out['batches'] else None


def next_step(db, op, members):
    """What comes after the furthest finished step, from recorded successors only."""
    if op and op['state'] == 'STAGED':
        return {'status': 'COMPLETE', 'detail': 'Operation is STAGED; Keep/Reject is a separate user decision.'}
    done = [m for m in members if m['state'] == 'DONE']
    if not done: return {'status': UNKNOWN, 'detail': 'No step of this work has completed yet.'}
    created = {m['created_by_job_id'] for m in members}
    tips = [m for m in done if m['job_id'] not in created]  # DONE jobs nothing was created from.
    if not tips:
        return {'status': UNKNOWN, 'detail': 'Every completed step has a recorded successor.'}
    last = max(tips, key=lambda m: (m['updated_at_ms'] or 0, m['job_id']))
    if last.get('activity_code') == 'SKIPPED_DUPLICATE':
        return {'status': 'COMPLETE', 'after': last,
                'detail': f'{_label(last)} was closed as a duplicate of staged work; no successor is expected.'}
    return {'status': 'MISSING', 'after': last,
            'detail': f'{_label(last)} completed and created no successor job; the work does not continue '
                      'automatically from here.'}


# ------------------------------------------------------------------ recovery

def recovery(db, job, op, now):
    history = _all(db, 'SELECT history_id,event_code,reason_code,invocation_id,occurred_at_ms FROM job_history '
                       'WHERE job_id=? ORDER BY history_id', (job['job_id'],))
    started = {h['invocation_id']: h for h in history if h['event_code'] == 'HANDLER_STARTED'}
    ended = {h['invocation_id'] for h in history if h['event_code'] in ('HANDLER_COMPLETED', 'HANDLER_FAILED')}
    open_runs = [h for i, h in started.items() if i not in ended]
    if job['state'] == 'RUNNING' and open_runs: open_runs = open_runs[:-1]  # The current run is not interrupted.
    last_request = max([h['history_id'] for h in history if h['event_code'] == 'RETRY_REQUESTED'], default=0)
    failures = sum(1 for h in history if h['event_code'] == 'RETRY_SCHEDULED' and h['history_id'] > last_request)
    count = lambda code: sum(1 for h in history if h['event_code'] == code)
    lease = None
    if job['state'] == 'RUNNING':
        expired = job['lease_expires_at_ms'] is not None and job['lease_expires_at_ms'] <= now
        lease = {'expires': when(job['lease_expires_at_ms']), 'expired': expired}
    try:
        from src.nextgen.storage.review_retry import capability
        retry = 'Available' if capability(db, job['job_id'])['enabled'] else 'Not available'
    except Exception as exc:
        retry = f'{UNKNOWN} ({exc.__class__.__name__})'
    return {'lease': lease,
            'interrupted_attempts': [{'started': when(h['occurred_at_ms'])} for h in open_runs],
            'lease_expired_events': count('LEASE_EXPIRED'),
            'retries_in_window': failures, 'retry_limit': DEFAULT_MAX_RETRIES,
            'retry_requests': count('RETRY_REQUESTED'),
            'next_attempt': when(job['available_at_ms']) if job['state'] in ('PENDING', 'RETRY_WAIT') else None,
            'recovery_events': {c: count(c) for c in ('MANAGEMENT_ACTION_RESUMED', 'TORRENT_RECONCILED',
                                                      'EPISODE_RESERVATIONS_RELEASED') if count(c)},
            'cleanup_state': op['cleanup_state'] if op else NOT_RECORDED,
            'retry_action': retry}


# --------------------------------------------------------------- summary

def _last_success(db, job):
    seen, current = set(), job
    for _ in range(MAX_HOPS * 4):
        if current is None or current['job_id'] in seen: break
        seen.add(current['job_id'])
        if current['state'] == 'DONE': return current
        current = _job(db, current['created_by_job_id']) if current['created_by_job_id'] else None
    return None


def summary(db, job, op, res, rec, now):
    from src.nextgen.reasons import get_reason
    state, reason = job['state'], job['last_reason_code']
    out = {'waiting_for': None, 'condition': None, 'automatic': UNKNOWN, 'manual': UNKNOWN}
    canceling = op is not None and op['state'] in ('CANCEL_REQUESTED', 'CANCELED')
    if state == 'DONE':
        out.update(doing='Finished.' + (' Closed as a duplicate of staged work.' if job['activity_code'] ==
                   'SKIPPED_DUPLICATE' else ''), automatic='Not needed.', manual='No.')
    elif state == 'CANCELED':
        out.update(doing='Canceled.', automatic='Not applicable.', manual='No.')
    elif state == 'FAILED':
        out.update(doing=f'Candidate attempt failed ({reason}).',
                   waiting_for='safe cleanup and fallback' if canceling else 'fallback',
                   condition='Cleanup confirms absence before next candidate.',
                   automatic='Yes — automatic cleanup and candidate fallback.',
                   manual='No.')
    elif canceling:
        out.update(doing=f'Operation #{op["operation_id"]} is {op["state"]}; this job will not run normally.',
                   waiting_for=f'cancellation cleanup ({op["cleanup_state"]})',
                   condition='Cancellation cleanup completes.',
                   automatic='Yes — cancellation cancels unfinished jobs.' if op['cleanup_state'] == 'PENDING' else UNKNOWN,
                   manual='Yes — resolve the cleanup review.' if op['cleanup_state'] == 'REVIEW' else 'No.')
    elif state == 'PENDING':
        out.update(doing=f'Queued; runnable from {when(job["available_at_ms"])}.', waiting_for='a free worker',
                   condition='A worker claims it.', automatic='Yes — the worker claims runnable jobs.', manual='No.')
    elif state == 'RUNNING':
        lease = rec['lease'] or {}
        if lease.get('expired'):
            out.update(doing=f'Recorded as RUNNING but its lease expired {lease["expires"]}.',
                       waiting_for='lease recovery', condition='The worker reclaims the expired lease.',
                       automatic='Yes — the running worker retries expired leases (job_queue.recover_expired).',
                       manual='Only if no worker is running.')
        else:
            out.update(doing=f'Running; lease valid until {lease.get("expires", NOT_RECORDED)}.',
                       automatic='Not needed.', manual='No.')
    elif state == 'RETRY_WAIT':
        what = {'WAITING_FOR_DOWNLOAD': 'the torrent download to finish',
                'WAITING_FOR_SEARCH': 'the next search', 'DOWNLOADING': 'the torrent download to finish'}
        out.update(doing=f'Waiting; next check at {when(job["available_at_ms"])}.',
                   waiting_for=what.get(job['activity_code'], 'the scheduled retry time'),
                   condition='The scheduled time arrives and the check succeeds.',
                   automatic='Yes — the worker runs it again at the scheduled time.', manual='No.')
    elif state == 'ON_HOLD' and reason == 'EPISODE_RESERVED' and res is not None:
        out.update(doing='On hold: another job owns this episode\'s reservation.',
                   waiting_for='the conflicting reservation to end')
        conflicts = res['conflicts']
        if conflicts:
            c = conflicts[0]
            out.update(condition=c['resolves_when'], automatic=c['automatic'], manual=c['manual'])
        else:
            out.update(condition='No blocker exists now; only a wake by wake_waiters would resume it.',
                       automatic='No — the release that would wake it has already happened or never happens.',
                       manual='Yes — no supported action resumes an ON_HOLD job (Retry covers NEEDS_REVIEW); '
                              'cancel it if the episode is covered elsewhere.')
    elif state in ('ON_HOLD', 'NEEDS_REVIEW', 'BLOCKED'):
        known = get_reason(reason) if reason else None
        out.update(doing=f'Stopped ({state}' + (f', {reason}' if reason else '') + ').',
                   condition=known.action if known else NOT_RECORDED,
                   automatic='No automatic resume is recorded for this state.',
                   manual=('Yes — Retry is available.' if rec['retry_action'] == 'Available' else
                           f'Yes — Retry is {rec["retry_action"].lower()} for this job; see Advice.'))
        if reason == 'JOB_RETRY_EXHAUSTED':
            out['doing'] += f' Automatic retries were used up ({rec["retries_in_window"]} scheduled).'
    else:
        out['doing'] = f'{state} (no diagnostic rule for this state).'
    last = _last_success(db, job)
    out['last_success'] = _label(last) if last else NOT_RECORDED
    out['last_activity'] = when(db.execute('SELECT max(occurred_at_ms) FROM job_history WHERE job_id=?',
                                           (job['job_id'],)).fetchone()[0])
    out['suspected_stranded'] = (state not in FINISHED and not canceling and
                                 not str(out['automatic']).startswith('Yes'))
    return out


# ------------------------------------------------------------- dependencies

def dependencies(db, job, op, res):
    deps = []
    if res:
        for c in res['conflicts']:
            owner_op = c.get('owner_operation')
            deps.append({'type': 'BATCH_EPISODE_RESERVATION' if c['batch'] else 'EPISODE_RESERVATION',
                         'blocking_job': c['owner_job_id'], 'blocking_operation': owner_op and owner_op['operation_id'],
                         'blocking_status': _status(c.get('owner_job')), 'reason': 'Same episode ('
                         + ', '.join(c['matched_on']) + f') reserved as #{c["reservation_id"]}.',
                         'since': when(c['reserved_at_ms']), 'resolves_when': c['resolves_when'],
                         'valid': c['validity'], 'next': c.get('blocking_member')})
    control = _one(db, 'SELECT action,target_job_id FROM management_actions WHERE job_id=?', (job['job_id'],))
    if control:
        target = _job(db, control['target_job_id'])
        deps.append({'type': 'MANAGEMENT_TARGET', 'blocking_job': control['target_job_id'],
                     'blocking_operation': (_operation(db, control['target_job_id']) or {}).get('operation_id'),
                     'blocking_status': _status(target), 'reason': f'{control["action"]} action acts on this work.',
                     'since': NOT_RECORDED, 'resolves_when': 'The action job finishes.', 'valid': 'VALID'})
    if job['state'] == 'RETRY_WAIT' and job['activity_code'] in ('WAITING_FOR_DOWNLOAD', 'DOWNLOADING'):
        deps.append({'type': 'EXTERNAL_DOWNLOAD', 'reason': 'qBittorrent download in progress.',
                     'since': NOT_RECORDED, 'resolves_when': 'The selected file reaches 100%.', 'valid': NOT_CHECKED})
    elif job['state'] == 'RETRY_WAIT' and job['activity_code'] == 'WAITING_FOR_SEARCH':
        deps.append({'type': 'EXTERNAL_SEARCH', 'reason': 'Indexer search pending.', 'since': NOT_RECORDED,
                     'resolves_when': 'A search returns a usable release.', 'valid': NOT_CHECKED})
    if op and op['state'] in ('CANCEL_REQUESTED', 'CANCELED') and job['state'] not in FINISHED:
        deps.append({'type': 'OPERATION_CANCELLATION', 'blocking_operation': op['operation_id'],
                     'blocking_status': f'{op["state"]} · cleanup {op["cleanup_state"]}',
                     'reason': 'The work was canceled.', 'since': NOT_RECORDED,
                     'resolves_when': 'Cleanup completes.', 'valid': 'VALID'})
    return deps


def chain(db, deps, have, start_id):
    """Follow blockers of blockers (bounded, cycle-safe) for held reservation jobs."""
    seen, out, current = {start_id}, [], deps
    for _ in range(MAX_HOPS):
        nxt = next((d.get('next') for d in current if d.get('next')), None)
        if nxt is None: break
        if nxt['job_id'] in seen:
            out.append(f'Cycle: Job #{nxt["job_id"]} was already visited.'); break
        seen.add(nxt['job_id'])
        job = _job(db, nxt['job_id'])
        out.append(_label(job))
        if job is None: break
        res = reservation(db, job, have)
        current = dependencies(db, job, _operation(db, job['job_id']), res)
    else:
        out.append(f'Stopped after {MAX_HOPS} hops.')
    return out


def diagnose(db, job_id, now=None):
    now = int(time.time() * 1000) if now is None else now
    have = _tables(db)
    job = _job(db, job_id)
    if job is None: return None
    op = _operation(db, job_id)
    members = [m['job_id'] for m in _members(db, op['operation_id'])] if op else [job_id]
    member_rows = _members(db, op['operation_id']) if op else []
    res = reservation(db, job, have)
    rec = recovery(db, job, op, now)
    deps = dependencies(db, job, op, res)
    creator = _job(db, job['created_by_job_id']) if job['created_by_job_id'] else None
    groups = {'complete': [], 'executing': [], 'waiting': [], 'intervention': [], 'canceled': []}
    for m in member_rows:
        key = {'DONE': 'complete', 'RUNNING': 'executing', 'PENDING': 'waiting', 'RETRY_WAIT': 'waiting',
               'ON_HOLD': 'waiting', 'CANCELED': 'canceled'}.get(m['state'], 'intervention')
        groups[key].append(m['job_id'])
    progress = 'Yes — a job is queued, running or scheduled.' if any(
        m['state'] in LIVE for m in member_rows) else UNKNOWN
    summ = summary(db, job, op, res, rec, now)
    if progress == UNKNOWN and op:
        progress = ('No — every job has finished.' if all(m['state'] in FINISHED for m in member_rows)
                    else 'Only through held jobs: ' + summ['automatic'] if job['state'] == 'ON_HOLD'
                    else 'No known path without intervention.')
    return {'job_id': job_id, 'state': summ, 'creator': _label(creator) if creator else None,
            'dependencies': deps, 'chain': chain(db, deps, have, job_id), 'reservation': res,
            'downloads': downloads(db, members, have), 'next_step': next_step(db, op, member_rows),
            'recovery': rec,
            'operation': op and {**op, 'groups': groups, 'progress': progress}}
