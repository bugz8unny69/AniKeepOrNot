"""Replay a previously admitted magnet once, after cleanup. Never query Prowlarr.

The original internal event is the source; no duplicate magnet cache table.
"""
from dataclasses import dataclass
import json
import time

from src.nextgen.acquisition.audio import dual_audio_tagged
from src.nextgen.acquisition.metadata import hash_magnet
from src.nextgen.history import append_job_history

RETENTION_MS = 30 * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class CachedTorrent:
    info_hash: str
    title: str
    declared_title: str
    magnet_uri: str


def _cached(db, plan_id, episode, english, now=None):
    """Only the original, released magnet is eligible for one new attempt."""
    if now is None:
        now = time.time_ns() // 1000000
    rows = db.execute(
        "SELECT c.info_hash,e.raw_body FROM episode_release_candidates c "
        "JOIN magnet_intents i ON i.plan_id=c.plan_id "
        "AND i.anidb_episode_number=c.anidb_episode_number AND i.info_hash=c.info_hash "
        "JOIN incoming_events e ON e.event_id=i.event_id AND e.source='release-discovery' "
        "JOIN job_operation_members m ON m.job_id=i.job_id "
        "JOIN job_operations o ON o.operation_id=m.operation_id "
        "WHERE c.plan_id=? AND c.anidb_episode_number=? AND c.state='FAILED' "
        "AND c.reason_code='TORRENT_METADATA_TIMEOUT' AND i.state='RELEASED' "
        "AND o.state='CANCELED' AND o.cleanup_state='COMPLETE' "
        "AND i.created_at_ms>=? "
        "AND (SELECT count(*) FROM magnet_intents previous "
        "WHERE previous.plan_id=i.plan_id AND previous.anidb_episode_number=i.anidb_episode_number "
        "AND previous.info_hash=i.info_hash)=1 "
        "ORDER BY c.rank,i.event_id", (plan_id, episode, now-RETENTION_MS)
    ).fetchall()
    for digest, raw in rows:
        try:
            item = json.loads(raw)
            if type(item) is not dict or item.get('torrent_infohash') != digest:
                continue
            title, declared, magnet = (item.get(k) for k in (
                'source_release_name', 'release_name', 'magnet_uri'))
            if not all(type(x) is str and x for x in (title,declared,magnet)):
                continue
            if english and not dual_audio_tagged(title):
                continue
            if hash_magnet(digest,magnet) != magnet:
                continue
            yield CachedTorrent(digest,title,declared,magnet)
        except (ValueError,TypeError,UnicodeError,AttributeError):
            continue


def load_cached(database,plan_id,episode,english):
    from src.nextgen.storage.migrations import _open
    db=_open(database,readonly=True,timeout_ms=5000)
    try:
        return next(_cached(db,plan_id,episode,english),None)
    finally:
        db.close()


def _scrub(db, condition, args):
    """Remove reusable magnet only after the prior operation is fully cleaned."""
    return db.execute(
        "UPDATE incoming_events SET raw_body=CAST(json_remove(CAST(raw_body AS TEXT),"
        "'$.magnet_uri') AS BLOB) WHERE event_id IN ("
        "SELECT i.event_id FROM magnet_intents i "
        "JOIN job_operation_members m ON m.job_id=i.job_id "
        "JOIN job_operations o ON o.operation_id=m.operation_id "
        "WHERE i.state='RELEASED' AND o.state='CANCELED' "
        "AND o.cleanup_state='COMPLETE' AND " + condition + ") "
        "AND source='release-discovery' AND json_valid(CAST(raw_body AS TEXT)) "
        "AND json_type(CAST(raw_body AS TEXT),'$.magnet_uri')='text'",
        args).rowcount


def clear_cached(db,plan_id,episode):
    return _scrub(db,"i.plan_id=? AND i.anidb_episode_number=?",(plan_id,episode))


def expire_cached(db,now):
    """Opportunistic 30-day garbage cleanup on manual Search again."""
    return _scrub(db,"i.created_at_ms<?",(now-RETENTION_MS,))


def persist_cached(db,claim,plan,episode,expected,english):
    """Fenced, new verification operation; the old job remains historical."""
    from src.nextgen.storage.episode_discovery import BackfillBatch, BackfillConflict, _plan
    from src.nextgen.storage.release_dispatch import dispatch_cached
    from src.nextgen.episode_identity import VerifiedEpisode
    if (not db.in_transaction or type(plan) is not BackfillBatch
            or claim.kind!='DISCOVER_EPISODE_RELEASES' or claim.event_id is None
            or episode not in plan.numbers or type(expected) is not CachedTorrent):
        raise BackfillConflict()
    frozen=_plan(db,plan.plan_id,plan.start)
    if (frozen.anidb_id,frozen.tvdb_series_id,frozen.numbers) != (
            plan.anidb_id,plan.tvdb_series_id,plan.numbers):
        raise BackfillConflict()
    if next(_cached(db,plan.plan_id,episode,english),None)!=expected:
        raise BackfillConflict()
    current=db.execute(
        "SELECT s.tvdb_episode_id,s.info_hash,c.state,s.tvdb_season,s.tvdb_episode "
        "FROM episode_release_selections s "
        "JOIN episode_release_candidates c ON c.plan_id=s.plan_id "
        "AND c.anidb_episode_number=s.anidb_episode_number AND c.info_hash=s.info_hash "
        "WHERE s.plan_id=? AND s.anidb_episode_number=?",(plan.plan_id,episode)).fetchone()
    if current is None or current[2]!='FAILED':
        raise BackfillConflict()
    if db.execute(
        "SELECT 1 FROM episode_release_candidates WHERE plan_id=? AND anidb_episode_number=? "
        "AND state IN ('READY','ACTIVE','SUCCEEDED') LIMIT 1",
        (plan.plan_id,episode)).fetchone():
        raise BackfillConflict()
    for table in ('episode_reservations','batch_episode_reservations'):
        if db.execute(
            f"SELECT 1 FROM {table} WHERE tvdb_series_id=? AND tvdb_season=? "
            "AND tvdb_episode=? AND state='ACTIVE' LIMIT 1",
            (plan.tvdb_series_id,current[3],current[4])).fetchone():
            raise BackfillConflict()
    change=db.execute(
        "UPDATE episode_release_candidates SET state='ACTIVE',reason_code=NULL "
        "WHERE plan_id=? AND anidb_episode_number=? AND info_hash=? "
        "AND state='FAILED' AND reason_code='TORRENT_METADATA_TIMEOUT'",
        (plan.plan_id,episode,expected.info_hash))
    if change.rowcount!=1:
        raise BackfillConflict()
    change=db.execute(
        "UPDATE episode_release_selections SET info_hash=?,selected_at_ms=? "
        "WHERE plan_id=? AND anidb_episode_number=? AND info_hash=?",
        (expected.info_hash,time.time_ns()//1000000,plan.plan_id,episode,current[1]))
    if change.rowcount!=1:
        raise BackfillConflict()
    identity=VerifiedEpisode(plan.tvdb_series_id,current[0],current[3],current[4])
    routed=dispatch_cached(db,claim,plan,identity,expected)
    audit=append_job_history(
        db,job_id=claim.job_id,event_code='EPISODE_DISCOVERY_RECORDED',
        component='worker',occurred_at_ms=time.time_ns()//1000000,
        details={'attempt':claim.attempt,'cached_replay':True})
    return (audit,*routed)
