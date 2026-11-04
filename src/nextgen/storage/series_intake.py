"""Series-first Intake read model and manual missing-episode search.

Existing per-operation Keep jobs and Prowlarr discovery do all real work.
No new tables, provider adapters, or import jobs are needed.
"""
import json

from src.nextgen.anime_titles import preferred_title
from src.nextgen.history import integer, append_job_history
from src.nextgen.storage.cancellation import CancellationStore, can_cancel_operation
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction, enqueue_job_with_receipt
from src.nextgen.storage.sonarr_backfill import PAGE_SIZE, WAITING_FOR_DUB, plan_window


class IntakeUnavailable(ValueError):
    reason_code = 'MANAGEMENT_ACTION_UNAVAILABLE'


def _waiting_for_dub(db, plan_id):
    """episode -> (job_id, attempt) of held 'Waiting for dub' backfill jobs."""
    rows = db.execute(
        "SELECT json_extract(payload_json,'$.exhausted_episode'),job_id,attempt_count FROM jobs "
        "WHERE kind='DISCOVER_SONARR_BACKFILL' AND state='ON_HOLD' AND last_reason_code=? "
        "AND json_extract(payload_json,'$.plan_id')=?", (WAITING_FOR_DUB, plan_id)).fetchall()
    return {episode: (job, attempt) for episode, job, attempt in rows if type(episode) is int}



def _placements(db, anidb_id, plan_id):
    """AniDB episode number -> verified TVDB (season, episode), from stored evidence only.

    Used for labels and the TVDB-keyed reservation/staging lookups of Step 1.
    Unknown placements stay unknown; nothing is inferred.
    """
    found = {}
    for number, season, episode in db.execute(
            "SELECT anidb_episode_number,tvdb_season,tvdb_episode FROM recognized_episodes "
            "WHERE anidb_id=? UNION "
            "SELECT anidb_episode_number,tvdb_season,tvdb_episode FROM release_verifications "
            "WHERE anidb_id=? AND tvdb_order='official' UNION "
            "SELECT anidb_episode_number,tvdb_season,tvdb_episode FROM sonarr_episode_selections "
            "WHERE plan_id=?", (anidb_id, anidb_id, plan_id)).fetchall():
        found.setdefault(number, set()).add((season, episode))
    return {number: places.pop() for number, places in found.items() if len(places) == 1}


def _active_episode(db, anidb_id, season, episode, anidb_episode_id=None):
    """Read existing acquisition ownership; never infer staged from a reservation."""
    where_clause = "r.anidb_id=? AND r.anidb_episode_id=? AND r.state='ACTIVE'" if anidb_episode_id is not None else "r.anidb_id=? AND r.tvdb_season=? AND r.tvdb_episode=? AND r.state='ACTIVE'"
    args = (anidb_id, anidb_episode_id) if anidb_episode_id is not None else (anidb_id, season, episode)
    owner = db.execute(
        "SELECT r.reservation_id,r.owner_job_id,m.operation_id,o.state,"
        "r.anidb_episode_id,r.tvdb_episode_id,r.tvdb_series_id "
        f"FROM episode_reservations r "
        f"JOIN job_operation_members m ON m.job_id=r.owner_job_id "
        f"JOIN job_operations o ON o.operation_id=m.operation_id "
        f"WHERE {where_clause} "
        f"UNION ALL "
        f"SELECT r.reservation_id,r.owner_job_id,m.operation_id,o.state,"
        f"r.anidb_episode_id,r.tvdb_episode_id,r.tvdb_series_id "
        f"FROM batch_episode_reservations r "
        f"JOIN job_operation_members m ON m.job_id=r.owner_job_id "
        f"JOIN job_operations o ON o.operation_id=m.operation_id "
        f"WHERE {where_clause} "
        f"LIMIT 1",
        (*args, *args)
    ).fetchone()
    if owner is None:
        return None
    (reservation_id, owner_job_id, operation_id, operation_state,
     anidb_episode_id, tvdb_episode_id, tvdb_series_id) = owner
    # Prefer the actionable stopped step over the completed RESERVE_EPISODE job.
    step = db.execute(
        "SELECT j.job_id,j.state,j.last_reason_code "
        "FROM jobs j JOIN job_operation_members m USING(job_id) "
        "WHERE m.operation_id=? "
        "ORDER BY CASE j.state WHEN 'NEEDS_REVIEW' THEN 0 WHEN 'BLOCKED' THEN 1 "
        "WHEN 'RUNNING' THEN 2 WHEN 'RETRY_WAIT' THEN 3 WHEN 'PENDING' THEN 4 "
        "ELSE 5 END, j.job_id DESC LIMIT 1", (operation_id,)
    ).fetchone()
    waiting_tvdb = ""
    waiting_args = [anidb_id, anidb_episode_id, tvdb_series_id]
    if season is not None and episode is not None and tvdb_episode_id is not None:
        waiting_tvdb = "AND v.tvdb_season=? AND v.tvdb_episode=? AND v.tvdb_episode_id=? "
        waiting_args.extend([season, episode, tvdb_episode_id])
    else:
        waiting_tvdb = "AND v.tvdb_season IS NULL AND v.tvdb_episode IS NULL AND v.tvdb_episode_id IS NULL "
    waiting = db.execute(
        "SELECT j.job_id FROM jobs j "
        "JOIN job_operation_members m ON m.job_id=j.job_id "
        "JOIN job_operations o ON o.operation_id=m.operation_id "
        "JOIN trial_evaluations t ON t.job_id=json_extract(j.payload_json,'$.evaluation_job_id') "
        "JOIN release_verifications v ON v.verification_id=t.verification_id "
        "WHERE j.kind='RESERVE_EPISODE' AND j.state='ON_HOLD' "
        "AND j.last_reason_code='EPISODE_RESERVED' AND o.state='ACTIVE' "
        f"AND t.selected=1 AND v.anidb_id=? AND v.anidb_episode_id=? "
        f"AND v.tvdb_series_id=? {waiting_tvdb}"
        "ORDER BY j.created_at_ms,j.job_id",
        waiting_args
    ).fetchall()
    job_id, job_state, reason = step if step else (owner_job_id, 'DONE', None)
    state = ('CLEANUP_PENDING' if operation_state != 'ACTIVE' else
             job_state if job_state in ('NEEDS_REVIEW', 'BLOCKED') else 'IN_PROGRESS')
    return {'status': state, 'job_id': job_id, 'reason_code': reason,
            'operation_id': operation_id, 'reservation_id': reservation_id,
            'waiting_job_ids': [row[0] for row in waiting],
            'can_retry_job': operation_state == 'ACTIVE' and job_state == 'NEEDS_REVIEW',
            'can_try_next': (operation_state == 'ACTIVE' and
                             job_state in ('NEEDS_REVIEW', 'BLOCKED') and bool(waiting))}

def detail(db, anidb_id, language='english'):
    integer(anidb_id, positive=True)
    row = db.execute(
        'SELECT a.anidb_id,a.tvdb_series_id,a.title,t.english_title,t.romaji_title '
        'FROM identified_anime a LEFT JOIN anime_title_metadata t USING(anidb_id) '
        'WHERE a.anidb_id=?', (anidb_id,)).fetchone()
    if row is None:
        return None
    info = db.execute(
        'SELECT series_json FROM identity_results WHERE anidb_id=? '
        'ORDER BY validated_at_ms DESC LIMIT 1', (anidb_id,)).fetchone()
    snapshot = json.loads(info[0]) if info else {}
    anime = snapshot.get('anime', {})
    english = row[3] or anime.get('english_title')
    romaji = row[4] or snapshot.get('attributes', {}).get('romaji_title')
    result = {
        'anidb_id': row[0], 'tvdb_series_id': row[1],
        'title': preferred_title(row[2], english, romaji, language),
        'english_title': english, 'romaji_title': romaji,
        'original_title': anime.get('original_title'),
        'year': anime.get('year'), 'type': anime.get('type'),
        'description': None,  # The persisted AnimeSeries identity has no synopsis field.
        'episodes': [], 'missing': [], 'keep_operations': []
    }
    rejected = db.execute(
        "SELECT d.job_id,d.reason_text,d.cleanup_state,j.state FROM anime_review_decisions d "
        "JOIN jobs j ON j.job_id=d.job_id WHERE d.anidb_id=?", (anidb_id,)).fetchone()
    result['rejection'] = (None if rejected is None else {
        'job_id': rejected[0], 'reason': rejected[1],
        'cleanup_state': rejected[2], 'job_state': rejected[3]})
    # All historical reservations are counted, including released candidates.
    # Aggregate once rather than querying for every episode shown in Intake.
    reservation_counts = {}
    for season, episode, count in db.execute(
        "SELECT tvdb_season,tvdb_episode,count(*) FROM episode_reservations "
        "WHERE anidb_id=? GROUP BY tvdb_season,tvdb_episode "
        "UNION ALL "
        "SELECT tvdb_season,tvdb_episode,count(*) FROM batch_episode_reservations "
        "WHERE anidb_id=? GROUP BY tvdb_season,tvdb_episode", (anidb_id, anidb_id)
    ).fetchall():
        key = (season, episode)
        reservation_counts[key] = reservation_counts.get(key, 0) + count
    have_manual = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_release_submissions'"
    ).fetchone()
    manual_counts = {
        (season,episode):count for season,episode,count in db.execute(
            'SELECT tvdb_season,tvdb_episode,count(*) FROM manual_release_submissions '
            'WHERE anidb_id=? GROUP BY tvdb_season,tvdb_episode',(anidb_id,)
        ).fetchall()
    } if have_manual else {}
    # Existing single-episode staging (one underlying operation per release).
    single = db.execute(
        "SELECT r.tvdb_season,r.tvdb_episode,s.job_id,m.operation_id,o.state,"
        " k.job_id,k.state,"
        " (SELECT v.anidb_episode_number FROM release_verifications v WHERE v.anidb_id=r.anidb_id AND v.anidb_episode_id=r.anidb_episode_id LIMIT 1) "
        "FROM staging_publications s "
        "JOIN acquisition_plans p USING(plan_id) "
        "JOIN episode_reservations r USING(reservation_id) "
        "JOIN job_operation_members m ON m.job_id=s.job_id "
        "JOIN job_operations o ON o.operation_id=m.operation_id "
        "LEFT JOIN management_actions a ON a.operation_id=m.operation_id AND a.action='KEEP' "
        "LEFT JOIN jobs k ON k.job_id=a.job_id "
        "WHERE r.anidb_id=? AND s.state='PUBLISHED'",
        (anidb_id,)).fetchall()
    # Season-pack episodes use their existing batch Keep operation.
    batch = db.execute(
        "SELECT r.tvdb_season,r.tvdb_episode,s.job_id,m.operation_id,o.state,"
        " k.job_id,k.state,NULL FROM batch_staging_publications s "
        "JOIN batch_episode_reservations r ON r.plan_id=s.plan_id AND r.file_index=s.file_index "
        "JOIN job_operation_members m ON m.job_id=s.job_id "
        "JOIN job_operations o ON o.operation_id=m.operation_id "
        "LEFT JOIN management_actions a ON a.operation_id=m.operation_id AND a.action='KEEP' "
        "LEFT JOIN jobs k ON k.job_id=a.job_id "
        "WHERE r.anidb_id=? AND s.state='PUBLISHED'",
        (anidb_id,)).fetchall()
    operations = set()
    staged_episodes = set()
    staged_anidb_numbers = set()
    for season, episode, job, op, state, keep_job, keep_state, anidb_num in sorted(
        single + batch,
        key=lambda r: (r[0] is not None, r[0], r[1] is not None, r[1], r[7] is not None, r[7], r[2])
    ):
        status = ('Kept' if keep_state == 'DONE' else
                  'Importing' if keep_job is not None else
                  'Staged' if state == 'STAGED' else state.title())
        if rejected is not None and status == 'Staged':
            status = 'Rejected' if rejected[2] == 'COMPLETE' else 'Rejecting'
        result['episodes'].append({
            'season': season, 'episode': episode, 'job_id': job,
            'operation_id': op, 'status': status,
            'anidb_episode': anidb_num,
            'reservation_count': reservation_counts.get((season, episode), 0),
            'manual_submission_count': manual_counts.get((season, episode), 0)
        })
        if season is not None and episode is not None:
            staged_episodes.add((season, episode))
        if anidb_num is not None:
            staged_anidb_numbers.add(anidb_num)
        if state == 'STAGED' and keep_state != 'DONE':
            operations.add(op)
    result['keep_operations'] = sorted(operations) if rejected is None else []
    result['reject_operations'] = sorted(operations) if rejected is None else []
    # Fetch reservation coverage once per series; Intake may show large windows
    # and its summary is recomputed during polling.
    active_episodes = set(db.execute(
        "SELECT tvdb_season,tvdb_episode FROM episode_reservations "
        "WHERE anidb_id=? AND state='ACTIVE' AND tvdb_season IS NOT NULL UNION "
        "SELECT tvdb_season,tvdb_episode FROM batch_episode_reservations "
        "WHERE anidb_id=? AND state='ACTIVE'", (anidb_id, anidb_id)
    ).fetchall())
    active_anidb_eids = set(row[0] for row in db.execute(
        "SELECT anidb_episode_id FROM episode_reservations WHERE anidb_id=? AND state='ACTIVE' UNION "
        "SELECT anidb_episode_id FROM batch_episode_reservations WHERE anidb_id=? AND state='ACTIVE'",
        (anidb_id, anidb_id)
    ).fetchall())
    anidb_eids = dict(db.execute(
        "SELECT anidb_episode_number, anidb_episode_id FROM release_verifications WHERE anidb_id=?",
        (anidb_id,)).fetchall())
    # One plan per AniDB anime; its window is in AniDB episode numbers.
    plans = db.execute(
        'SELECT plan_id,anchor_episode,backfill_count,following_count '
        'FROM trial_plans WHERE anidb_id=?', (anidb_id,)
    ).fetchall()
    for plan_id, anchor, backfill, following in plans:
        observed = dict(db.execute(
            'SELECT anidb_episode_number,status FROM sonarr_backfill_results WHERE plan_id=?',
            (plan_id,)).fetchall())
        waiting = _waiting_for_dub(db, plan_id)
        placements = _placements(db, anidb_id, plan_id)
        # The anchor is intentionally excluded from automatic backfill, but
        # it is NOT excluded from Intake: its original acquisition may fail.
        visible = set(plan_window(db, plan_id, anchor, backfill, following))
        visible.add(anchor)
        for number in sorted(visible):
            place = placements.get(number)
            if number in staged_anidb_numbers or (place is not None and place in staged_episodes):
                continue
            eid = anidb_eids.get(number)
            if place in active_episodes:
                work = _active_episode(db, anidb_id, *place)
            elif eid is not None and eid in active_anidb_eids:
                work = _active_episode(db, anidb_id, None, None, anidb_episode_id=eid)
            else:
                work = None
            status = (work['status'] if work else
                      WAITING_FOR_DUB if number in waiting else observed.get(number, 'NOT_SEARCHED'))
            season, episode = place or (None, None)
            result['missing'].append({
                'anidb_episode': number,
                # Verified TVDB placement is a label only; None when not yet known.
                'season': season, 'episode': episode, 'status': status,
                'reservation_count': reservation_counts.get(place, 0),
                'manual_submission_count': manual_counts.get(place, 0),
                'can_retry': rejected is None and work is None and number != anchor
                             and status in ('NOT_SEARCHED', 'NO_RESULTS', 'NOT_AIRED',
                                            'MAPPING_MISSING', WAITING_FOR_DUB),
                **(work or {})
            })
    return result


# Intake row status, in display precedence. The dashboard filter uses these codes.
SERIES_STATUSES = ('REJECTED', 'STAGED', 'WAITING_FOR_DUB', 'KEPT', 'TRACKING')
SCAN_CHUNK = 50


def _summary(db, aid, language):
    item = detail(db, aid, language=language)
    row = {
        'anidb_id': aid, 'title': item['title'],
        'tvdb_series_id': item['tvdb_series_id'],
        'staged_count': sum(x['status'] == 'Staged' for x in item['episodes']),
        'kept_count': sum(x['status'] == 'Kept' for x in item['episodes']),
        'missing_count': len(item['missing']),
        'waiting_for_dub': sum(x['status'] == WAITING_FOR_DUB for x in item['missing']),
        'season_count': len({x['season'] for x in item['episodes'] + item['missing']
                             if x['season'] is not None}),
        'rejection': item['rejection']
    }
    row['status'] = ('REJECTED' if row['rejection'] else 'STAGED' if row['staged_count'] else
                     'WAITING_FOR_DUB' if row['waiting_for_dub'] else
                     'KEPT' if row['kept_count'] else 'TRACKING')
    return row


def list_items(db, before, limit, language='english', match=None, status=None):
    # ``match`` is an optional (sql, params) filter from src.nextgen.search.
    # ``status`` is computed per series, so filtered pages scan in chunks.
    if status is not None and status not in SERIES_STATUSES:
        raise ValueError('Invalid Intake status')
    where, params = match if match else ('1', [])
    items, cursor = [], before
    while True:
        rows = db.execute(
            'SELECT anidb_id FROM identified_anime WHERE anidb_id<? AND (' + where + ') '
            'ORDER BY anidb_id DESC LIMIT ?', (cursor, *params, SCAN_CHUNK)
        ).fetchall()
        for (aid,) in rows:
            if len(items) == limit:
                return {'items': items, 'next_before_id': items[-1]['anidb_id']}
            row = _summary(db, aid, language)
            if status is None or row['status'] == status:
                items.append(row)
        if len(rows) < SCAN_CHUNK:
            return {'items': items, 'next_before_id': None}
        cursor = rows[-1][0]


def keep_targets(db, anidb_id, operation_ids):
    info = detail(db, anidb_id)
    if info is None or type(operation_ids) is not list or not 1 <= len(operation_ids) <= 100:
        raise IntakeUnavailable()
    if any(type(x) is not int or x <= 0 for x in operation_ids):
        raise IntakeUnavailable()
    wanted = set(operation_ids)
    if len(wanted) != len(operation_ids) or not wanted.issubset(set(info['keep_operations'])):
        raise IntakeUnavailable()
    if info['rejection'] is not None:
        raise IntakeUnavailable()
    # Resolve each captured operation to an existing staged job. A new arrival
    # cannot silently become part of the approved Keep scope.
    rows = db.execute(
        'SELECT m.operation_id,MAX(m.job_id) FROM job_operation_members m '
        'WHERE m.operation_id IN (' + ','.join('?' for _ in wanted) + ') '
        'GROUP BY m.operation_id', tuple(sorted(wanted))
    ).fetchall()
    if len(rows) != len(wanted):
        raise IntakeUnavailable()
    return [job for op, job in sorted(rows)]


REJECTION_REASONS = ('Not interested', 'Already watched', 'Wrong anime', 'Other')


def reject_series(database, anidb_id, operation_ids, reason, note):
    """One durable series decision, with exact staged targets and canceled live work."""
    integer(anidb_id, positive=True)
    if (type(operation_ids) is not list or len(operation_ids) > 100
            or any(type(x) is not int or x <= 0 for x in operation_ids)
            or len(set(operation_ids)) != len(operation_ids)
            or type(reason) is not str or reason not in REJECTION_REASONS
            or type(note) is not str or len(note) > 300
            or any(ord(c) < 32 and c not in '\n\t' for c in note)):
        raise IntakeUnavailable()
    explanation = reason + (': ' + note.strip() if note.strip() else '')
    queue = JobQueue(database)
    with queue.connection() as db, transaction(db):
        info = detail(db, anidb_id)
        if info is None:
            raise IntakeUnavailable()
        if info['rejection'] is not None:
            existing = info['rejection']['job_id']
            kind = db.execute('SELECT kind FROM jobs WHERE job_id=?', (existing,)).fetchone()
            if kind != ('REJECT_SERIES',):
                raise IntakeUnavailable()
            return {'job_id': existing, 'reused': True}
        if sorted(operation_ids) != info['reject_operations']:
            raise IntakeUnavailable()
        for op in operation_ids:
            if db.execute("SELECT 1 FROM management_actions WHERE operation_id=? "
                          "AND action='KEEP'", (op,)).fetchone():
                raise IntakeUnavailable()
        control, _ = enqueue_job_with_receipt(
            db, kind='REJECT_SERIES',
            payload={'anidb_id': anidb_id, 'operation_ids': sorted(operation_ids)},
            component='api')
        control_op = db.execute('SELECT operation_id FROM job_operation_members '
                                'WHERE job_id=?', (control,)).fetchone()[0]
        now = queue.now()
        db.execute("INSERT INTO anime_review_decisions "
                   "(anidb_id,operation_id,job_id,decision,cleanup_state,decided_at_ms,reason_text) "
                   "VALUES(?,?,?,'REJECT','PENDING',?,?)",
                   (anidb_id, control_op, control, now, explanation))
        append_job_history(db, job_id=control, event_code='SERIES_REJECTED',
                           reason_code='ANIME_REJECTED', component='api',
                           occurred_at_ms=now, details={'attempt': 0})
        # Existing reservations may already be downloading. Request their normal
        # operation cancellation, then queue the existing owned-torrent cleaner.
        # The decision above blocks any subsequent new reservations.
        active = db.execute(
            "SELECT m.operation_id,MIN(r.owner_job_id) FROM "
            "(SELECT owner_job_id FROM episode_reservations WHERE anidb_id=? "
            "UNION ALL SELECT owner_job_id FROM batch_episode_reservations "
            "WHERE anidb_id=?) r "
            "JOIN job_operation_members m ON m.job_id=r.owner_job_id "
            "JOIN job_operations o ON o.operation_id=m.operation_id "
            "WHERE o.state='ACTIVE' GROUP BY m.operation_id",
            (anidb_id, anidb_id)).fetchall()
        cancel = CancellationStore(database)
        for op, target in active:
            current = db.execute(
                'SELECT operation_id,state,cleanup_state,reason_code '
                'FROM job_operations WHERE operation_id=?', (op,)).fetchone()
            if not can_cancel_operation(db, current):
                continue
            cancel.request_in_transaction(db, target)
            if db.execute("SELECT 1 FROM management_actions WHERE operation_id=? "
                          "AND action='CLEANUP'", (op,)).fetchone():
                continue
            event = db.execute('SELECT event_id FROM jobs WHERE job_id=?',
                               (target,)).fetchone()[0]
            job, _ = enqueue_job_with_receipt(
                db, kind='CLEANUP_CANCELED_OPERATION',
                payload={'target_job_id': target}, event_id=event,
                component='api')
            db.execute("INSERT INTO management_actions "
                       "(operation_id,action,target_job_id,job_id,created_at_ms) "
                       "VALUES(?,'CLEANUP',?,?,?)", (op, target, job, now))
        return {'job_id': control, 'reused': False}


def retry_episode(database, anidb_id, episode, *, delete_cache=True):
    """Search one missing AniDB episode of the anime's plan again."""
    integer(anidb_id, positive=True)
    integer(episode, positive=True)
    if type(delete_cache) is not bool:
        raise IntakeUnavailable()
    queue = JobQueue(database)
    with queue.connection() as db, transaction(db):
        row = db.execute(
            "SELECT p.plan_id,p.anchor_episode,p.backfill_count,p.following_count,"
            " j.event_id,j.job_id FROM trial_plans p "
            "JOIN release_verifications v ON v.verification_id=p.anchor_verification_id "
            "JOIN jobs j ON j.job_id=v.job_id "
            "WHERE p.anidb_id=?",
            (anidb_id,)).fetchone()
        if row is None:
            raise IntakeUnavailable()
        if db.execute("SELECT 1 FROM anime_review_decisions WHERE anidb_id=?", (anidb_id,)).fetchone():
            raise IntakeUnavailable()
        plan_id, anchor, backfill, following, event_id, parent_job = row
        numbers = plan_window(db, plan_id, anchor, backfill, following)
        if episode not in numbers:
            raise IntakeUnavailable()
        held = _waiting_for_dub(db, plan_id).get(episode)
        # A repeated click must not enqueue another worker while retry is pending.
        active_retry = db.execute(
            "SELECT job_id FROM jobs WHERE kind='DISCOVER_SONARR_BACKFILL' "
            "AND state IN ('PENDING','RUNNING','RETRY_WAIT') "
            "AND json_extract(payload_json,'$.plan_id')=? "
            "AND (json_extract(payload_json,'$.exhausted_episode')=? "
            "OR json_extract(payload_json,'$.retry_episode')=?) "
            "ORDER BY job_id DESC LIMIT 1", (plan_id, episode, episode)).fetchone()
        if active_retry:
            return {'job_id':active_retry[0], 'reused':True}
        prior = db.execute(
            'SELECT status FROM sonarr_backfill_results WHERE plan_id=? AND anidb_episode_number=?',
            (plan_id, episode)).fetchone()
        if held is None and prior and prior[0] not in ('NO_RESULTS', 'NOT_AIRED',
                                                         'MAPPING_MISSING', WAITING_FOR_DUB):
            raise IntakeUnavailable()
        place = _placements(db, anidb_id, plan_id).get(episode)
        for table in ('episode_reservations', 'batch_episode_reservations'):
            if place is not None and db.execute(
                f"SELECT 1 FROM {table} WHERE anidb_id=? AND tvdb_season=? "
                "AND tvdb_episode=? AND state='ACTIVE' LIMIT 1",
                (anidb_id, *place)).fetchone():
                raise IntakeUnavailable()
        if held is None:
            if prior and prior[0] == WAITING_FOR_DUB:
                has_candidates = db.execute(
                    "SELECT 1 FROM sonarr_episode_candidates WHERE plan_id=? AND anidb_episode_number=? LIMIT 1",
                    (plan_id, episode)).fetchone() is not None
                if has_candidates:
                    return _search_episode(db, plan_id, numbers, episode, event_id, parent_job,
                                           exhausted=True, delete_cache=delete_cache)
            elif prior and prior[0] == 'NO_RESULTS' and not delete_cache:
                from src.nextgen.storage.sonarr_cached_retry import load_cached
                if load_cached(database, plan_id, episode, False) is not None:
                    return _search_episode(db, plan_id, numbers, episode, event_id, parent_job,
                                           exhausted=True, delete_cache=False)
            return _search_episode(db, plan_id, numbers, episode, event_id, parent_job)
    # Waiting for dub: "Search again" is the held job's existing Retry.
    from src.nextgen.storage.review_retry import request_review_retry, RetryNotAllowed, RetryStale
    try:
        receipt = request_review_retry(database, held[0], expected_attempt=held[1],
                                       expected_reason=WAITING_FOR_DUB,
                                       delete_cache=delete_cache)
    except (RetryNotAllowed, RetryStale):
        raise IntakeUnavailable() from None
    return {'job_id': receipt.job_id, 'reused': receipt.outcome == 'ALREADY_QUEUED'}


def _search_episode(db, plan_id, numbers, episode, event_id, parent_job, *, exhausted=False, delete_cache=True):
    pending = db.execute(
        "SELECT job_id FROM jobs WHERE kind='DISCOVER_SONARR_BACKFILL' "
        "AND state IN ('PENDING','RUNNING','RETRY_WAIT') "
        "AND json_extract(payload_json,'$.plan_id')=? "
        "AND (json_extract(payload_json,'$.retry_episode')=? "
        "OR json_extract(payload_json,'$.exhausted_episode')=? "
        "OR json_extract(payload_json,'$.retry_episode') IS NULL) LIMIT 1",
        (plan_id, episode, episode)).fetchone()
    if pending:
        return {'job_id': pending[0], 'reused': True}
    start = (numbers.index(episode) // PAGE_SIZE) * PAGE_SIZE + 1
    payload = {'plan_id': plan_id, 'start': start}
    if exhausted:
        payload['exhausted_episode'] = episode
        payload['delete_cache'] = delete_cache
    else:
        payload['retry_episode'] = episode
    job, _ = enqueue_job_with_receipt(
        db, kind='DISCOVER_SONARR_BACKFILL',
        payload=payload,
        event_id=event_id, created_by_job_id=parent_job, component='api'
    )
    return {'job_id': job, 'reused': False}
