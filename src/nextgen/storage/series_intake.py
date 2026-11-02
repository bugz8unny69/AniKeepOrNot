"""Series-first Intake read model and manual missing-episode search.

Existing per-operation Keep jobs and Prowlarr discovery do all real work.
No new tables, provider adapters, or import jobs are needed.
"""
import json

from src.nextgen.history import integer, append_job_history
from src.nextgen.storage.cancellation import CancellationStore, can_cancel_operation
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction, enqueue_job_with_receipt
from src.nextgen.storage.sonarr_backfill import PAGE_SIZE, plan_window


class IntakeUnavailable(ValueError):
    reason_code = 'MANAGEMENT_ACTION_UNAVAILABLE'


def detail(db, anidb_id):
    integer(anidb_id, positive=True)
    row = db.execute(
        'SELECT anidb_id,tvdb_series_id,title FROM identified_anime WHERE anidb_id=?',
        (anidb_id,)).fetchone()
    if row is None:
        return None
    info = db.execute(
        'SELECT series_json FROM identity_results WHERE anidb_id=? '
        'ORDER BY validated_at_ms DESC LIMIT 1', (anidb_id,)).fetchone()
    anime = json.loads(info[0]).get('anime', {}) if info else {}
    result = {
        'anidb_id': row[0], 'tvdb_series_id': row[1], 'title': row[2],
        'english_title': anime.get('english_title'), 'original_title': anime.get('original_title'),
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
    # Existing single-episode staging (one underlying operation per release).
    single = db.execute(
        "SELECT r.tvdb_season,r.tvdb_episode,s.job_id,m.operation_id,o.state,"
        " k.job_id,k.state FROM staging_publications s "
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
        " k.job_id,k.state FROM batch_staging_publications s "
        "JOIN batch_episode_reservations r ON r.plan_id=s.plan_id AND r.file_index=s.file_index "
        "JOIN job_operation_members m ON m.job_id=s.job_id "
        "JOIN job_operations o ON o.operation_id=m.operation_id "
        "LEFT JOIN management_actions a ON a.operation_id=m.operation_id AND a.action='KEEP' "
        "LEFT JOIN jobs k ON k.job_id=a.job_id "
        "WHERE r.anidb_id=? AND s.state='PUBLISHED'",
        (anidb_id,)).fetchall()
    operations = set()
    staged_episodes = set()
    for season, episode, job, op, state, keep_job, keep_state in sorted(single + batch):
        status = ('Kept' if keep_state == 'DONE' else
                  'Importing' if keep_job is not None else
                  'Staged' if state == 'STAGED' else state.title())
        if rejected is not None and status == 'Staged':
            status = 'Rejected' if rejected[2] == 'COMPLETE' else 'Rejecting'
        result['episodes'].append({
            'season': season, 'episode': episode, 'job_id': job,
            'operation_id': op, 'status': status
        })
        staged_episodes.add((season, episode))
        if state == 'STAGED' and keep_state != 'DONE':
            operations.add(op)
    result['keep_operations'] = sorted(operations) if rejected is None else []
    result['reject_operations'] = sorted(operations) if rejected is None else []
    plans = db.execute(
        'SELECT plan_id,tvdb_season,anchor_episode,backfill_count,following_count '
        'FROM trial_plans WHERE anidb_id=? ORDER BY tvdb_season', (anidb_id,)
    ).fetchall()
    for plan_id, season, anchor, backfill, following in plans:
        observed = dict(db.execute(
            'SELECT tvdb_episode,status FROM sonarr_backfill_results WHERE plan_id=?',
            (plan_id,)).fetchall())
        for episode in plan_window(db, plan_id, anchor, backfill, following):
            if (season, episode) in staged_episodes:
                continue
            status = observed.get(episode, 'NOT_SEARCHED')
            result['missing'].append({
                'season': season, 'episode': episode, 'status': status,
                'can_retry': rejected is None and status in ('NOT_SEARCHED', 'NO_RESULTS', 'NOT_AIRED', 'MAPPING_MISSING')
            })
    return result


def list_items(db, before, limit, match=None):
    # ``match`` is an optional (sql, params) filter from src.nextgen.search.
    where, params = match if match else ('1', [])
    rows = db.execute(
        'SELECT anidb_id FROM identified_anime WHERE anidb_id<? AND (' + where + ') '
        'ORDER BY anidb_id DESC LIMIT ?', (before, *params, limit + 1)
    ).fetchall()
    items = []
    for (aid,) in rows[:limit]:
        item = detail(db, aid)
        items.append({
            'anidb_id': aid, 'title': item['title'],
            'tvdb_series_id': item['tvdb_series_id'],
            'staged_count': sum(x['status'] == 'Staged' for x in item['episodes']),
            'kept_count': sum(x['status'] == 'Kept' for x in item['episodes']),
            'missing_count': sum(x['can_retry'] for x in item['missing']),
            'season_count': len({x['season'] for x in item['episodes'] + item['missing']}),
            'rejection': item['rejection']
        })
    return {'items': items, 'next_before_id': rows[limit - 1][0] if len(rows) > limit else None}


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


def retry_episode(database, anidb_id, season, episode):
    integer(anidb_id, positive=True)
    integer(season)
    integer(episode, positive=True)
    if season < 0:
        raise IntakeUnavailable()
    queue = JobQueue(database)
    with queue.connection() as db, transaction(db):
        row = db.execute(
            "SELECT p.plan_id,p.anchor_episode,p.backfill_count,p.following_count,"
            " j.event_id,j.job_id FROM trial_plans p "
            "JOIN release_verifications v ON v.verification_id=p.anchor_verification_id "
            "JOIN jobs j ON j.job_id=v.job_id "
            "WHERE p.anidb_id=? AND p.tvdb_season=?",
            (anidb_id, season)).fetchone()
        if row is None:
            raise IntakeUnavailable()
        if db.execute("SELECT 1 FROM anime_review_decisions WHERE anidb_id=?", (anidb_id,)).fetchone():
            raise IntakeUnavailable()
        plan_id, anchor, backfill, following, event_id, parent_job = row
        numbers = plan_window(db, plan_id, anchor, backfill, following)
        if episode not in numbers:
            raise IntakeUnavailable()
        prior = db.execute(
            'SELECT status FROM sonarr_backfill_results WHERE plan_id=? AND tvdb_episode=?',
            (plan_id, episode)).fetchone()
        if prior and prior[0] not in ('NO_RESULTS', 'NOT_AIRED', 'MAPPING_MISSING'):
            raise IntakeUnavailable()
        for table in ('episode_reservations', 'batch_episode_reservations'):
            if db.execute(
                f"SELECT 1 FROM {table} WHERE anidb_id=? AND tvdb_season=? "
                "AND tvdb_episode=? AND state='ACTIVE' LIMIT 1",
                (anidb_id, season, episode)).fetchone():
                raise IntakeUnavailable()
        pending = db.execute(
            "SELECT job_id FROM jobs WHERE kind='DISCOVER_SONARR_BACKFILL' "
            "AND state IN ('PENDING','RUNNING','RETRY_WAIT') "
            "AND json_extract(payload_json,'$.plan_id')=? "
            "AND (json_extract(payload_json,'$.retry_episode')=? "
            "OR json_extract(payload_json,'$.retry_episode') IS NULL) LIMIT 1",
            (plan_id, episode)).fetchone()
        if pending:
            return {'job_id': pending[0], 'reused': True}
        start = (numbers.index(episode) // PAGE_SIZE) * PAGE_SIZE + 1
        job, _ = enqueue_job_with_receipt(
            db, kind='DISCOVER_SONARR_BACKFILL',
            payload={'plan_id': plan_id, 'start': start, 'retry_episode': episode},
            event_id=event_id, created_by_job_id=parent_job, component='api'
        )
        return {'job_id': job, 'reused': False}
