"""Frozen backfill/continue plan snapshots, de-duplicated scheduling, and observations.

Never grants an episode reservation, media ownership, or torrent send permit.
Table and job names keep their historical "sonarr" prefix; the search source
is Prowlarr (acquisition.prowlarr_search).
"""
from dataclasses import dataclass
import sqlite3
import time

from src.nextgen.history import append_job_history, integer
from src.nextgen.storage.migrations import LATEST_VERSION, MigrationError, _open
from src.nextgen.storage.job_queue import QueueUnavailable
from src.nextgen.storage.sqlite import enqueue_job_with_receipt
from src.nextgen.acquisition.prowlarr_search import BackfillObservation, CandidateSelection

PAGE_SIZE = 4
MAX_EPISODE = 1000
STATUSES = ('IN_PROGRESS', 'NOT_AIRED', 'MAPPING_MISSING', 'CANDIDATES', 'NO_RESULTS')


class BackfillConflict(ValueError):
    reason_code = 'SONARR_BACKFILL_PLAN_CONFLICT'
    def __init__(self):
        super().__init__('Frozen backfill discovery facts need review.')


AUDIO_SEARCH_EVENT = 'TRIAL_AUDIO_SEARCH'


def window(anchor, backfill, following, include_anchor=False):
    """Episodes to search: backfill 1..X plus continue anchor..anchor+F-1, overlaps once.

    The anchor is normally excluded: its own delivery is already acquiring it,
    and its reservation may not exist yet when this search runs. include_anchor
    is for a delivery that was not downloaded (no Dual/Multi Audio tag).
    """
    numbers = set(range(1, backfill + 1))
    if following: numbers |= set(range(anchor, min(anchor + following, MAX_EPISODE + 1)))
    if include_anchor: numbers.add(anchor)
    else: numbers.discard(anchor)
    return tuple(n for n in sorted(numbers) if n <= MAX_EPISODE)


def anchor_searched(db, plan_id):
    """The plan's anchor delivery was skipped for audio, so search its episode."""
    return db.execute(
        "SELECT 1 FROM trial_plans p JOIN trial_evaluations t "
        "ON t.verification_id=p.anchor_verification_id AND t.plan_id=p.plan_id "
        "JOIN job_history h ON h.job_id=t.job_id "
        "WHERE p.plan_id=? AND h.event_code=? LIMIT 1",
        (plan_id, AUDIO_SEARCH_EVENT)).fetchone() is not None


def plan_window(db, plan_id, anchor, backfill, following):
    return window(anchor, backfill, following, anchor_searched(db, plan_id))


@dataclass(frozen=True)
class BackfillBatch:
    plan_id: int
    anidb_id: int
    tvdb_series_id: int
    season: int
    window_size: int
    start: int
    numbers: tuple[int, ...]
    reserved: tuple[int, ...]
    verified: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class BackfillCompletion:
    plan: BackfillBatch
    observations: tuple[BackfillObservation, ...]
    retry_episode: int | None = None


def _plan(db, plan_id, start, retry_episode=None):
    """start is a 1-based position in the plan's combined episode window."""
    integer(plan_id, positive=True)
    integer(start, positive=True)
    row = db.execute(
        "SELECT p.anidb_id,p.tvdb_series_id,p.tvdb_season,p.backfill_count,"
        "p.anchor_episode,p.following_count,v.anidb_id,v.tvdb_series_id,v.tvdb_season,"
        "v.tvdb_episode FROM trial_plans p JOIN release_verifications v ON "
        "v.verification_id=p.anchor_verification_id WHERE p.plan_id=? "
        "AND p.tvdb_order='official' AND v.tvdb_order='official'", (plan_id,)
    ).fetchone()
    if row is None or (row[0],row[1],row[2],row[4]) != tuple(row[6:]):
        raise BackfillConflict()
    aid, tid, season, backfill, anchor, following = row[:6]
    episodes = plan_window(db, plan_id, anchor, backfill, following)
    if not episodes or start > len(episodes) or (start - 1) % PAGE_SIZE:
        raise BackfillConflict()
    numbers = episodes[start-1:start-1+PAGE_SIZE]
    if retry_episode is not None:
        if retry_episode not in numbers: raise BackfillConflict()
        numbers = (retry_episode,)
    reserved = tuple(sorted(set(r[0] for r in db.execute(
        "SELECT tvdb_episode FROM episode_reservations WHERE "
        "tvdb_series_id=? AND tvdb_season=? AND state='ACTIVE' "
        "AND tvdb_episode BETWEEN ? AND ? UNION "
        "SELECT tvdb_episode FROM batch_episode_reservations WHERE "
        "tvdb_series_id=? AND tvdb_season=? AND state='ACTIVE' "
        "AND tvdb_episode BETWEEN ? AND ?",
        (tid, season, numbers[0], numbers[-1], tid, season, numbers[0], numbers[-1])
    )) & set(numbers)))
    known = tuple(row for row in db.execute(
        "SELECT tvdb_episode,tvdb_episode_id FROM recognized_episodes WHERE "
        "anidb_id=? AND tvdb_series_id=? AND tvdb_season=? AND "
        "tvdb_episode BETWEEN ? AND ? ORDER BY tvdb_episode",
        (aid, tid, season, numbers[0], numbers[-1])
    ).fetchall() if row[0] in numbers)
    return BackfillBatch(plan_id,aid,tid,season,len(episodes),start,numbers,reserved,known)


AUTOMATIC_RETRY_MS = 30 * 60 * 1000
SEARCH_FAILURES = ('PROWLARR_SEARCH_TIMEOUT', 'PROWLARR_UNAVAILABLE')


def automatic_retry_used(database, job_id):
    """Whether this job already had its one delayed retry since the last manual Retry."""
    db = None
    try:
        db = _open(database, readonly=True, timeout_ms=5000)
        return db.execute(
            "SELECT 1 FROM job_history WHERE job_id=? AND event_code='DEFERRED' "
            "AND reason_code IN (?,?) AND history_id>COALESCE((SELECT max(history_id) "
            "FROM job_history WHERE job_id=? AND event_code='RETRY_REQUESTED'),0) LIMIT 1",
            (job_id, *SEARCH_FAILURES, job_id)).fetchone() is not None
    except (sqlite3.Error, OSError, MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None: db.close()


def load_batch(database, plan_id, start, retry_episode=None):
    db = None
    try:
        db = _open(database,readonly=True,timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0] != LATEST_VERSION:
            raise QueueUnavailable()
        return _plan(db,plan_id,start,retry_episode)
    except (sqlite3.Error,OSError,MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None: db.close()


def schedule_first(db, claim, source, *, kind):
    """Exactly one first page per frozen plan, in the caller's completion TX."""
    if not db.in_transaction: raise ValueError('worker transaction required')
    row = db.execute(
        "SELECT p.plan_id,p.backfill_count,p.anchor_episode,p.following_count "
        "FROM trial_plans p WHERE "
        "p.anidb_id=? AND p.tvdb_series_id=? AND p.tvdb_season=? "
        "AND p.tvdb_order='official'",
        (source.anidb_id,source.tvdb_series_id,source.tvdb_season)
    ).fetchone()
    if row is None: raise BackfillConflict()
    plan_id,backfill,anchor,following = row
    # Nothing to search when the window is only the delivered anchor episode.
    if not plan_window(db,plan_id,anchor,backfill,following): return ()
    # Transaction is already a SQLite write transaction. Concurrent worker
    # completions serialize here; later deliveries cannot schedule a replay.
    exists = db.execute(
        "SELECT 1 FROM jobs WHERE kind=? AND "
        "json_extract(payload_json,'$.plan_id')=? LIMIT 1",(kind,plan_id)
    ).fetchone()
    if exists: return ()
    # Backfill is its own operation: the episode that triggered it must be able
    # to finish staging (operation STAGED) while backfill is still searching.
    _,receipt = enqueue_job_with_receipt(
        db,component='worker',kind=kind,payload={'plan_id':plan_id,'start':1},
        event_id=claim.event_id,created_by_job_id=claim.job_id
    )
    return (receipt,)



@dataclass(frozen=True)
class FallbackTarget:
    """Current failed selection and next stored, untried candidate."""
    identity: object
    failed_hash: str
    next_hash: str


@dataclass(frozen=True)
class FallbackCompletion:
    plan: BackfillBatch
    episode: int
    target: FallbackTarget
    selection: CandidateSelection


@dataclass(frozen=True)
class FallbackMissing:
    """The ranked candidate is no longer offered in a fresh Prowlarr search."""
    plan: BackfillBatch
    episode: int
    target: FallbackTarget


def persist_fallback_missing(db,claim,value):
    """Skip one withdrawn release and schedule the same existing worker."""
    from src.nextgen.storage.sonarr_candidates import (
        activate_candidate,fail_candidate,next_candidate,CandidateConflict)
    if (not db.in_transaction or claim.kind!='DISCOVER_SONARR_BACKFILL'
            or claim.event_id is None or type(value) is not FallbackMissing):
        raise BackfillConflict()
    plan,episode,target=value.plan,value.episode,value.target
    if (type(plan) is not BackfillBatch or type(target) is not FallbackTarget
            or episode not in plan.numbers):
        raise BackfillConflict()
    frozen=_plan(db,plan.plan_id,plan.start)
    if (frozen.anidb_id,frozen.tvdb_series_id,frozen.season,
            frozen.numbers)!=(plan.anidb_id,plan.tvdb_series_id,
                              plan.season,plan.numbers):
        raise BackfillConflict()
    row=db.execute(
        "SELECT p.tvdb_series_id,p.tvdb_season,s.tvdb_episode_id,s.info_hash "
        "FROM trial_plans p JOIN sonarr_episode_selections s USING(plan_id) "
        "WHERE p.plan_id=? AND s.tvdb_episode=?",
        (plan.plan_id,episode)).fetchone()
    if row!=(target.identity.tvdb_series_id,target.identity.tvdb_season,
             target.identity.tvdb_episode_id,target.failed_hash):
        raise BackfillConflict()
    prior=db.execute(
        "SELECT state FROM sonarr_episode_candidates WHERE plan_id=? "
        "AND tvdb_episode=? AND info_hash=?",
        (plan.plan_id,episode,target.failed_hash)).fetchone()
    pending=next_candidate(db,plan_id=plan.plan_id,episode=episode)
    if (prior!=('FAILED',) or pending is None
            or pending[1]!=target.next_hash):
        raise BackfillConflict()
    try:
        activate_candidate(db,plan_id=plan.plan_id,episode=episode,
                           info_hash=target.next_hash)
        fail_candidate(db,plan_id=plan.plan_id,episode=episode,
                       info_hash=target.next_hash,
                       reason_code='PROWLARR_RELEASE_WITHDRAWN')
    except CandidateConflict:raise BackfillConflict() from None
    following=next_candidate(db,plan_id=plan.plan_id,episode=episode)
    _,created=enqueue_job_with_receipt(
        db,component='worker',kind='DISCOVER_SONARR_BACKFILL',
        event_id=claim.event_id,created_by_job_id=claim.job_id,
        payload={'plan_id':plan.plan_id,'start':plan.start,
                 ('fallback_episode' if following else 'exhausted_episode'):episode})
    return (created,)


def load_fallback(database,plan_id,episode):
    """Read the next candidate without changing any selection or sending."""
    from src.nextgen.sonarr_episode import VerifiedEpisode
    from src.nextgen.storage.sonarr_candidates import next_candidate
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:
            raise QueueUnavailable()
        row=db.execute(
            "SELECT p.tvdb_series_id,p.tvdb_season,s.tvdb_episode_id,s.info_hash "
            "FROM trial_plans p JOIN sonarr_episode_selections s USING(plan_id) "
            "WHERE p.plan_id=? AND s.tvdb_episode=?",
            (plan_id,episode)).fetchone()
        if row is None:raise BackfillConflict()
        failed=db.execute(
            "SELECT state FROM sonarr_episode_candidates WHERE plan_id=? "
            "AND tvdb_episode=? AND info_hash=?",
            (plan_id,episode,row[3])).fetchone()
        pending=next_candidate(db,plan_id=plan_id,episode=episode)
        if failed!=('FAILED',) or pending is None:raise BackfillConflict()
        return FallbackTarget(
            VerifiedEpisode(row[0],row[2],row[1],episode),
            row[3],pending[1])
    except (sqlite3.Error,OSError,MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()


def persist_fallback(db,claim,value):
    """Replace a failed current choice only after operation cleanup is complete."""
    from src.nextgen.storage.sonarr_candidates import (
        CandidateConflict,activate_candidate,next_candidate)
    from src.nextgen.acquisition.prowlarr_search import _magnet_hash
    from src.nextgen.storage.sonarr_dispatch import dispatch as dispatch_source
    from src.nextgen.storage.sonarr_selection import SelectionConflict
    if (not db.in_transaction or type(value) is not FallbackCompletion
            or claim.kind!='DISCOVER_SONARR_BACKFILL'
            or claim.event_id is None):
        raise BackfillConflict()
    plan,episode,target,choice=value.plan,value.episode,value.target,value.selection
    frozen=_plan(db,plan.plan_id,plan.start) if type(plan) is BackfillBatch else None
    if (type(plan) is not BackfillBatch or episode not in plan.numbers
            or type(target) is not FallbackTarget
            or type(choice) is not CandidateSelection
            or choice.identity!=target.identity
            or choice.release.info_hash!=target.next_hash
            or _magnet_hash(choice.release.magnet_url)!=target.next_hash
            or not choice.policy.accepts(choice.release)
            or not any(x is choice.release for x in choice.result.candidates)
            or choice.release.tvdb_episode_id!=choice.result.tvdb_episode_id
            or choice.result.tvdb_episode_id!=target.identity.tvdb_episode_id
            or choice.identity.tvdb_series_id!=plan.tvdb_series_id
            or choice.identity.tvdb_season!=plan.season
            or choice.identity.tvdb_episode!=episode
            or (frozen.anidb_id,frozen.tvdb_series_id,frozen.season,
                frozen.window_size,frozen.numbers)!=(plan.anidb_id,
                plan.tvdb_series_id,plan.season,plan.window_size,plan.numbers)):
        raise BackfillConflict()
    current=db.execute(
        "SELECT s.info_hash,c.state FROM sonarr_episode_selections s "
        "JOIN sonarr_episode_candidates c ON c.plan_id=s.plan_id "
        "AND c.tvdb_episode=s.tvdb_episode AND c.info_hash=s.info_hash "
        "WHERE s.plan_id=? AND s.tvdb_episode=? AND s.tvdb_episode_id=?",
        (plan.plan_id,episode,target.identity.tvdb_episode_id)).fetchone()
    if current!=(target.failed_hash,'FAILED'):
        raise BackfillConflict()
    if db.execute(
        "SELECT 1 FROM sonarr_episode_candidates WHERE plan_id=? "
        "AND tvdb_episode=? AND state='ACTIVE'",(plan.plan_id,episode)).fetchone():
        raise BackfillConflict()
    pending=next_candidate(db,plan_id=plan.plan_id,episode=episode)
    if pending is None or pending[1]!=target.next_hash:
        raise BackfillConflict()
    # The canceled operation has already released its episode reservation.
    for table in ('episode_reservations','batch_episode_reservations'):
        if db.execute(
            f"SELECT 1 FROM {table} WHERE state='ACTIVE' "
            "AND tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=? LIMIT 1",
            (plan.tvdb_series_id,plan.season,episode)).fetchone():
            raise BackfillConflict()
    updated=db.execute(
        "UPDATE sonarr_episode_selections SET info_hash=?,selected_at_ms=? "
        "WHERE plan_id=? AND tvdb_episode=? AND info_hash=?",
        (target.next_hash,integer(time.time_ns()//1_000_000),
         plan.plan_id,episode,target.failed_hash))
    if updated.rowcount!=1:raise BackfillConflict()
    try:
        activate_candidate(db,plan_id=plan.plan_id,episode=episode,
                           info_hash=target.next_hash)
        routed=dispatch_source(db,claim,plan,choice)
    except (CandidateConflict,SelectionConflict):raise BackfillConflict() from None
    receipt=append_job_history(db,job_id=claim.job_id,
        event_code='SONARR_BACKFILL_OBSERVED',component='worker',
        occurred_at_ms=integer(time.time_ns()//1_000_000),
        details={'attempt':claim.attempt})
    return (receipt,*routed)


def persist_observations(db, claim, value, *, dispatch=False):
    if type(dispatch) is not bool: raise ValueError('invalid dispatch mode')
    if not db.in_transaction or type(value) is not BackfillCompletion:
        raise ValueError('worker transaction and typed results required')
    plan = value.plan
    if len(value.observations) != len(plan.numbers):
        raise BackfillConflict()
    # Recheck immutable scope. Reservation state is intentionally not re-queried
    # here: it may change after a read-only search; this is observation, not
    # authorization.
    current = _plan(db,plan.plan_id,plan.start,value.retry_episode)
    if (current.plan_id,current.anidb_id,current.tvdb_series_id,
        current.season,current.window_size,current.numbers) != (
        plan.plan_id,plan.anidb_id,plan.tvdb_series_id,plan.season,
        plan.window_size,plan.numbers):
        raise BackfillConflict()
    if claim.event_id is None: raise BackfillConflict()
    for number,item in zip(plan.numbers,value.observations):
        if (type(item) is not BackfillObservation or item.number != number
                or item.status not in STATUSES
                or not 0 <= item.accepted <= item.candidates <= 1000
                or item.status != 'CANDIDATES' and (item.candidates or item.accepted)):
            raise BackfillConflict()
        previous = db.execute(
            'SELECT status FROM sonarr_backfill_results WHERE plan_id=? AND tvdb_episode=?',
            (plan.plan_id,number)
        ).fetchone()
        if previous and (value.retry_episode != number or
                         previous[0] not in ('NO_RESULTS','NOT_AIRED','MAPPING_MISSING')):
            raise BackfillConflict()
    now = integer(time.time_ns() // 1000000)
    # Selection is an opt-in side effect of the same fenced worker completion.
    # Keep source URLs out of SQLite and reject stale/unverified identity facts.
    from src.nextgen.storage.sonarr_selection import select, SelectionConflict
    from src.nextgen.storage.sonarr_candidates import (save_candidates, activate_candidate,
                                                        CandidateConflict)
    routed=[]
    for item in value.observations:
        choice = item.selection
        if choice is not None:
            if (type(choice) is not CandidateSelection or item.status != 'CANDIDATES'
                    or item.tvdb_episode_id != choice.identity.tvdb_episode_id
                    or item.number != choice.identity.tvdb_episode
                    or plan.tvdb_series_id != choice.identity.tvdb_series_id
                    or plan.season != choice.identity.tvdb_season
                    or dict(plan.verified).get(item.number) not in (None,item.tvdb_episode_id)):
                raise BackfillConflict()
            try:
                save_candidates(db,plan_id=plan.plan_id,episode=item.number,
                                result=choice.result,policy=choice.policy)
                inserted=select(db, plan_id=plan.plan_id, identity=choice.identity,
                       result=choice.result, release=choice.release, policy=choice.policy)
                if inserted and dispatch:
                    activate_candidate(db,plan_id=plan.plan_id,episode=item.number,
                                       info_hash=choice.release.info_hash)
                    from src.nextgen.storage.sonarr_dispatch import dispatch as dispatch_source
                    routed.extend(dispatch_source(db,claim,plan,choice))
            except (SelectionConflict,CandidateConflict):
                raise BackfillConflict() from None
        db.execute(
            "INSERT INTO sonarr_backfill_results("
            "plan_id,tvdb_episode,job_id,sonarr_episode_id,tvdb_episode_id,status,"
            "candidates,approved,exact,observed_at_ms) VALUES(?,?,?,NULL,?,?,?,?,?,?) "
            "ON CONFLICT(plan_id,tvdb_episode) DO UPDATE SET "
            "job_id=excluded.job_id,tvdb_episode_id=excluded.tvdb_episode_id,"
            "status=excluded.status,candidates=excluded.candidates,"
            "approved=excluded.approved,exact=excluded.exact,"
            "observed_at_ms=excluded.observed_at_ms",
            (plan.plan_id,item.number,claim.job_id,item.tvdb_episode_id,item.status,
             item.candidates,item.accepted,item.accepted,now)
        )
    return (append_job_history(db,job_id=claim.job_id,
        event_code='SONARR_BACKFILL_OBSERVED',component='worker',occurred_at_ms=now,
        details={'attempt':claim.attempt}),*routed)


# --- Waiting for dub -------------------------------------------------------
# English audio is required, every ranked release of an episode failed, and at
# least one failed the audio check. The exhausted job goes ON_HOLD with this
# reason instead of NEEDS_REVIEW; other failures (stalled torrents, conflicts)
# are just releases that did not work. Retry ("Search again") searches Prowlarr
# once more and appends only new Dual/Multi Audio releases; the existing
# fallback job then tries them. No new table: the held job is the status.
WAITING_FOR_DUB = 'WAITING_FOR_DUB'


@dataclass(frozen=True)
class DubSearch:
    identity: object
    tried: frozenset


@dataclass(frozen=True)
class DubFound:
    plan: BackfillBatch
    episode: int
    target: DubSearch
    hashes: tuple


def _audio_only(db, plan_id, episode):
    """Hashes tried for this episode when all failed and one failed on audio."""
    rows = db.execute(
        'SELECT info_hash,state,reason_code FROM sonarr_episode_candidates '
        'WHERE plan_id=? AND tvdb_episode=?', (plan_id, episode)).fetchall()
    if (not rows or any(state != 'FAILED' for _, state, _ in rows)
            or not any(reason == 'MEDIA_AUDIO_REQUIRED' for *_, reason in rows)):
        return None
    return frozenset(row[0] for row in rows)


def audio_exhausted(db, plan_id, episode):
    return _audio_only(db, plan_id, episode) is not None


def load_dub_search(database, plan_id, episode):
    """The episode identity and tried hashes, or None if not an audio-only exhaustion."""
    from src.nextgen.sonarr_episode import VerifiedEpisode
    db = None
    try:
        db = _open(database, readonly=True, timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0] != LATEST_VERSION:
            raise QueueUnavailable()
        tried = _audio_only(db, plan_id, episode)
        if tried is None: return None
        row = db.execute(
            "SELECT p.tvdb_series_id,p.tvdb_season,s.tvdb_episode_id "
            "FROM trial_plans p JOIN sonarr_episode_selections s USING(plan_id) "
            "WHERE p.plan_id=? AND s.tvdb_episode=?", (plan_id, episode)).fetchone()
        if row is None: raise BackfillConflict()
        return DubSearch(VerifiedEpisode(row[0], row[2], row[1], episode), tried)
    except (sqlite3.Error, OSError, MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None: db.close()


def persist_dub_found(db, claim, value):
    """Append new tagged releases after the tried ones and hand off to fallback."""
    if (not db.in_transaction or type(value) is not DubFound
            or claim.kind != 'DISCOVER_SONARR_BACKFILL' or claim.event_id is None
            or type(value.plan) is not BackfillBatch or value.episode not in value.plan.numbers
            or not value.hashes or len(set(value.hashes)) != len(value.hashes)):
        raise BackfillConflict()
    plan, episode = value.plan, value.episode
    if _audio_only(db, plan.plan_id, episode) != value.target.tried or set(value.hashes) & value.target.tried:
        raise BackfillConflict()
    last = db.execute('SELECT max(rank) FROM sonarr_episode_candidates WHERE plan_id=? '
                      'AND tvdb_episode=?', (plan.plan_id, episode)).fetchone()[0]
    for rank, digest in enumerate(value.hashes, last + 1):
        db.execute('INSERT INTO sonarr_episode_candidates(plan_id,tvdb_episode,rank,info_hash) '
                   'VALUES(?,?,?,?)', (plan.plan_id, episode, rank, digest))
    _, created = enqueue_job_with_receipt(
        db, component='worker', kind='DISCOVER_SONARR_BACKFILL',
        event_id=claim.event_id, created_by_job_id=claim.job_id,
        payload={'plan_id': plan.plan_id, 'start': plan.start, 'fallback_episode': episode})
    return (created,)
