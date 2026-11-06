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
    cid = db.execute(
        "SELECT candidate_id FROM batch_candidates WHERE scope_id=? AND info_hash=?",
        (scope_id, digest)).fetchone()[0]
    return BatchCandidate(cid, scope_id, digest, clean_title, next_rank, 'READY', None,
                          int(bool(has_metadata)), metadata_json, seeders, size_bytes,
                          source_event_id, now, now)


def discover_existing_candidates(db, scope_id, anidb_id, season, *,
                                 client=None, qb_torrents=None, now_ms=None):
    """WI-02: Discover and register existing candidate releases from incoming_events and qBittorrent observations."""
    integer(scope_id, positive=True)
    integer(anidb_id, positive=True)
    integer(season, positive=True)
    now = now_ms if now_ms is not None else _now()

    # 1. Discover from incoming_events linked via identity_results to this anidb_id
    rows = db.execute(
        "SELECT e.event_id, e.raw_body, e.received_at_ms "
        "FROM incoming_events e "
        "JOIN identity_results r ON r.event_id=e.event_id "
        "WHERE r.anidb_id=?",
        (anidb_id,)).fetchall()

    added = []
    for event_id, raw_body, received_at in rows:
        try:
            body = json.loads(bytes(raw_body).decode('utf-8')) if isinstance(raw_body, (bytes, memoryview)) else json.loads(raw_body)
            if not isinstance(body, dict):
                continue
            title = body.get('release_name') or body.get('title') or ''
            hash_val = body.get('torrent_infohash') or body.get('info_hash') or ''
            if not title or not hash_val:
                continue
            digest = infohash(hash_val)
            hint = release_scope_hint(title)
            if hint.kind not in ('season_pack', 'mixed_pack') or hint.season_hint != season:
                continue

            seeders = body.get('seeders')
            size_bytes = body.get('size')

            # Check if metadata is already available in admissions or verifications
            has_meta = 0
            meta_json = None
            adm = db.execute(
                "SELECT inventory_json FROM batch_metadata_admissions WHERE info_hash=? AND inventory_json IS NOT NULL",
                (digest,)).fetchone()
            if adm is not None and adm[0]:
                has_meta = 1
                meta_json = adm[0]
            else:
                v_row = db.execute(
                    "SELECT batch_id FROM batch_verifications WHERE info_hash=?",
                    (digest,)).fetchone()
                if v_row is not None:
                    has_meta = 1

            # Check qB torrents or client read-only if passed
            if not has_meta:
                if qb_torrents:
                    qb_info = qb_torrents.get(digest)
                    if qb_info is not None:
                        files = getattr(qb_info, 'files', None)
                        if files or getattr(qb_info, 'has_metadata', False):
                            has_meta = 1
                elif client is not None and hasattr(client, 'inspect'):
                    try:
                        obs = client.inspect(digest, include_files=True)
                        if obs is not None and getattr(obs, 'files', None):
                            has_meta = 1
                            meta_json = json.dumps({'files': [{'index': f.index, 'name': f.name, 'size': f.size} for f in obs.files]})
                    except Exception:
                        pass

            cand = add_candidate(db, scope_id, digest, title, source_event_id=event_id,
                                 seeders=seeders, size_bytes=size_bytes, has_metadata=has_meta,
                                 metadata_json=meta_json, now_ms=now)
            added.append(cand)
        except Exception:
            continue

    return tuple(added)


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
    db.execute(
        "UPDATE batch_candidate_scopes SET active_candidate_id=?, operation_id=?, updated_at_ms=? "
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


def auto_cleanup_failed_batch_candidate(db, claim):
    """Enqueue the existing cancellation worker for a failed batch candidate."""
    if not db.in_transaction or claim.event_id is None:
        return ()
    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_candidate_scopes'").fetchone()
    if not has_table:
        return ()
    row = db.execute(
        "SELECT s.scope_id, s.operation_id FROM job_operation_members m "
        "JOIN batch_candidate_scopes s ON s.operation_id=m.operation_id "
        "WHERE m.job_id=? AND s.active_candidate_id IS NOT NULL",
        (claim.job_id,)).fetchone()
    if row is None:
        return ()
    from src.nextgen.storage.sonarr_candidates import request_automatic_cleanup
    return request_automatic_cleanup(db, claim)


def schedule_after_cleanup_batch(db, operation_id):
    """After cleanup of a failed batch candidate completes, advance to the next candidate (WI-04)."""
    if not db.in_transaction:
        raise CandidateConflict()
    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_candidate_scopes'").fetchone()
    if not has_table:
        return ()
    scope_row = db.execute(
        "SELECT scope_id, anidb_id, tvdb_series_id, tvdb_season, active_candidate_id "
        "FROM batch_candidate_scopes WHERE operation_id=?",
        (operation_id,)).fetchone()
    if scope_row is None:
        return ()
    scope_id, anidb_id, tvdb_series_id, season, active_cid = scope_row

    automated = db.execute(
        "SELECT a.target_job_id, control.payload_json FROM management_actions a "
        "JOIN jobs control ON control.job_id=a.job_id "
        "AND control.kind='CLEANUP_CANCELED_OPERATION' "
        "WHERE a.operation_id=? AND a.action='CLEANUP' LIMIT 1",
        (operation_id,)).fetchone()
    if automated is None:
        return ()
    target_job_id, payload_str = automated
    issue = None
    if payload_str:
        try:
            issue = json.loads(payload_str).get('failure_reason')
        except Exception:
            pass
    if not issue:
        try:
            t_row = db.execute("SELECT last_reason_code FROM jobs WHERE job_id=?", (target_job_id,)).fetchone()
            if t_row and t_row[0] and t_row[0] != 'JOB_CANCELED':
                issue = t_row[0]
        except sqlite3.OperationalError:
            pass
    if not issue:
        try:
            h_row = db.execute(
                "SELECT reason_code FROM job_history WHERE job_id=? AND event_code IN ('FAILED','NEEDS_REVIEW') "
                "ORDER BY history_id DESC LIMIT 1", (target_job_id,)).fetchone()
            if h_row and h_row[0]:
                issue = h_row[0]
        except sqlite3.OperationalError:
            pass
    if not issue:
        issue = 'OPERATION_FAILED'

    # Mark active candidate as FAILED
    active_cand = get_active_candidate(db, scope_id)
    if active_cand is not None:
        fail_candidate(db, scope_id, active_cand.info_hash, issue)

    # Pick next candidate in ranked pool
    next_cand = next_candidate(db, scope_id)
    if next_cand is None:
        exhaust_scope(db, scope_id)
        return ()

    now = _now()
    new_op_id = db.execute(
        "INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(?,?)",
        (now, now)).lastrowid
    activate_candidate(db, scope_id, next_cand.info_hash, operation_id=new_op_id)
    from src.nextgen.storage.sqlite import enqueue_job_with_receipt
    _, receipt = enqueue_job_with_receipt(db, component='worker', kind='VERIFY_RELEASE',
                                          payload={'anidb_id': anidb_id},
                                          available_at_ms=0,
                                          event_id=next_cand.source_event_id,
                                          operation_id=new_op_id)
    return (receipt,)

