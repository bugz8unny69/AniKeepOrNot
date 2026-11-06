"""Episode-scoped reset of failed acquisition ownership.

Only the existing cancellation/owned-resource cleanup may free reservations.
The reset worker waits until every affected operation has completed cleanup,
then retires candidate caches and schedules one fresh Prowlarr search.
"""
import time

from src.nextgen.history import append_job_history
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import enqueue_job_with_receipt, transaction


class EpisodeResetUnavailable(ValueError):
    reason_code = 'MANAGEMENT_ACTION_UNAVAILABLE'


def _scope(db, plan_id, episode, *, allow_staged=False):
    from src.nextgen.storage.series_intake import _placements
    from src.nextgen.storage.sonarr_backfill import plan_window
    row = db.execute(
        'SELECT anidb_id,tvdb_series_id,anchor_episode,backfill_count,following_count '
        'FROM trial_plans WHERE plan_id=?', (plan_id,)
    ).fetchone()
    if row is None:
        raise EpisodeResetUnavailable()
    aid, tid, anchor, backfill, following = row
    if episode not in set(plan_window(db, plan_id, anchor, backfill, following)) | {anchor}:
        raise EpisodeResetUnavailable()
    place = _placements(db, aid, plan_id).get(episode)
    if place is None:
        # Cannot safely enumerate all owners by guessed TVDB placement.
        raise EpisodeResetUnavailable()
    if db.execute(
        'SELECT 1 FROM anime_review_decisions WHERE anidb_id=?', (aid,)
    ).fetchone():
        raise EpisodeResetUnavailable()
    season, number = place
    for table, join in (() if allow_staged else (
        ('staging_publications',
         'JOIN acquisition_plans p USING(plan_id) JOIN episode_reservations r USING(reservation_id)'),
        ('batch_staging_publications',
         'JOIN batch_episode_reservations r ON r.plan_id=s.plan_id AND r.file_index=s.file_index'),
    )):
        if db.execute(
            f'SELECT 1 FROM {table} s {join} '
            'WHERE s.state=\'PUBLISHED\' AND r.anidb_id=? '
            'AND r.tvdb_season=? AND r.tvdb_episode=? LIMIT 1',
            (aid, season, number)
        ).fetchone():
            raise EpisodeResetUnavailable()
    return aid, tid, season, number, anchor, backfill, following


def _owners(db, plan_id, episode, aid, season, number):
    """Operation and source job for ALL matching releases, including manual ones."""
    rows = db.execute(
        'SELECT m.operation_id,MIN(target.job_id) FROM ('
        ' SELECT r.owner_job_id AS job_id FROM episode_reservations r '
        ' WHERE r.anidb_id=? AND r.tvdb_season=? AND r.tvdb_episode=? '
        ' UNION ALL SELECT r.owner_job_id FROM batch_episode_reservations r '
        ' WHERE r.anidb_id=? AND r.tvdb_season=? AND r.tvdb_episode=? '
        ' UNION ALL SELECT j.job_id FROM manual_release_submissions s '
        ' JOIN jobs j ON j.event_id=s.event_id '
        ' WHERE s.anidb_id=? AND s.tvdb_season=? AND s.tvdb_episode=? '
        ' UNION ALL SELECT j.job_id FROM sonarr_magnet_intents i '
        ' JOIN jobs j ON j.event_id=i.event_id '
        ' WHERE i.plan_id=? AND i.anidb_episode_number=? '
        ') target JOIN job_operation_members m ON m.job_id=target.job_id '
        'GROUP BY m.operation_id ORDER BY m.operation_id',
        (aid,season,number,aid,season,number,aid,season,number,plan_id,episode)
    ).fetchall()
    return tuple(rows)


def _foreign_reservation(db, operation_id, aid, season, number):
    for table in ('episode_reservations', 'batch_episode_reservations'):
        if db.execute(
            f'SELECT 1 FROM {table} r JOIN job_operation_members m '
            'ON m.job_id=r.owner_job_id '
            'WHERE m.operation_id=? AND r.state=\'ACTIVE\' AND '
            '(r.anidb_id!=? OR r.tvdb_season IS NOT ? OR r.tvdb_episode IS NOT ?) LIMIT 1',
            (operation_id,aid,season,number)
        ).fetchone():
            return True
    return False


def _pending_discovery(db, plan_id):
    return db.execute(
        "SELECT 1 FROM jobs WHERE kind='DISCOVER_SONARR_BACKFILL' "
        "AND state IN ('PENDING','RUNNING','RETRY_WAIT') "
        "AND json_extract(payload_json,'$.plan_id')=? LIMIT 1", (plan_id,)
    ).fetchone() is not None


def _pending_unowned_manual(db, aid, season, number):
    return db.execute(
        'SELECT j.job_id FROM manual_release_submissions s '
        'JOIN jobs j ON j.event_id=s.event_id '
        'LEFT JOIN job_operation_members m ON m.job_id=j.job_id '
        'WHERE s.anidb_id=? AND s.tvdb_season=? AND s.tvdb_episode=? '
        "AND m.job_id IS NULL AND j.state IN ('PENDING','RETRY_WAIT','RUNNING','ON_HOLD','NEEDS_REVIEW','FAILED')",
        (aid,season,number)
    ).fetchall()


def _ready(db, plan_id, episode):
    aid, tid, season, number, *_ = _scope(db, plan_id, episode)
    if _pending_discovery(db, plan_id) or _pending_unowned_manual(db,aid,season,number):
        return False
    for op, _ in _owners(db,plan_id,episode,aid,season,number):
        if _foreign_reservation(db,op,aid,season,number):
            raise EpisodeResetUnavailable()
        row=db.execute('SELECT state,cleanup_state FROM job_operations WHERE operation_id=?',(op,)).fetchone()
        if row is None or row[0]=='STAGED' or row[0]=='CANCELED' and row[1]=='REVIEW':
            raise EpisodeResetUnavailable()
        if row == ('CANCELED','COMPLETE'):
            continue
        # An idle operation is harmless only when it owns no resources or live work.
        if row[0]=='ACTIVE':
            from src.nextgen.storage.cancellation import can_cancel_operation, unfinished_resources
            if not can_cancel_operation(db,(op,*row,None)) and not unfinished_resources(db,op):
                continue
        return False
    for table in ('episode_reservations','batch_episode_reservations'):
        if db.execute(
            f'SELECT 1 FROM {table} WHERE anidb_id=? AND tvdb_season=? '
            "AND tvdb_episode=? AND state='ACTIVE' LIMIT 1", (aid,season,number)
        ).fetchone():
            return False
    return True


def progress(database,plan_id,episode):
    """Idempotent cancellation scheduling. Never deletes a reservation or torrent."""
    from src.nextgen.storage.cancellation import CancellationStore, can_cancel_operation, cancel_job
    from src.nextgen.storage.management_actions import ActionUnavailable
    from src.nextgen.cancellation import CancellationError
    queue=JobQueue(database)
    with queue.connection() as db, transaction(db):
        aid,_,season,number,*_=_scope(db,plan_id,episode)
        now=queue.now()
        if _pending_discovery(db,plan_id):
            return 'WAIT'
        # A queued manual VERIFY_RELEASE may not yet have been claimed/assigned
        # an operation. Cancel it, rather than let it acquire after the reset.
        for (job_id,) in _pending_unowned_manual(db,aid,season,number):
            cancel_job(db,job_id,now,'worker')
        owners=_owners(db,plan_id,episode,aid,season,number)
        # Validate every target before starting any cancellation: a batch
        # owning another episode must not be partially torn down.
        for op,_ in owners:
            if _foreign_reservation(db,op,aid,season,number):
                return 'REVIEW'
            owner_state=db.execute(
                'SELECT state,cleanup_state FROM job_operations WHERE operation_id=?',(op,)
            ).fetchone()
            if owner_state is None or owner_state[0]=='STAGED' or (
                    owner_state[0]=='CANCELED' and owner_state[1]=='REVIEW'):
                return 'REVIEW'
        for op,target in owners:
            row=db.execute(
                'SELECT operation_id,state,cleanup_state,reason_code FROM job_operations '
                'WHERE operation_id=?',(op,)
            ).fetchone()
            if row is None or row[1]=='STAGED' or (row[1]=='CANCELED' and row[2]=='REVIEW'):
                return 'REVIEW'
            if row[1]=='ACTIVE':
                if not can_cancel_operation(db,row):
                    continue
                try:
                    CancellationStore(database).request_in_transaction(db,target)
                except CancellationError:
                    return 'REVIEW'
                row=db.execute(
                    'SELECT operation_id,state,cleanup_state,reason_code FROM job_operations '
                    'WHERE operation_id=?',(op,)
                ).fetchone()
            if row[1] in ('CANCEL_REQUESTED','CANCELED') and row[2]!='COMPLETE':
                old=db.execute(
                    "SELECT a.job_id,j.state FROM management_actions a "
                    "JOIN jobs j ON j.job_id=a.job_id WHERE a.operation_id=? AND a.action='CLEANUP'",
                    (op,)
                ).fetchone()
                if old is None:
                    event=db.execute('SELECT event_id FROM jobs WHERE job_id=?',(target,)).fetchone()[0]
                    control,_=enqueue_job_with_receipt(
                        db,kind='CLEANUP_CANCELED_OPERATION',
                        payload={'target_job_id':target},event_id=event,component='worker')
                    db.execute(
                        "INSERT INTO management_actions(operation_id,action,target_job_id,job_id,created_at_ms) "
                        "VALUES(?,'CLEANUP',?,?,?)",(op,target,control,now)
                    )
                elif old[1] in ('NEEDS_REVIEW','BLOCKED','FAILED','DONE'):
                    return 'REVIEW'
        return 'READY' if _ready(db,plan_id,episode) else 'WAIT'


def _size_source(db,plan_id,episode,aid,season,number):
    """Newest previously size-rejected source; verification still runs again."""
    size_reasons=('ACQUISITION_SIZE_LIMIT','ACQUISITION_PLAN_SIZE_LIMIT')
    common=(
        "EXISTS (SELECT 1 FROM jobs j JOIN job_history h ON h.job_id=j.job_id "
        "WHERE j.event_id=source.event_id AND h.reason_code IN (?,?))"
    )
    sources=(
        "SELECT source.event_id,source.info_hash,source.source,source.metainfo,"
        "e.raw_body,i.series_json,source.created_at_ms FROM manual_release_submissions source "
        "JOIN incoming_events e ON e.event_id=source.event_id "
        "JOIN identity_results i ON i.event_id=source.event_id AND i.anidb_id=? "
        "WHERE source.anidb_id=? AND source.tvdb_season=? AND source.tvdb_episode=? AND "+common+
        " UNION ALL SELECT source.event_id,source.info_hash,'magnet',NULL,"
        "e.raw_body,i.series_json,source.created_at_ms FROM sonarr_magnet_intents source "
        "JOIN incoming_events e ON e.event_id=source.event_id "
        "JOIN identity_results i ON i.event_id=source.event_id AND i.anidb_id=? "
        "WHERE source.plan_id=? AND source.anidb_episode_number=? AND "+common+
        " ORDER BY 7 DESC LIMIT 1"
    )
    return db.execute(sources,(
        aid,aid,season,number,*size_reasons,
        aid,plan_id,episode,*size_reasons
    )).fetchone()


def can_replay(db,plan_id,episode,aid,season,number):
    # The smaller historical Intake test fixtures have no magnet-intent table.
    # Keep their read model compatible while real migrated DBs use v62.
    tables={r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if 'sonarr_magnet_intents' not in tables:
        return False
    return _size_source(db,plan_id,episode,aid,season,number) is not None


def _replay(db,claim,source,aid,season,number,now):
    """Clone immutable input into a fresh manual-scoped VERIFY_RELEASE attempt."""
    import json
    from src.nextgen.history import append_event_history
    from src.nextgen.storage.sqlite import persist_identity
    event_id,digest,method,metainfo,raw,series,_=source
    document=json.loads(bytes(raw))
    if (document.get('torrent_infohash')!=digest
            or type(document.get('release_name')) is not str
            or type(document.get('magnet_uri')) is not str
            or method not in ('torrent','magnet')):
        raise EpisodeResetUnavailable()
    new_event=db.execute(
        'INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES(?,?,?)',
        ('manual-release',raw,now)
    ).lastrowid
    append_event_history(db,event_id=new_event,event_code='ACCEPTED',
                         component='worker',occurred_at_ms=now)
    persist_identity(db,new_event,json.loads(series),validated_at_ms=now)
    job,audit=enqueue_job_with_receipt(
        db,kind='VERIFY_RELEASE',payload={'anidb_id':aid},
        component='worker',event_id=new_event,created_by_job_id=claim.job_id
    )
    db.execute(
        'INSERT INTO manual_release_submissions('
        'event_id,verify_job_id,anidb_id,tvdb_season,tvdb_episode,'
        'info_hash,source,metainfo,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?)',
        (new_event,job,aid,season,number,digest,method,metainfo,now)
    )
    return audit


def finish(db,claim,plan_id,episode,mode='search'):
    """Worker-fenced transaction: archive only AFTER the owned cleanup completed."""
    if not db.in_transaction or not _ready(db,plan_id,episode):
        raise EpisodeResetUnavailable()
    from src.nextgen.storage.sonarr_cached_retry import clear_cached
    from src.nextgen.storage.sonarr_backfill import PAGE_SIZE,plan_window
    aid,_,season,number,anchor,backfill,following=_scope(db,plan_id,episode)
    if mode not in ('search','replay'):
        raise EpisodeResetUnavailable()
    source=_size_source(db,plan_id,episode,aid,season,number) if mode=='replay' else None
    if mode=='replay' and source is None:
        raise EpisodeResetUnavailable()
    now=time.time_ns()//1_000_000
    # Keep episode/job/hash history and exact TVDB links, but retire the active
    # candidate registry so the same hash can be submitted after a size change.
    db.execute(
        'INSERT OR IGNORE INTO manual_release_archive('
        'event_id,verify_job_id,anidb_id,tvdb_season,tvdb_episode,'
        'info_hash,source,created_at_ms,retired_at_ms) '
        'SELECT event_id,verify_job_id,anidb_id,tvdb_season,tvdb_episode,'
        'info_hash,source,created_at_ms,? FROM manual_release_submissions '
        'WHERE anidb_id=? AND tvdb_season=? AND tvdb_episode=?',
        (now,aid,season,number)
    )
    db.execute(
        'DELETE FROM manual_release_submissions WHERE anidb_id=? AND tvdb_season=? AND tvdb_episode=?',
        (aid,season,number)
    )
    clear_cached(db,plan_id,episode)
    db.execute('DELETE FROM sonarr_episode_candidates WHERE plan_id=? AND anidb_episode_number=?',
               (plan_id,episode))
    db.execute('DELETE FROM sonarr_episode_selections WHERE plan_id=? AND anidb_episode_number=?',
               (plan_id,episode))
    db.execute('DELETE FROM sonarr_backfill_results WHERE plan_id=? AND anidb_episode_number=?',
               (plan_id,episode))
    if mode=='replay':
        audit=_replay(db,claim,source,aid,season,number,now)
    else:
        numbers=tuple(sorted(set(plan_window(db,plan_id,anchor,backfill,following)) | ({anchor} if episode==anchor else set())))
        if episode not in numbers:
            raise EpisodeResetUnavailable()
        start=(numbers.index(episode)//PAGE_SIZE)*PAGE_SIZE+1
        job,audit=enqueue_job_with_receipt(
            db,kind='DISCOVER_SONARR_BACKFILL',
            payload={'plan_id':plan_id,'start':start,'retry_episode':episode},
            event_id=claim.event_id,created_by_job_id=claim.job_id,component='worker')
    record=append_job_history(
        db,job_id=claim.job_id,event_code='EPISODE_SEARCH_RESET',
        component='worker',occurred_at_ms=now,details={'attempt':claim.attempt})
    return (audit,record)


def schedule(database,aid,episode,*,mode='search'):
    """Queue exactly one reset for a selected AniDB episode; no I/O or deletion."""
    if mode not in ('search','replay'):
        raise EpisodeResetUnavailable()
    queue=JobQueue(database)
    with queue.connection() as db, transaction(db):
        row=db.execute(
            'SELECT p.plan_id,p.anchor_episode,v.job_id,j.event_id FROM trial_plans p '
            'JOIN release_verifications v ON v.verification_id=p.anchor_verification_id '
            'JOIN jobs j ON j.job_id=v.job_id WHERE p.anidb_id=?',(aid,)
        ).fetchone()
        if row is None:
            raise EpisodeResetUnavailable()
        plan_id,anchor,parent_job,event=row
        _,_,season,number,*_=_scope(db,plan_id,episode)
        if mode=='replay' and not can_replay(db,plan_id,episode,aid,season,number):
            raise EpisodeResetUnavailable()
        previous=db.execute(
            "SELECT job_id FROM jobs WHERE kind='RESET_EPISODE_SEARCH' "
            "AND state IN ('PENDING','RUNNING','RETRY_WAIT') "
            "AND json_extract(payload_json,'$.plan_id')=? "
            "AND json_extract(payload_json,'$.episode')=? "
            "ORDER BY job_id DESC LIMIT 1",(plan_id,episode)
        ).fetchone()
        if previous:
            return {'job_id':previous[0],'reused':True}
        job,_=enqueue_job_with_receipt(
            db,kind='RESET_EPISODE_SEARCH',payload={'plan_id':plan_id,'episode':episode,'mode':mode},
            event_id=event,created_by_job_id=parent_job,component='api')
        return {'job_id':job,'reused':False}


def find_episode(db,tvdb_episode_id):
    """Resolve an AniDB or TVDB episode ID uniquely, without release-name guesses."""
    if type(tvdb_episode_id) is not int or tvdb_episode_id<1:
        raise EpisodeResetUnavailable()
    rows=db.execute(
        'SELECT p.anidb_id,p.plan_id,s.anidb_episode_number FROM sonarr_episode_selections s '
        'JOIN trial_plans p USING(plan_id) WHERE s.tvdb_episode_id=? '
        'UNION SELECT p.anidb_id,p.plan_id,e.anidb_episode_number FROM recognized_episodes e '
        'JOIN trial_plans p ON p.anidb_id=e.anidb_id AND p.tvdb_series_id=e.tvdb_series_id '
        'WHERE e.tvdb_episode_id=? '
        'UNION SELECT p.anidb_id,p.plan_id,v.anidb_episode_number FROM release_verifications v '
        'JOIN trial_plans p ON p.anidb_id=v.anidb_id AND p.tvdb_series_id=v.tvdb_series_id '
        'WHERE v.tvdb_episode_id=? '
        'UNION SELECT p.anidb_id,p.plan_id,e.anidb_episode_number FROM recognized_episodes e '
        'JOIN trial_plans p ON p.anidb_id=e.anidb_id AND p.tvdb_series_id=e.tvdb_series_id '
        'WHERE e.anidb_episode_id=? '
        'UNION SELECT p.anidb_id,p.plan_id,v.anidb_episode_number FROM release_verifications v '
        'JOIN trial_plans p ON p.anidb_id=v.anidb_id AND p.tvdb_series_id=v.tvdb_series_id '
        'WHERE v.anidb_episode_id=?',
        (tvdb_episode_id,)*5
    ).fetchall()
    if len(rows)!=1:
        raise EpisodeResetUnavailable()
    aid,plan_id,episode=rows[0]
    _,_,season,number,*_=_scope(db,plan_id,episode,allow_staged=True)
    return {'anidb_id':aid,'plan_id':plan_id,'anidb_episode':episode,
            'tvdb_season':season,'tvdb_episode':number,
            'reservations':tuple(db.execute(
                'SELECT reservation_id,state,owner_job_id FROM episode_reservations '
                'WHERE anidb_id=? AND tvdb_season=? AND tvdb_episode=? '
                'UNION ALL SELECT reservation_id,state,owner_job_id FROM batch_episode_reservations '
                'WHERE anidb_id=? AND tvdb_season=? AND tvdb_episode=?',
                (aid,season,number,aid,season,number)
            ).fetchall()),
            'manual_candidates':db.execute(
                'SELECT count(*) FROM manual_release_submissions '
                'WHERE anidb_id=? AND tvdb_season=? AND tvdb_episode=?',
                (aid,season,number)
            ).fetchone()[0],
            'owners':_owners(db,plan_id,episode,aid,season,number)}
