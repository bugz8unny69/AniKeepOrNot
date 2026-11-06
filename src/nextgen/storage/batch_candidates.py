"""Durable series-scoped batch candidate pool and controller (WI-01, WI-02, WI-04).

Maintains one active batch-acquisition operation per (anidb_id, season) scope,
with a durable, ranked list of candidate releases.
"""
from dataclasses import dataclass, field
import json
import sqlite3
import time

from src.nextgen.acquisition.audio import dual_audio_tagged, wants_english
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
        # A restarted job of the active candidate continues; anything else waits.
        action = 'PROCEED' if active.info_hash == digest else 'POOLED'
        return CoordinationDecision(action, scope.scope_id, cand.candidate_id)
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
    if top.source_event_id is None:
        raise CandidateConflict()
    anidb_id = db.execute("SELECT anidb_id FROM batch_candidate_scopes WHERE scope_id=?", (scope_id,)).fetchone()[0]
    marks = ','.join('?' * len(_OPEN_JOB_STATES))
    open_job = db.execute(
        f"SELECT job_id FROM jobs WHERE kind='VERIFY_RELEASE' AND event_id=? AND state IN ({marks}) "
        "ORDER BY job_id LIMIT 1", (top.source_event_id, *_OPEN_JOB_STATES)).fetchone()
    if open_job is not None:
        activate_candidate(db, scope_id, top.info_hash, operation_id=_job_operation(db, open_job[0]), now_ms=now)
        return ()
    from src.nextgen.storage.sqlite import enqueue_job_with_receipt
    job_id, receipt = enqueue_job_with_receipt(
        db, component='worker', kind='VERIFY_RELEASE', payload={'anidb_id': anidb_id},
        event_id=top.source_event_id, created_by_job_id=created_by_job_id, available_at_ms=now)
    activate_candidate(db, scope_id, top.info_hash, operation_id=_job_operation(db, job_id), now_ms=now)
    return (receipt,)


def rank_candidates(candidates, *, audio_check='japanese_english', rank_by='resolution'):
    """Rank eligible candidates: audio suitability, metadata readiness, resolution, seeders."""
    items = tuple(candidates)
    req_dual = wants_english(audio_check)

    def sort_key(cand):
        title = cand.title
        # Audio suitability: Dual/Multi Audio tag prioritized when Japanese+English required
        is_dual = dual_audio_tagged(title)
        audio_priority = 0 if (not req_dual or is_dual) else 1

        # Metadata readiness: metadata-ready releases prioritized ahead of discovery-needed (WI-02)
        meta_priority = 0 if cand.has_metadata else 1

        # Resolution preference
        res = resolution(title)
        seed = cand.seeders or 0

        pref = (-res, -seed) if rank_by == 'resolution' else (-seed, -res)
        return (audio_priority, meta_priority, pref[0], pref[1], cand.candidate_id)

    return tuple(sorted(items, key=sort_key))


def reorder_candidates(db, scope_id, ranked_candidates, *, now_ms=None):
    """Update ranks for READY candidates to reflect latest policy ranking."""
    integer(scope_id, positive=True)
    now = now_ms if now_ms is not None else _now()
    rank = 1
    for cand in ranked_candidates:
        if cand.state == 'READY':
            db.execute(
                "UPDATE batch_candidates SET rank=?, updated_at_ms=? WHERE candidate_id=?",
                (rank, now, cand.candidate_id))
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
