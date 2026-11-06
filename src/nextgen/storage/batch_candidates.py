"""Durable series-scoped batch candidate pool and controller (WI-01, WI-02, WI-04).

Maintains one active batch-acquisition operation per (anidb_id, season) scope,
with a durable, ranked list of candidate releases.
"""
from dataclasses import dataclass, field
import json
import sqlite3
import time

from src.nextgen.acquisition.audio import dual_audio_tagged, wants_english, is_audio_incompatible
from src.nextgen.acquisition.metadata import infohash
from src.nextgen.acquisition.prowlarr_search import resolution
from src.nextgen.batch.recognition import release_scope_hint
from src.nextgen.history import integer
from src.nextgen.providers.identity import delivery_title
from src.nextgen.release_contracts import ReleaseInput


class CandidateConflict(ValueError):
    reason_code = 'BATCH_SELECTION_CONFLICT'


@dataclass(frozen=True)
class BatchScope:
    scope_id: int
    anidb_id: int
    tvdb_series_id: int
    tvdb_season: int
    operation_id: int | None
    active_candidate_id: int | None
    controller_job_id: int | None
    state: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class BatchCandidate:
    candidate_id: int
    scope_id: int
    info_hash: str
    title: str
    rank: int
    state: str
    reason_code: str | None = None
    has_metadata: int = 0
    metadata_json: str | None = None
    seeders: int | None = None
    size_bytes: int | None = None
    source_event_id: int | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0


def _now(clock=None):
    return time.time_ns() // 1000000 if clock is None else clock.now()


def get_or_create_scope(db, anidb_id, tvdb_series_id, season, *, now_ms=None):
    """Retrieve or insert the single durable scope for an identified anime season."""
    integer(anidb_id, positive=True)
    integer(tvdb_series_id, positive=True)
    integer(season, positive=True)
    now = now_ms if now_ms is not None else _now()
    row = db.execute(
        "SELECT scope_id, anidb_id, tvdb_series_id, tvdb_season, operation_id, "
        "active_candidate_id, controller_job_id, state, created_at_ms, updated_at_ms "
        "FROM batch_candidate_scopes WHERE anidb_id=? AND tvdb_season=?",
        (anidb_id, season)).fetchone()
    if row is not None:
        return BatchScope(*row)
    db.execute(
        "INSERT INTO batch_candidate_scopes("
        "anidb_id, tvdb_series_id, tvdb_season, state, created_at_ms, updated_at_ms) "
        "VALUES(?,?,?,'ACTIVE',?,?)",
        (anidb_id, tvdb_series_id, season, now, now))
    scope_id = db.execute(
        "SELECT scope_id FROM batch_candidate_scopes WHERE anidb_id=? AND tvdb_season=?",
        (anidb_id, season)).fetchone()[0]
    return BatchScope(scope_id, anidb_id, tvdb_series_id, season, None, None, None, 'ACTIVE', now, now)


def add_candidate(db, scope_id, info_hash, title, *, source_event_id=None,
                  seeders=None, size_bytes=None, has_metadata=0,
                  metadata_json=None, now_ms=None):
    """Add a candidate to the scope pool; deduplicates by infohash and records event provenance."""
    integer(scope_id, positive=True)
    digest = infohash(info_hash)
    if not isinstance(title, str) or not title.strip():
        raise CandidateConflict()
    clean_title = title.strip()
    now = now_ms if now_ms is not None else _now()

    if source_event_id is not None:
        integer(source_event_id, positive=True)
        db.execute(
            "INSERT OR IGNORE INTO batch_candidate_events(scope_id, info_hash, event_id, received_at_ms) "
            "VALUES(?,?,?,?)",
            (scope_id, digest, source_event_id, now))

    existing = db.execute(
        "SELECT candidate_id, scope_id, info_hash, title, rank, state, reason_code, "
        "has_metadata, metadata_json, seeders, size_bytes, source_event_id, created_at_ms, updated_at_ms "
        "FROM batch_candidates WHERE scope_id=? AND info_hash=?",
        (scope_id, digest)).fetchone()
    if existing is not None:
        cand = BatchCandidate(*existing)
        # If metadata became known later, update evidence read-only
        if has_metadata and not cand.has_metadata:
            db.execute(
                "UPDATE batch_candidates SET has_metadata=1, metadata_json=?, updated_at_ms=? "
                "WHERE candidate_id=?",
                (metadata_json, now, cand.candidate_id))
            return BatchCandidate(cand.candidate_id, cand.scope_id, cand.info_hash, cand.title,
                                  cand.rank, cand.state, cand.reason_code, 1, metadata_json,
                                  cand.seeders, cand.size_bytes, cand.source_event_id,
                                  cand.created_at_ms, now)
        return cand

    count = db.execute(
        "SELECT count(*) FROM batch_candidates WHERE scope_id=?", (scope_id,)).fetchone()[0]
    next_rank = count + 1

    db.execute(
        "INSERT INTO batch_candidates("
        "scope_id, info_hash, title, rank, state, has_metadata, metadata_json, "
        "seeders, size_bytes, source_event_id, created_at_ms, updated_at_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (scope_id, digest, clean_title, next_rank, 'READY', int(bool(has_metadata)),
         metadata_json, seeders, size_bytes, source_event_id, now, now))
    # A new release reopens a season that had run out of candidates.
    db.execute(
        "UPDATE batch_candidate_scopes SET state='ACTIVE', updated_at_ms=? "
        "WHERE scope_id=? AND state='EXHAUSTED'",
        (now, scope_id))
    cid = db.execute(
        "SELECT candidate_id FROM batch_candidates WHERE scope_id=? AND info_hash=?",
        (scope_id, digest)).fetchone()[0]
    return BatchCandidate(cid, scope_id, digest, clean_title, next_rank, 'READY', None,
                          int(bool(has_metadata)), metadata_json, seeders, size_bytes,
                          source_event_id, now, now)


@dataclass(frozen=True)
class ReleaseFacts:
    """One identified season-pack delivery for a scope, read from its event."""
    event_id: int
    info_hash: str
    title: str
    seeders: int | None = None
    size_bytes: int | None = None


def _event_document(raw_body):
    body = json.loads(bytes(raw_body).decode('utf-8') if isinstance(raw_body, (bytes, memoryview)) else raw_body)
    if not isinstance(body, dict):
        raise ValueError('event body is not an object')
    return body


def _count(value, *, positive=False):
    return value if type(value) is int and (value > 0 if positive else value >= 0) else None


def scope_releases(db, anidb_id, season):
    """Read-only: identified season-pack deliveries for (anidb_id, season).

    A delivery whose stored body cannot describe a candidate (no title or no
    valid infohash) is skipped; database errors propagate.
    """
    integer(anidb_id, positive=True)
    integer(season, positive=True)
    rows = db.execute(
        "SELECT e.event_id, e.raw_body FROM incoming_events e "
        "JOIN identity_results r ON r.event_id=e.event_id "
        "WHERE r.anidb_id=? ORDER BY e.event_id",
        (anidb_id,)).fetchall()
    found = []
    for event_id, raw_body in rows:
        try:
            body = _event_document(raw_body)
            title = body.get('release_name') or body.get('title') or ''
            if type(title) is not str or not title.strip():
                continue
            digest = infohash(body.get('torrent_infohash') or body.get('info_hash'))
        except (ValueError, TypeError, UnicodeError):
            continue
        hint = release_scope_hint(title)
        if hint.kind not in ('season_pack', 'mixed_pack') or hint.season_hint != season:
            continue
        found.append(ReleaseFacts(event_id, digest, title.strip(),
                                  _count(body.get('seeders')), _count(body.get('size'), positive=True)))
    return tuple(found)


def _metadata_evidence(db, digest, observations):
    """(has_metadata, metadata_json) from durable admissions/verifications or a read-only observation."""
    row = db.execute(
        "SELECT inventory_json FROM batch_metadata_admissions WHERE info_hash=? AND inventory_json IS NOT NULL",
        (digest,)).fetchone()
    if row is not None and row[0]:
        return 1, row[0]
    if db.execute("SELECT 1 FROM batch_verifications WHERE info_hash=?", (digest,)).fetchone():
        return 1, None
    observed = (observations or {}).get(digest)
    if observed is not None:
        return 1, observed.evidence_json()
    return 0, None


def discover_existing_candidates(db, scope_id, anidb_id, season, *, observations=None, now_ms=None):
    """WI-02: register the scope's existing deliveries as candidates.

    observations maps infohash to ExistingMetadata read from qBittorrent
    *before* this transaction (acquisition/existing_metadata.py). They are
    evidence for ranking only and never authorize adopting a torrent.
    """
    integer(scope_id, positive=True)
    now = now_ms if now_ms is not None else _now()
    added = []
    for release in scope_releases(db, anidb_id, season):
        has_meta, meta_json = _metadata_evidence(db, release.info_hash, observations)
        added.append(add_candidate(db, scope_id, release.info_hash, release.title,
                                   source_event_id=release.event_id, seeders=release.seeders,
                                   size_bytes=release.size_bytes, has_metadata=has_meta,
                                   metadata_json=meta_json, now_ms=now))
    return tuple(added)


@dataclass(frozen=True)
class CoordinationDecision:
    """PROCEED: this delivery is the scope's active candidate and continues.
    POOLED: another candidate is active or ranks higher; winner_candidate_id,
    when set, must be activated and scheduled by the pooled job's completion.
    """
    action: str
    scope_id: int
    candidate_id: int
    winner_candidate_id: int | None = None


_OPEN_JOB_STATES = ('PENDING', 'RUNNING', 'RETRY_WAIT', 'ON_HOLD')


def _job_operation(db, job_id):
    row = db.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise CandidateConflict()
    return row[0]


def coordinate_release(db, *, anidb_id, tvdb_series_id, season, info_hash, title, event_id, job_id,
                       seeders=None, size_bytes=None, observations=None,
                       audio_check='japanese_english', now_ms=None):
    """Register one arriving season pack and decide, inside one write transaction.

    Every input that came from the network was gathered before this
    transaction; everything decided here is re-read under the write lock, so
    concurrent arrivals cannot activate two candidates.
    """
    if not db.in_transaction:
        raise CandidateConflict()
    now = now_ms if now_ms is not None else _now()
    digest = infohash(info_hash)
    scope = get_or_create_scope(db, anidb_id, tvdb_series_id, season, now_ms=now)
    discover_existing_candidates(db, scope.scope_id, anidb_id, season, observations=observations, now_ms=now)
    has_meta, meta_json = _metadata_evidence(db, digest, observations)
    cand = add_candidate(db, scope.scope_id, digest, title, source_event_id=event_id,
                         seeders=_count(seeders), size_bytes=_count(size_bytes, positive=True),
                         has_metadata=has_meta, metadata_json=meta_json, now_ms=now)
    reorder_candidates(db, scope.scope_id,
                       rank_candidates(get_candidate_pool(db, scope.scope_id), audio_check=audio_check),
                       now_ms=now)
    state = db.execute("SELECT state FROM batch_candidate_scopes WHERE scope_id=?", (scope.scope_id,)).fetchone()[0]
    active = get_active_candidate(db, scope.scope_id)
    if active is not None:
        # Only the active candidate's own operation continues (also after a
        # restart). Another job for the same release, such as an older
        # pooled delivery of the same hash, waits: never two downloads.
        operation = db.execute("SELECT operation_id FROM batch_candidate_scopes WHERE scope_id=?",
                               (scope.scope_id,)).fetchone()[0]
        mine = active.info_hash == digest and _job_operation(db, job_id) == operation
        return CoordinationDecision('PROCEED' if mine else 'POOLED', scope.scope_id, cand.candidate_id)
    if state == 'SUCCEEDED':
        return CoordinationDecision('POOLED', scope.scope_id, cand.candidate_id)
    top = next_candidate(db, scope.scope_id)
    if top is None:
        # This delivery already failed or was canceled in this cycle.
        return CoordinationDecision('POOLED', scope.scope_id, cand.candidate_id)
    if top.info_hash == digest:
        activate_candidate(db, scope.scope_id, digest, operation_id=_job_operation(db, job_id), now_ms=now)
        return CoordinationDecision('PROCEED', scope.scope_id, cand.candidate_id)
    return CoordinationDecision('POOLED', scope.scope_id, cand.candidate_id, top.candidate_id)


def schedule_winner(db, scope_id, candidate_id, *, created_by_job_id=None, now_ms=None):
    """Activate a higher-ranked READY candidate and make sure a job runs it.

    Called in the pooled job's completion transaction. Re-checks that nothing
    else became active meanwhile and that the candidate is still the best
    READY one; otherwise does nothing. An open VERIFY_RELEASE job for the
    candidate's delivery is reused instead of creating a second one.
    """
    if not db.in_transaction:
        raise CandidateConflict()
    integer(scope_id, positive=True)
    now = now_ms if now_ms is not None else _now()
    state = db.execute("SELECT state FROM batch_candidate_scopes WHERE scope_id=?", (scope_id,)).fetchone()
    if state is None or state[0] == 'SUCCEEDED' or get_active_candidate(db, scope_id) is not None:
        return ()
    top = next_candidate(db, scope_id)
    if top is None or top.candidate_id != candidate_id:
        return ()
    anidb_id = db.execute("SELECT anidb_id FROM batch_candidate_scopes WHERE scope_id=?", (scope_id,)).fetchone()[0]
    receipt = _start_candidate(db, scope_id, anidb_id, top, created_by_job_id=created_by_job_id, now_ms=now)
    return () if receipt is None else (receipt,)


def rank_candidates(candidates, *, audio_check='japanese_english', rank_by='resolution'):
    """Rank eligible candidates: audio suitability, metadata readiness, resolution, seeders."""
    items = tuple(candidates)
    req_dual = wants_english(audio_check)

    def sort_key(cand):
        title = cand.title
        # Incompatible foreign dubs ranked last when japanese_english is required (WI-06)
        incompatible = 1 if is_audio_incompatible(title, audio_check) else 0

        # Audio suitability: Dual/Multi Audio tag prioritized when Japanese+English required
        is_dual = dual_audio_tagged(title)
        audio_priority = 0 if (not req_dual or is_dual) else 1

        # Metadata readiness: metadata-ready releases prioritized ahead of discovery-needed (WI-02)
        meta_priority = 0 if cand.has_metadata else 1

        # Resolution preference
        res = resolution(title)
        seed = cand.seeders or 0

        pref = (-res, -seed) if rank_by == 'resolution' else (-seed, -res)
        return (incompatible, audio_priority, meta_priority, pref[0], pref[1], cand.candidate_id)

    return tuple(sorted(items, key=sort_key))


def reorder_candidates(db, scope_id, ranked_candidates, *, now_ms=None):
    """Update ranks for READY candidates to reflect latest policy ranking.

    Only rows whose rank changes are written, so seeing the same releases
    again leaves their records (and updated_at_ms) untouched.
    """
    integer(scope_id, positive=True)
    now = now_ms if now_ms is not None else _now()
    rank = 1
    for cand in ranked_candidates:
        if cand.state == 'READY':
            db.execute(
                "UPDATE batch_candidates SET rank=?, updated_at_ms=? WHERE candidate_id=? AND rank<>?",
                (rank, now, cand.candidate_id, rank))
            rank += 1


def next_candidate(db, scope_id):
    """Read-only query for the next untried READY candidate in rank order."""
    integer(scope_id, positive=True)
    row = db.execute(
        "SELECT candidate_id, scope_id, info_hash, title, rank, state, reason_code, "
        "has_metadata, metadata_json, seeders, size_bytes, source_event_id, created_at_ms, updated_at_ms "
        "FROM batch_candidates WHERE scope_id=? AND state='READY' ORDER BY rank ASC LIMIT 1",
        (scope_id,)).fetchone()
    return BatchCandidate(*row) if row is not None else None


def activate_candidate(db, scope_id, info_hash, *, operation_id=None, now_ms=None):
    """Activate exactly one candidate for the scope. Fails closed on conflicting active candidate."""
    integer(scope_id, positive=True)
    digest = infohash(info_hash)
    now = now_ms if now_ms is not None else _now()

    active = db.execute(
        "SELECT candidate_id, info_hash FROM batch_candidates WHERE scope_id=? AND state='ACTIVE'",
        (scope_id,)).fetchall()
    if active:
        if active == [(active[0][0], digest)]:
            # Idempotent re-activation of the same candidate
            if operation_id is not None:
                db.execute(
                    "UPDATE batch_candidate_scopes SET operation_id=?, updated_at_ms=? WHERE scope_id=?",
                    (operation_id, now, scope_id))
            return True
        raise CandidateConflict()

    cand = db.execute(
        "SELECT candidate_id, state FROM batch_candidates WHERE scope_id=? AND info_hash=?",
        (scope_id, digest)).fetchone()
    if cand is None or cand[1] != 'READY':
        raise CandidateConflict()

    cid = cand[0]
    db.execute(
        "UPDATE batch_candidates SET state='ACTIVE', updated_at_ms=? WHERE candidate_id=?",
        (now, cid))
    # Activating a candidate reopens a scope that had run out of candidates.
    db.execute(
        "UPDATE batch_candidate_scopes SET active_candidate_id=?, operation_id=?, "
        "state=CASE state WHEN 'EXHAUSTED' THEN 'ACTIVE' ELSE state END, updated_at_ms=? "
        "WHERE scope_id=?",
        (cid, operation_id, now, scope_id))
    return True


def get_candidate(db, scope_id, info_hash):
    """Retrieve candidate by scope_id and info_hash."""
    integer(scope_id, positive=True)
    digest = infohash(info_hash)
    row = db.execute(
        "SELECT candidate_id, scope_id, info_hash, title, rank, state, reason_code, "
        "has_metadata, metadata_json, seeders, size_bytes, source_event_id, "
        "created_at_ms, updated_at_ms FROM batch_candidates WHERE scope_id=? AND info_hash=?",
        (scope_id, digest)).fetchone()
    if row is None:
        return None
    return BatchCandidate(*row)


def get_active_candidate(db, scope_id):
    """Return the currently ACTIVE candidate for the scope, or None."""
    integer(scope_id, positive=True)
    row = db.execute(
        "SELECT candidate_id, scope_id, info_hash, title, rank, state, reason_code, "
        "has_metadata, metadata_json, seeders, size_bytes, source_event_id, created_at_ms, updated_at_ms "
        "FROM batch_candidates WHERE scope_id=? AND state='ACTIVE' LIMIT 1",
        (scope_id,)).fetchone()
    return BatchCandidate(*row) if row is not None else None


def get_candidate_pool(db, scope_id):
    """Return all candidates for the scope ordered by rank."""
    integer(scope_id, positive=True)
    rows = db.execute(
        "SELECT candidate_id, scope_id, info_hash, title, rank, state, reason_code, "
        "has_metadata, metadata_json, seeders, size_bytes, source_event_id, created_at_ms, updated_at_ms "
        "FROM batch_candidates WHERE scope_id=? ORDER BY rank ASC, candidate_id ASC",
        (scope_id,)).fetchall()
    return tuple(BatchCandidate(*r) for r in rows)


def fail_candidate(db, scope_id, info_hash, reason_code, *, now_ms=None):
    """Mark an active candidate FAILED with its failure reason code and clear active slot."""
    integer(scope_id, positive=True)
    digest = infohash(info_hash)
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise CandidateConflict()
    now = now_ms if now_ms is not None else _now()

    updated = db.execute(
        "UPDATE batch_candidates SET state='FAILED', reason_code=?, updated_at_ms=? "
        "WHERE scope_id=? AND info_hash=? AND state='ACTIVE'",
        (reason_code.strip(), now, scope_id, digest))
    if updated.rowcount != 1:
        raise CandidateConflict()

    db.execute(
        "UPDATE batch_candidate_scopes SET active_candidate_id=NULL, updated_at_ms=? "
        "WHERE scope_id=?",
        (now, scope_id))


def cancel_candidate(db, scope_id, info_hash, reason_code='JOB_CANCELED', *, now_ms=None):
    """Mark an active candidate CANCELED and clear active slot."""
    integer(scope_id, positive=True)
    digest = infohash(info_hash)
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise CandidateConflict()
    now = now_ms if now_ms is not None else _now()

    updated = db.execute(
        "UPDATE batch_candidates SET state='CANCELED', reason_code=?, updated_at_ms=? "
        "WHERE scope_id=? AND info_hash=? AND state='ACTIVE'",
        (reason_code.strip(), now, scope_id, digest))
    if updated.rowcount != 1:
        raise CandidateConflict()

    db.execute(
        "UPDATE batch_candidate_scopes SET active_candidate_id=NULL, updated_at_ms=? "
        "WHERE scope_id=?",
        (now, scope_id))


def succeed_candidate(db, scope_id, info_hash, *, now_ms=None):
    """Mark an active candidate and the scope SUCCEEDED."""
    integer(scope_id, positive=True)
    digest = infohash(info_hash)
    now = now_ms if now_ms is not None else _now()

    updated = db.execute(
        "UPDATE batch_candidates SET state='SUCCEEDED', updated_at_ms=? "
        "WHERE scope_id=? AND info_hash=? AND state='ACTIVE'",
        (now, scope_id, digest))
    if updated.rowcount != 1:
        raise CandidateConflict()

    db.execute(
        "UPDATE batch_candidate_scopes SET state='SUCCEEDED', updated_at_ms=? WHERE scope_id=?",
        (now, scope_id))


def exhaust_scope(db, scope_id, *, now_ms=None):
    """Mark the scope EXHAUSTED when no eligible candidate remains."""
    integer(scope_id, positive=True)
    now = now_ms if now_ms is not None else _now()
    db.execute(
        "UPDATE batch_candidate_scopes SET state='EXHAUSTED', active_candidate_id=NULL, updated_at_ms=? "
        "WHERE scope_id=?",
        (now, scope_id))


STOP_DISPOSITIONS = ('FAILED', 'CANCELED')


def _active_for_operation(db, operation_id):
    """(scope_id, active candidate) for the scope this operation is running, or None."""
    row = db.execute("SELECT scope_id FROM batch_candidate_scopes WHERE operation_id=? "
                     "AND active_candidate_id IS NOT NULL", (operation_id,)).fetchone()
    if row is None:
        return None
    active = get_active_candidate(db, row[0])
    return None if active is None else (row[0], active)


def record_candidate_stop(db, operation_id, disposition, reason_code, *, now_ms=None):
    """Remember why the active candidate of this operation is stopping (WI-13).

    Called where the decision is made: a user Cancel (CANCELED) or a job that
    failed verification, metadata, size or audio checks (FAILED). The first
    decision wins; cleanup applies it later. Returns True when recorded.
    """
    if not db.in_transaction or disposition not in STOP_DISPOSITIONS:
        raise CandidateConflict()
    if type(reason_code) is not str or not 1 <= len(reason_code.strip()) <= 64:
        raise CandidateConflict()
    found = _active_for_operation(db, operation_id)
    if found is None:
        return False
    now = now_ms if now_ms is not None else _now()
    return db.execute(
        "INSERT OR IGNORE INTO batch_candidate_stops(candidate_id,operation_id,disposition,reason_code,recorded_at_ms) "
        "VALUES(?,?,?,?,?)", (found[1].candidate_id, operation_id, disposition, reason_code.strip(), now)).rowcount == 1


def auto_cleanup_failed_batch_candidate(db, claim):
    """A definitive failure of the active candidate's job: record FAILED, then clean up.

    Uses the same cancellation transition and cleanup control job as a user
    Cancel (sonarr_candidates.request_automatic_cleanup); only the recorded
    disposition differs.
    """
    if not db.in_transaction or claim.event_id is None:
        return ()
    from src.nextgen.storage.cancellation import operation
    op = operation(db, claim.job_id)
    if op[1] != 'ACTIVE' or _active_for_operation(db, op[0]) is None:
        return ()
    if db.execute("SELECT 1 FROM management_actions WHERE operation_id=? AND action='CLEANUP'", (op[0],)).fetchone():
        return ()
    reason = db.execute("SELECT last_reason_code FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()[0]
    record_candidate_stop(db, op[0], 'FAILED', reason or 'JOB_FAILED')
    from src.nextgen.storage.sonarr_candidates import request_automatic_cleanup
    return request_automatic_cleanup(db, claim)


def _legacy_disposition(db, operation_id):
    """Stops recorded before batch_candidate_stops existed.

    Same rule as single episodes (sonarr_candidates.schedule_after_cleanup):
    only a cleanup control created BY a job of this operation is an automatic
    failure; any other cleanup was requested by a person.
    """
    row = db.execute(
        "SELECT target.last_reason_code FROM management_actions a "
        "JOIN jobs control ON control.job_id=a.job_id AND control.kind='CLEANUP_CANCELED_OPERATION' "
        "JOIN job_operation_members source ON source.job_id=control.created_by_job_id "
        "AND source.operation_id=a.operation_id "
        "JOIN jobs target ON target.job_id=a.target_job_id "
        "WHERE a.operation_id=? AND a.action='CLEANUP' LIMIT 1", (operation_id,)).fetchone()
    if row is None:
        return 'CANCELED', 'JOB_CANCELED'
    return 'FAILED', row[0] or 'JOB_FAILED'


def _anime_rejected(db, anidb_id):
    return db.execute("SELECT 1 FROM anime_review_decisions WHERE anidb_id=? AND decision='REJECT'",
                      (anidb_id,)).fetchone() is not None


def _start_candidate(db, scope_id, anidb_id, candidate, *, created_by_job_id=None, now_ms=None):
    """Activate a READY candidate with the VERIFY_RELEASE job that runs it.

    An open VERIFY_RELEASE job for the candidate's delivery (one that was
    pooled and has not run yet) is adopted instead of creating a second one.
    Returns the new job's receipt, or None when an open job was adopted.
    """
    if candidate.source_event_id is None:
        raise CandidateConflict()
    marks = ','.join('?' * len(_OPEN_JOB_STATES))
    open_job = db.execute(
        f"SELECT job_id FROM jobs WHERE kind='VERIFY_RELEASE' AND event_id=? AND state IN ({marks}) "
        "ORDER BY job_id LIMIT 1", (candidate.source_event_id, *_OPEN_JOB_STATES)).fetchone()
    if open_job is not None:
        activate_candidate(db, scope_id, candidate.info_hash, operation_id=_job_operation(db, open_job[0]),
                           now_ms=now_ms)
        return None
    from src.nextgen.storage.sqlite import enqueue_job_with_receipt
    job_id, receipt = enqueue_job_with_receipt(
        db, component='worker', kind='VERIFY_RELEASE', payload={'anidb_id': anidb_id},
        event_id=candidate.source_event_id, created_by_job_id=created_by_job_id,
        available_at_ms=now_ms if now_ms is not None else _now())
    activate_candidate(db, scope_id, candidate.info_hash, operation_id=_job_operation(db, job_id), now_ms=now_ms)
    return receipt


def schedule_after_cleanup_batch(db, operation_id):
    """Cleanup of the active candidate is COMPLETE: apply its stop, then advance (WI-04, WI-13).

    Runs in the cleanup's completion transaction, after its torrents, files
    and reservations were released, so the next candidate never starts
    early. Idempotent: once applied, the operation no longer belongs to an
    active candidate and a repeat does nothing.
    """
    if not db.in_transaction:
        raise CandidateConflict()
    found = _active_for_operation(db, operation_id)
    if found is None:
        return ()
    scope_id, active = found
    now = _now()
    stop = db.execute("SELECT disposition,reason_code FROM batch_candidate_stops WHERE candidate_id=?",
                      (active.candidate_id,)).fetchone()
    disposition, reason = stop if stop is not None else _legacy_disposition(db, operation_id)
    if disposition == 'CANCELED':
        cancel_candidate(db, scope_id, active.info_hash, reason, now_ms=now)
    else:
        fail_candidate(db, scope_id, active.info_hash, reason, now_ms=now)
    anidb_id = db.execute("SELECT anidb_id FROM batch_candidate_scopes WHERE scope_id=?", (scope_id,)).fetchone()[0]
    if _anime_rejected(db, anidb_id):
        return ()   # Rejecting the series stops the season; it does not try the next release.
    nxt = next_candidate(db, scope_id)
    if nxt is None:
        # Known candidates are exhausted: one controlled Prowlarr search.
        return request_prowlarr_fallback(db, scope_id, event_id=active.source_event_id, now_ms=now)
    receipt = _start_candidate(db, scope_id, anidb_id, nxt, now_ms=now)
    return () if receipt is None else (receipt,)


def succeed_operation_candidate(db, operation_id, *, now_ms=None):
    """The operation reached STAGED: its active candidate and scope SUCCEEDED."""
    found = _active_for_operation(db, operation_id)
    if found is not None:
        succeed_candidate(db, found[0], found[1].info_hash, now_ms=now_ms)


# --- Prowlarr fallback (WI-05, WI-14, WI-15) ----------------------------------

DISCOVERY_KIND = 'DISCOVER_BATCH_PROWLARR'
# Minimum time between two batch searches for the same season.
FALLBACK_COOLDOWN_MS = 5 * 60 * 1000


def _open_discovery(db, scope_id):
    marks = ','.join('?' * len(_OPEN_JOB_STATES))
    return db.execute(
        f"SELECT job_id FROM jobs WHERE kind=? AND json_extract(payload_json,'$.scope_id')=? "
        f"AND state IN ({marks}) LIMIT 1", (DISCOVERY_KIND, scope_id, *_OPEN_JOB_STATES)).fetchone()


def request_prowlarr_fallback(db, scope_id, *, event_id, created_by_job_id=None, now_ms=None):
    """Known candidates are exhausted: schedule one Prowlarr batch search.

    The scope stays ACTIVE (not EXHAUSTED) until a search completes and finds
    nothing. An open search job is never duplicated, also after restarts;
    a search within the cooldown is delayed, not skipped.
    """
    if not db.in_transaction:
        raise CandidateConflict()
    integer(scope_id, positive=True)
    now = now_ms if now_ms is not None else _now()
    if _open_discovery(db, scope_id) is not None:
        return ()
    anidb_id, tvdb_series_id, season = db.execute(
        "SELECT anidb_id,tvdb_series_id,tvdb_season FROM batch_candidate_scopes WHERE scope_id=?",
        (scope_id,)).fetchone()
    last = db.execute(
        "SELECT max(captured_at_ms) FROM prowlarr_search_snapshots "
        "WHERE tvdb_series_id=? AND season=? AND search_mode='batch_fallback'",
        (tvdb_series_id, season)).fetchone()[0]
    available = now if last is None else max(now, last + FALLBACK_COOLDOWN_MS)
    from src.nextgen.storage.sqlite import enqueue_job_with_receipt
    _, receipt = enqueue_job_with_receipt(
        db, component='worker', kind=DISCOVERY_KIND,
        payload={'scope_id': scope_id, 'anidb_id': anidb_id, 'tvdb_series_id': tvdb_series_id,
                 'tvdb_season': season},
        event_id=event_id, created_by_job_id=created_by_job_id, available_at_ms=available)
    return (receipt,)


@dataclass(frozen=True)
class SearchParameters:
    scope_id: int
    anidb_id: int
    tvdb_series_id: int
    season: int
    titles: tuple
    tried: frozenset


def prepare_batch_prowlarr_search(db, scope_id):
    """Read-only: what to search for, or None when the scope no longer needs a search."""
    integer(scope_id, positive=True)
    scope = db.execute(
        "SELECT anidb_id,tvdb_series_id,tvdb_season,state,active_candidate_id "
        "FROM batch_candidate_scopes WHERE scope_id=?", (scope_id,)).fetchone()
    if scope is None or scope[3] != 'ACTIVE' or scope[4] is not None:
        return None
    anidb_id, tvdb_series_id, season = scope[:3]
    if next_candidate(db, scope_id) is not None:
        return None          # A release arrived meanwhile; no search is needed.
    tried = frozenset(r[0] for r in db.execute(
        "SELECT info_hash FROM batch_candidates WHERE scope_id=?", (scope_id,)))
    main = db.execute("SELECT title FROM identified_anime WHERE anidb_id=?", (anidb_id,)).fetchone()
    meta = db.execute("SELECT english_title,romaji_title FROM anime_title_metadata WHERE anidb_id=?",
                      (anidb_id,)).fetchone() or (None, None)
    titles = tuple(dict.fromkeys(t for t in (main[0] if main else None, *meta) if t))
    if not titles:
        raise CandidateConflict()
    return SearchParameters(scope_id, anidb_id, tvdb_series_id, season, titles, tried)


def _verified_series(db, anidb_id, tvdb_series_id):
    """The anime identity already verified for this scope; never fabricated."""
    from src.nextgen.job_contracts import parse_payload
    row = db.execute(
        "SELECT r.series_json FROM batch_candidates c "
        "JOIN batch_candidate_scopes s ON s.scope_id=c.scope_id "
        "JOIN identity_results r ON r.event_id=c.source_event_id "
        "WHERE s.anidb_id=? AND r.anidb_id=? ORDER BY r.validated_at_ms DESC LIMIT 1",
        (anidb_id, anidb_id)).fetchone()
    if row is None:
        raise CandidateConflict()
    series = parse_payload(row[0])
    anime = series.get('anime', {})
    if anime.get('anidb_id') != anidb_id or anime.get('tvdb_series_id') != tvdb_series_id:
        raise CandidateConflict()
    return series


def _deliver(db, release, series, now):
    """One Prowlarr release as an internal delivery, in the established event contract.

    Same body as single-episode backfill (sonarr_dispatch): the canonical
    magnet_uri field holds the hash-bound, tracker-sanitized magnet, which
    the batch metadata reader uses when it admits the torrent.
    """
    from src.nextgen.acquisition.metadata import hash_magnet
    from src.nextgen.history import append_event_history
    from src.nextgen.storage.sqlite import persist_identity
    digest = infohash(release.info_hash)
    magnet = hash_magnet(digest, release.magnet_url)
    if magnet is None:
        raise CandidateConflict()      # The magnet names a different torrent.
    body = json.dumps({'release_name': release.title, 'torrent_infohash': digest, 'magnet_uri': magnet,
                       'seeders': release.seeders, 'size': release.size or None, 'indexer': release.indexer},
                      sort_keys=True, separators=(',', ':')).encode('utf-8')
    event_id = db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES(?,?,?)",
                          ('prowlarr-batch', body, now)).lastrowid
    receipts = [append_event_history(db, event_id=event_id, event_code='ACCEPTED', component='worker',
                                     occurred_at_ms=now)]
    _, audit = persist_identity(db, event_id, series, validated_at_ms=now)
    if audit is not None:
        receipts.append(audit)
    return event_id, receipts


def ingest_batch_prowlarr_results(db, params, result, *, snapshots=None, job_id=None, attempt=0,
                                  audio_check='japanese_english', now_ms=None):
    """After a completed search, inside one write transaction: record and decide.

    Re-reads the controller first: a release that arrived during the search
    may already be active, in which case the new candidates only join the
    pool. EXHAUSTED only follows a search in which every query succeeded and
    nothing eligible was found.
    """
    if not db.in_transaction or type(params) is not SearchParameters:
        raise CandidateConflict()
    now = now_ms if now_ms is not None else _now()
    if job_id is not None and snapshots:
        from src.nextgen.storage.prowlarr_snapshots import save_snapshots
        save_snapshots(db, job_id, attempt, snapshots, now_ms=now)
    state = db.execute("SELECT state FROM batch_candidate_scopes WHERE scope_id=?", (params.scope_id,)).fetchone()
    if state is None or state[0] == 'SUCCEEDED':
        return ()
    receipts = []
    known = {c.info_hash for c in get_candidate_pool(db, params.scope_id)}
    fresh = [r for r in result.releases if infohash(r.info_hash) not in known]
    if fresh:
        series = _verified_series(db, params.anidb_id, params.tvdb_series_id)
        for release in fresh:
            event_id, delivered = _deliver(db, release, series, now)
            receipts.extend(delivered)
            add_candidate(db, params.scope_id, release.info_hash, release.title, source_event_id=event_id,
                          seeders=_count(release.seeders), size_bytes=_count(release.size, positive=True),
                          now_ms=now)
        reorder_candidates(db, params.scope_id,
                           rank_candidates(get_candidate_pool(db, params.scope_id), audio_check=audio_check),
                           now_ms=now)
    if get_active_candidate(db, params.scope_id) is not None:
        return tuple(receipts)
    top = next_candidate(db, params.scope_id)
    if top is None:
        if result.complete:
            exhaust_scope(db, params.scope_id, now_ms=now)
        return tuple(receipts)
    started = _start_candidate(db, params.scope_id, params.anidb_id, top, created_by_job_id=job_id, now_ms=now)
    return tuple(receipts) + (() if started is None else (started,))
