"""Frozen backfill/continue plan snapshots, de-duplicated scheduling, and observations.

Never grants an episode reservation, media ownership, or torrent send permit.
Episodes are searched with Prowlarr (acquisition.prowlarr_search); results are
in episode_discovery_results and jobs of kind DISCOVER_EPISODE_RELEASES.
"""
from dataclasses import dataclass
import sqlite3
import time

from src.anime.history import append_job_history, integer
from src.anime.storage.migrations import LATEST_VERSION, MigrationError, _open
from src.anime.storage.job_queue import QueueUnavailable
from src.anime.storage.sqlite import enqueue_job_with_receipt
from src.anime.acquisition.prowlarr_search import BackfillObservation, CandidateSelection

PAGE_SIZE = 4
STATUSES = ('IN_PROGRESS', 'NOT_AIRED', 'MAPPING_MISSING', 'CANDIDATES', 'NO_RESULTS',
            'WAITING_FOR_DUB')


class BackfillConflict(ValueError):
    reason_code = 'EPISODE_DISCOVERY_PLAN_CONFLICT'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'Frozen backfill discovery facts need review.')
        self.failed_check = failed_check
        self.failed_path = failed_path


AUDIO_SEARCH_EVENT = 'USER_EVALUATION_AUDIO_SEARCH'


def window(anchor, backfill, following, include_anchor=False):
    """AniDB episodes to search: the plan's selection window (trial_policy.Window).

    The anchor is normally excluded: its own delivery is already acquiring it,
    and its reservation may not exist yet when this search runs. include_anchor
    is for a delivery that was not downloaded (no Dual/Multi Audio tag).
    """
    from src.anime.user_evaluation_policy import Window
    numbers = set(Window(backfill, following).anidb_episodes(anchor))
    if include_anchor: numbers.add(anchor)
    else: numbers.discard(anchor)
    return tuple(sorted(numbers))


def anchor_searched(db, plan_id):
    """The plan's anchor delivery was skipped for audio, so search its episode."""
    return db.execute(
        "SELECT 1 FROM user_evaluation_plans p JOIN user_evaluation_checks t "
        "ON t.verification_id=p.anchor_verification_id AND t.plan_id=p.plan_id "
        "JOIN job_history h ON h.job_id=t.job_id "
        "WHERE p.plan_id=? AND h.event_code=? LIMIT 1",
        (plan_id, AUDIO_SEARCH_EVENT)).fetchone() is not None


def plan_window(db, plan_id, anchor, backfill, following):
    computed = window(anchor, backfill, following, anchor_searched(db, plan_id))
    try:
        stored = tuple(r[0] for r in db.execute(
            "SELECT anidb_episode_number FROM episode_discovery_results "
            "WHERE plan_id=? ORDER BY anidb_episode_number", (plan_id,)).fetchall())
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        stored = ()
    if stored and not set(stored).issubset(set(computed)):
        return stored
    return computed


@dataclass(frozen=True)
class BackfillBatch:
    """One page of a plan's AniDB window. numbers are AniDB episode numbers."""
    plan_id: int
    anidb_id: int
    tvdb_series_id: int
    window_size: int
    start: int
    numbers: tuple[int, ...]
    reserved: tuple[int, ...]
    verified: tuple[tuple[int, int], ...]
    # Active single/batch reservations of this series, as TVDB (season, episode).
    reserved_placements: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class BackfillCompletion:
    plan: BackfillBatch
    observations: tuple[BackfillObservation, ...]
    retry_episode: int | None = None


def _plan(db, plan_id, start, retry_episode=None):
    """start is a 1-based position in the plan's combined AniDB episode window."""
    integer(plan_id, positive=True)
    integer(start, positive=True)
    row = db.execute(
        "SELECT p.anidb_id,p.tvdb_series_id,p.backfill_count,"
        "p.anchor_episode,p.following_count,v.anidb_id,v.tvdb_series_id,"
        "v.anidb_episode_number FROM user_evaluation_plans p JOIN release_verifications v ON "
        "v.verification_id=p.anchor_verification_id WHERE p.plan_id=? "
        "AND p.tvdb_order='official' AND v.tvdb_order='official'", (plan_id,)
    ).fetchone()
    if row is None or (row[0],row[1],row[3]) != tuple(row[5:]):
        raise BackfillConflict()
    aid, tid, backfill, anchor, following = row[:5]
    episodes = plan_window(db, plan_id, anchor, backfill, following)
    # Explicit single-episode recovery may include the original delivered
    # anchor, even when automatic backfill correctly excluded it.
    if retry_episode == anchor:
        episodes = tuple(sorted(set(episodes) | {anchor}))
    if not episodes or start > len(episodes) or (start - 1) % PAGE_SIZE:
        raise BackfillConflict()
    numbers = episodes[start-1:start-1+PAGE_SIZE]
    if retry_episode is not None:
        if retry_episode not in numbers: raise BackfillConflict()
        numbers = (retry_episode,)
    marks = ','.join('?' for _ in numbers)
    # Active reservations block by AniDB identity (both via release verifications
    # and recognized episodes) and by TVDB placement for every target (Client.backfill).
    reserved = tuple(sorted({r[0] for r in db.execute(
        "SELECT v.anidb_episode_number FROM episode_reservations r "
        "JOIN release_verifications v USING(verification_id) "
        f"WHERE r.state='ACTIVE' AND r.anidb_id=? AND v.anidb_episode_number IN ({marks}) UNION "
        "SELECT e.anidb_episode_number FROM recognized_episodes e WHERE e.anidb_id=? "
        f"AND e.anidb_episode_number IN ({marks}) AND EXISTS ("
        "SELECT 1 FROM episode_reservations r WHERE r.state='ACTIVE' "
        "AND r.anidb_episode_id=e.anidb_episode_id UNION ALL "
        "SELECT 1 FROM batch_episode_reservations r WHERE r.state='ACTIVE' "
        "AND r.anidb_episode_id=e.anidb_episode_id)", (aid, *numbers, aid, *numbers))}))
    placements = tuple(sorted(set(db.execute(
        "SELECT tvdb_season,tvdb_episode FROM episode_reservations WHERE "
        "tvdb_series_id=? AND state='ACTIVE' UNION "
        "SELECT tvdb_season,tvdb_episode FROM batch_episode_reservations WHERE "
        "tvdb_series_id=? AND state='ACTIVE'", (tid, tid)).fetchall())))
    known = tuple(db.execute(
        "SELECT anidb_episode_number,tvdb_episode_id FROM recognized_episodes WHERE "
        f"anidb_id=? AND tvdb_series_id=? AND anidb_episode_number IN ({marks}) "
        "ORDER BY anidb_episode_number", (aid, tid, *numbers)).fetchall())
    return BackfillBatch(plan_id,aid,tid,len(episodes),start,numbers,reserved,known,placements)


AUTOMATIC_RETRY_MS = 30 * 60 * 1000
# Prowlarr timeouts get one delayed retry; an outage is a service wait (SERVICE_UNAVAILABLE).
SEARCH_FAILURES = ('RELEASE_SEARCH_TIMEOUT',)


def automatic_retry_used(database, job_id):
    """Whether this job already had its one delayed retry since the last manual Retry."""
    db = None
    try:
        db = _open(database, readonly=True, timeout_ms=5000)
        return db.execute(
            "SELECT 1 FROM job_history WHERE job_id=? AND event_code='DEFERRED' "
            "AND reason_code IN (" + ",".join("?" * len(SEARCH_FAILURES)) + ") AND history_id>COALESCE((SELECT max(history_id) "
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
        "FROM user_evaluation_plans p WHERE p.anidb_id=? AND p.tvdb_series_id=? "
        "AND p.tvdb_order='official'",
        (source.anidb_id,source.tvdb_series_id)
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
    from src.anime.storage.release_candidates import (
        activate_candidate,fail_candidate,next_candidate,CandidateConflict)
    if (not db.in_transaction or claim.kind!='DISCOVER_EPISODE_RELEASES'
            or claim.event_id is None or type(value) is not FallbackMissing):
        raise BackfillConflict()
    plan,episode,target=value.plan,value.episode,value.target
    if (type(plan) is not BackfillBatch or type(target) is not FallbackTarget
            or episode not in plan.numbers):
        raise BackfillConflict()
    frozen=_plan(db,plan.plan_id,plan.start)
    if (frozen.anidb_id,frozen.tvdb_series_id,
            frozen.numbers)!=(plan.anidb_id,plan.tvdb_series_id,plan.numbers):
        raise BackfillConflict()
    if _selected(db,plan.plan_id,episode)!=(target.identity,target.failed_hash):
        raise BackfillConflict()
    prior=db.execute(
        "SELECT state FROM episode_release_candidates WHERE plan_id=? "
        "AND anidb_episode_number=? AND info_hash=?",
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
                       reason_code='RELEASE_WITHDRAWN')
    except CandidateConflict:raise BackfillConflict() from None
    following=next_candidate(db,plan_id=plan.plan_id,episode=episode)
    _,created=enqueue_job_with_receipt(
        db,component='worker',kind='DISCOVER_EPISODE_RELEASES',
        event_id=claim.event_id,created_by_job_id=claim.job_id,
        payload={'plan_id':plan.plan_id,'start':plan.start,
                 ('fallback_episode' if following else 'exhausted_episode'):episode})
    return (created,)


def _selected(db,plan_id,episode):
    """(EpisodeSearchContext or VerifiedEpisode at its own TVDB placement, hash) of a selected AniDB episode."""
    from src.anime.acquisition.episode_context import (
        EpisodeContextMissing, load_authoritative_episode_context,
    )
    from src.anime.episode_identity import VerifiedEpisode
    row=db.execute(
        "SELECT p.tvdb_series_id,s.tvdb_episode_id,s.tvdb_season,s.tvdb_episode,s.info_hash "
        "FROM user_evaluation_plans p JOIN episode_release_selections s USING(plan_id) "
        "WHERE p.plan_id=? AND s.anidb_episode_number=?",(plan_id,episode)).fetchone()
    if row is None:
        return None
    # Legacy pre-migration schemas, including historical lightweight adapters,
    # lack the authoritative AniDB plan ID entirely. Detect that structural
    # absence explicitly; do not turn arbitrary SQLite query failures into
    # missing episode evidence.
    if db.execute('PRAGMA user_version').fetchone()[0] == 0:
        columns = {col[1] for col in db.execute('PRAGMA table_info(user_evaluation_plans)').fetchall()}
        if 'anidb_id' not in columns:
            return (VerifiedEpisode(*row[:4]), row[4])
    try:
        ctx = load_authoritative_episode_context(db, plan_id, episode)
    except EpisodeContextMissing:
        # Only true legacy absence permits provider-backed resolution.
        return (VerifiedEpisode(*row[:4]), row[4])
    return (ctx, row[4])


def load_fallback(database,plan_id,episode):
    """Read the next candidate without changing any selection or sending."""
    from src.anime.episode_identity import VerifiedEpisode
    from src.anime.storage.release_candidates import next_candidate
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:
            raise QueueUnavailable()
        selected=_selected(db,plan_id,episode)
        if selected is None:raise BackfillConflict()
        identity,digest=selected
        failed=db.execute(
            "SELECT state FROM episode_release_candidates WHERE plan_id=? "
            "AND anidb_episode_number=? AND info_hash=?",
            (plan_id,episode,digest)).fetchone()
        pending=next_candidate(db,plan_id=plan_id,episode=episode)
        if failed!=('FAILED',) or pending is None:raise BackfillConflict()
        return FallbackTarget(identity,digest,pending[1])
    except (sqlite3.Error,OSError,MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()


def persist_fallback(db,claim,value):
    """Replace a failed current choice only after operation cleanup is complete."""
    from src.anime.storage.release_candidates import (
        CandidateConflict,activate_candidate,next_candidate)
    from src.anime.acquisition.prowlarr_search import _magnet_hash
    from src.anime.storage.release_dispatch import dispatch as dispatch_source
    from src.anime.storage.release_selection import SelectionConflict
    if (not db.in_transaction or type(value) is not FallbackCompletion
            or claim.kind!='DISCOVER_EPISODE_RELEASES'
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
            or _selected(db,plan.plan_id,episode)!=(choice.identity,target.failed_hash)
            or (frozen.anidb_id,frozen.tvdb_series_id,
                frozen.window_size,frozen.numbers)!=(plan.anidb_id,
                plan.tvdb_series_id,plan.window_size,plan.numbers)):
        raise BackfillConflict()
    current=db.execute(
        "SELECT s.info_hash,c.state FROM episode_release_selections s "
        "JOIN episode_release_candidates c ON c.plan_id=s.plan_id "
        "AND c.anidb_episode_number=s.anidb_episode_number AND c.info_hash=s.info_hash "
        "WHERE s.plan_id=? AND s.anidb_episode_number=? AND s.tvdb_episode_id=?",
        (plan.plan_id,episode,target.identity.tvdb_episode_id)).fetchone()
    if current!=(target.failed_hash,'FAILED'):
        raise BackfillConflict()
    if db.execute(
        "SELECT 1 FROM episode_release_candidates WHERE plan_id=? "
        "AND anidb_episode_number=? AND state='ACTIVE'",(plan.plan_id,episode)).fetchone():
        raise BackfillConflict()
    pending=next_candidate(db,plan_id=plan.plan_id,episode=episode)
    if pending is None or pending[1]!=target.next_hash:
        raise BackfillConflict()
    # The canceled operation has already released its episode reservation.
    for table in ('episode_reservations','batch_episode_reservations'):
        if db.execute(
            f"SELECT 1 FROM {table} WHERE state='ACTIVE' "
            "AND tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=? LIMIT 1",
            (plan.tvdb_series_id,target.identity.tvdb_season,
             target.identity.tvdb_episode)).fetchone():
            raise BackfillConflict()
    updated=db.execute(
        "UPDATE episode_release_selections SET info_hash=?,selected_at_ms=? "
        "WHERE plan_id=? AND anidb_episode_number=? AND info_hash=?",
        (target.next_hash,integer(time.time_ns()//1_000_000),
         plan.plan_id,episode,target.failed_hash))
    if updated.rowcount!=1:raise BackfillConflict()
    try:
        activate_candidate(db,plan_id=plan.plan_id,episode=episode,
                           info_hash=target.next_hash)
        routed=dispatch_source(db,claim,plan,choice)
    except (CandidateConflict,SelectionConflict):raise BackfillConflict() from None
    receipt=append_job_history(db,job_id=claim.job_id,
        event_code='EPISODE_DISCOVERY_RECORDED',component='worker',
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
        current.window_size,current.numbers) != (
        plan.plan_id,plan.anidb_id,plan.tvdb_series_id,
        plan.window_size,plan.numbers):
        raise BackfillConflict()
    if claim.event_id is None: raise BackfillConflict()
    for number,item in zip(plan.numbers,value.observations):
        if (type(item) is not BackfillObservation or item.number != number
                or item.status not in STATUSES
                or not 0 <= item.accepted <= item.candidates <= 1000
                or item.status == WAITING_FOR_DUB and (item.accepted or item.selection is not None)
                or item.status not in ('CANDIDATES', WAITING_FOR_DUB)
                   and (item.candidates or item.accepted)):
            raise BackfillConflict()
        previous = db.execute(
            'SELECT status FROM episode_discovery_results WHERE plan_id=? AND anidb_episode_number=?',
            (plan.plan_id,number)
        ).fetchone()
        if previous and (value.retry_episode != number or
                         previous[0] not in ('NO_RESULTS','NOT_AIRED','MAPPING_MISSING',
                                             WAITING_FOR_DUB)):
            raise BackfillConflict()
    now = integer(time.time_ns() // 1000000)
    # Selection is an opt-in side effect of the same fenced worker completion.
    # Keep source URLs out of SQLite and reject stale/unverified identity facts.
    from src.anime.storage.release_selection import select, SelectionConflict
    from src.anime.storage.release_candidates import (save_candidates, activate_candidate,
                                                        CandidateConflict)
    routed=[]
    for item in value.observations:
        choice = item.selection
        if choice is not None:
            if (type(choice) is not CandidateSelection or item.status != 'CANDIDATES'
                    or item.tvdb_episode_id != choice.identity.tvdb_episode_id
                    or plan.tvdb_series_id != choice.identity.tvdb_series_id
                    or dict(plan.verified).get(item.number) not in (None,item.tvdb_episode_id)):
                raise BackfillConflict()
            try:
                save_candidates(db,plan_id=plan.plan_id,episode=item.number,
                                result=choice.result,policy=choice.policy)
                inserted=select(db, plan_id=plan.plan_id, episode=item.number, identity=choice.identity,
                       result=choice.result, release=choice.release, policy=choice.policy)
                if inserted and dispatch:
                    activate_candidate(db,plan_id=plan.plan_id,episode=item.number,
                                       info_hash=choice.release.info_hash)
                    from src.anime.storage.release_dispatch import dispatch as dispatch_source
                    routed.extend(dispatch_source(db,claim,plan,choice))
            except (SelectionConflict,CandidateConflict):
                raise BackfillConflict() from None
        db.execute(
            "INSERT INTO episode_discovery_results("
            "plan_id,anidb_episode_number,job_id,sonarr_episode_id,tvdb_episode_id,status,"
            "candidates,approved,exact,observed_at_ms) VALUES(?,?,?,NULL,?,?,?,?,?,?) "
            "ON CONFLICT(plan_id,anidb_episode_number) DO UPDATE SET "
            "job_id=excluded.job_id,tvdb_episode_id=excluded.tvdb_episode_id,"
            "status=excluded.status,candidates=excluded.candidates,"
            "approved=excluded.approved,exact=excluded.exact,"
            "observed_at_ms=excluded.observed_at_ms",
            (plan.plan_id,item.number,claim.job_id,item.tvdb_episode_id,item.status,
             item.candidates,item.accepted,item.accepted,now)
        )
    return (append_job_history(db,job_id=claim.job_id,
        event_code='EPISODE_DISCOVERY_RECORDED',component='worker',occurred_at_ms=now,
        details={'attempt':claim.attempt}),*routed)


# --- Waiting for dub -------------------------------------------------------
# Unavailable Dual/Multi Audio is recorded as a per-episode search result.
# Separately, when every attempted release fails and at least one failed
# the audio probe, the exhausted job goes ON_HOLD with this
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


@dataclass(frozen=True)
class DubExhausted:
    plan: BackfillBatch
    episode: int


@dataclass(frozen=True)
class CandidatesExhausted:
    plan: BackfillBatch
    episode: int


def _audio_only(db, plan_id, episode):
    """Hashes tried for this episode when all candidates are failed."""
    rows = db.execute(
        'SELECT info_hash,state,reason_code FROM episode_release_candidates '
        'WHERE plan_id=? AND anidb_episode_number=?', (plan_id, episode)).fetchall()
    if not rows or any(state != 'FAILED' for _, state, _ in rows):
        return None
    return frozenset(row[0] for row in rows)


def audio_exhausted(db, plan_id, episode):
    rows = db.execute(
        'SELECT info_hash,state,reason_code FROM episode_release_candidates '
        'WHERE plan_id=? AND anidb_episode_number=?', (plan_id, episode)).fetchall()
    return bool(rows and all(state == 'FAILED' for _, state, _ in rows)
                and any(reason == 'MEDIA_AUDIO_REQUIRED' for *_, reason in rows))


def load_tried_hashes(database, plan_id, numbers):
    """(AniDB episode -> frozenset of tried hashes) for episodes in numbers."""
    if not numbers: return {}
    db = None
    try:
        db = _open(database, readonly=True, timeout_ms=5000)
        placeholders = ','.join('?' for _ in numbers)
        rows = db.execute(
            f"SELECT anidb_episode_number, info_hash FROM episode_release_candidates "
            f"WHERE plan_id=? AND anidb_episode_number IN ({placeholders})",
            (plan_id, *numbers)
        ).fetchall()
        result = {}
        for ep, h in rows:
            if h:
                result.setdefault(ep, set()).add(h.lower())
        return {ep: frozenset(hs) for ep, hs in result.items()}
    except (sqlite3.Error, OSError, MigrationError):
        return {}
    finally:
        if db is not None: db.close()


def load_dub_search(database, plan_id, episode):
    """The episode identity and tried hashes, or None if not an audio-only exhaustion."""
    from src.anime.episode_identity import VerifiedEpisode
    db = None
    try:
        db = _open(database, readonly=True, timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0] != LATEST_VERSION:
            raise QueueUnavailable()
        tried = _audio_only(db, plan_id, episode)
        if tried is None: return None
        selected = _selected(db, plan_id, episode)
        if selected is None: raise BackfillConflict()
        return DubSearch(selected[0], tried)
    except (sqlite3.Error, OSError, MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None: db.close()


def persist_dub_found(db, claim, value):
    """Append new tagged releases after the tried ones and hand off to fallback."""
    if (not db.in_transaction or type(value) is not DubFound
            or claim.kind != 'DISCOVER_EPISODE_RELEASES' or claim.event_id is None
            or type(value.plan) is not BackfillBatch or value.episode not in value.plan.numbers
            or not value.hashes or len(set(value.hashes)) != len(value.hashes)):
        raise BackfillConflict()
    plan, episode = value.plan, value.episode
    if _audio_only(db, plan.plan_id, episode) != value.target.tried or set(value.hashes) & value.target.tried:
        raise BackfillConflict()
    last = db.execute('SELECT max(rank) FROM episode_release_candidates WHERE plan_id=? '
                      'AND anidb_episode_number=?', (plan.plan_id, episode)).fetchone()[0]
    for rank, digest in enumerate(value.hashes, last + 1):
        db.execute('INSERT INTO episode_release_candidates(plan_id,anidb_episode_number,rank,info_hash) '
                   'VALUES(?,?,?,?)', (plan.plan_id, episode, rank, digest))
    _, created = enqueue_job_with_receipt(
        db, component='worker', kind='DISCOVER_EPISODE_RELEASES',
        event_id=claim.event_id, created_by_job_id=claim.job_id,
        payload={'plan_id': plan.plan_id, 'start': plan.start, 'fallback_episode': episode})
    return (created,)


def persist_dub_exhausted(db, claim, value):
    """Mark the episode as WAITING_FOR_DUB after candidate exhaustion."""
    if (not db.in_transaction or type(value) is not DubExhausted
            or claim.kind != 'DISCOVER_EPISODE_RELEASES' or claim.event_id is None
            or type(value.plan) is not BackfillBatch or value.episode not in value.plan.numbers):
        raise BackfillConflict()
    plan, episode = value.plan, value.episode
    now = int(time.time() * 1000)
    db.execute(
        "INSERT INTO episode_discovery_results("
        "plan_id,anidb_episode_number,job_id,sonarr_episode_id,tvdb_episode_id,status,"
        "candidates,approved,exact,observed_at_ms) VALUES(?,?,?,NULL,NULL,'WAITING_FOR_DUB',0,0,0,?) "
        "ON CONFLICT(plan_id,anidb_episode_number) DO UPDATE SET "
        "status='WAITING_FOR_DUB',observed_at_ms=excluded.observed_at_ms",
        (plan.plan_id, episode, claim.job_id, now)
    )
    return ()


def persist_candidates_exhausted(db, claim, value):
    """Mark the episode as NO_RESULTS after candidate exhaustion."""
    if (not db.in_transaction or type(value) is not CandidatesExhausted
            or claim.kind != 'DISCOVER_EPISODE_RELEASES' or claim.event_id is None
            or type(value.plan) is not BackfillBatch or value.episode not in value.plan.numbers):
        raise BackfillConflict()
    plan, episode = value.plan, value.episode
    now = int(time.time() * 1000)
    db.execute(
        "INSERT INTO episode_discovery_results("
        "plan_id,anidb_episode_number,job_id,sonarr_episode_id,tvdb_episode_id,status,"
        "candidates,approved,exact,observed_at_ms) VALUES(?,?,?,NULL,NULL,'NO_RESULTS',0,0,0,?) "
        "ON CONFLICT(plan_id,anidb_episode_number) DO UPDATE SET "
        "status='NO_RESULTS',observed_at_ms=excluded.observed_at_ms",
        (plan.plan_id, episode, claim.job_id, now)
    )
    return ()
