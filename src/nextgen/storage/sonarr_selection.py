"""Initial backfill source choice and current candidate pointer, no torrent sends.

An initial choice is immutable while active. After proven cancellation cleanup,
the existing discovery worker can advance the pointer to the next ranked
candidate. Selection never grants a torrent submission permit.
"""
import time

from src.nextgen.acquisition.prowlarr_search import DiscoveryPolicy, Release, SearchResult, _magnet_hash
from src.nextgen.acquisition.metadata import infohash
from src.nextgen.sonarr_episode import VerifiedEpisode


class SelectionConflict(ValueError):
    reason_code = 'SONARR_SELECTION_CONFLICT'
    def __init__(self):
        super().__init__('Backfill candidate selection needs review.')


def select(db, *, plan_id, episode, identity, result, release, policy):
    """Select AniDB episode `episode` once; duplicate identical attempts are idempotent.

    identity is that episode's own verified TVDB placement (any TVDB season).

    Refuses unverified catalog-only episodes and any existing active acquisition.
    No URL, passkey, or magnet URI is persisted.
    """
    if (not db.in_transaction or type(plan_id) is not int or plan_id <= 0
            or type(episode) is not int or episode <= 0
            or type(identity) is not VerifiedEpisode
            or type(result) is not SearchResult or type(release) is not Release
            or type(policy) is not DiscoveryPolicy
            or not any(candidate is release for candidate in result.candidates)
            or result.tvdb_series_id != identity.tvdb_series_id
            or result.tvdb_episode_id != identity.tvdb_episode_id
            or release.tvdb_episode_id != result.tvdb_episode_id
            or not policy.accepts(release)
            or not release.info_hash
            or _magnet_hash(release.magnet_url) != release.info_hash):
        raise SelectionConflict()
    try: digest = infohash(release.info_hash)
    except ValueError: raise SelectionConflict() from None
    plan = db.execute(
        "SELECT anidb_id,tvdb_series_id,anchor_episode,"
        "backfill_count,following_count FROM trial_plans "
        "WHERE plan_id=? AND tvdb_order='official'", (plan_id,)
    ).fetchone()
    if plan is None or plan[1] != identity.tvdb_series_id:
        raise SelectionConflict()
    aid, _, anchor, backfill, following = plan
    from src.nextgen.trial_policy import Window
    if episode not in Window(backfill, following).anidb_episodes(anchor):
        raise SelectionConflict()
    recognized = db.execute(
        "SELECT anidb_episode_number FROM recognized_episodes WHERE anidb_id=? AND tvdb_series_id=? "
        "AND tvdb_season=? AND tvdb_episode=? AND tvdb_episode_id=?",
        (aid, identity.tvdb_series_id, identity.tvdb_season,
         identity.tvdb_episode, identity.tvdb_episode_id)
    ).fetchone()
    # A recognized AniDB episode must keep its own recognized placement.
    if recognized not in (None, (episode,)) or db.execute(
            "SELECT 1 FROM recognized_episodes WHERE anidb_id=? AND anidb_episode_number=? "
            "AND tvdb_episode_id<>?", (aid, episode, identity.tvdb_episode_id)).fetchone():
        raise SelectionConflict()
    # Catalog-only placements are *hints*, not proof of anime identity.
    # VERIFY_RELEASE must independently resolve them before any reservation.
    if recognized is None:
        conflicting=db.execute(
            "SELECT tvdb_episode_id FROM recognized_episodes WHERE "
            "tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=?",
            (identity.tvdb_series_id,identity.tvdb_season,identity.tvdb_episode)
        ).fetchone()
        if conflicting is not None:
            raise SelectionConflict()
    previous = db.execute(
        "SELECT tvdb_season,tvdb_episode,tvdb_episode_id,info_hash FROM sonarr_episode_selections "
        "WHERE plan_id=? AND anidb_episode_number=?", (plan_id, episode)
    ).fetchone()
    if previous is not None:
        if previous != (identity.tvdb_season, identity.tvdb_episode,
                        identity.tvdb_episode_id, digest):
            raise SelectionConflict()
        return False
    # The existing single and batch reservation namespaces remain authoritative.
    args = (identity.tvdb_series_id, identity.tvdb_season, identity.tvdb_episode)
    for table in ("episode_reservations", "batch_episode_reservations"):
        if db.execute(
            f"SELECT 1 FROM {table} WHERE state='ACTIVE' "
            "AND tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=? LIMIT 1",
            args
        ).fetchone():
            raise SelectionConflict()
    if db.execute("SELECT 1 FROM sonarr_episode_selections WHERE plan_id=? AND info_hash=?",
                  (plan_id,digest)).fetchone():
        raise SelectionConflict()
    db.execute(
        "INSERT INTO sonarr_episode_selections "
        "(plan_id,anidb_episode_number,tvdb_season,tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms) "
        "VALUES(?,?,?,?,?,?,?)",
        (plan_id,episode,identity.tvdb_season,identity.tvdb_episode,
         identity.tvdb_episode_id,digest,time.time_ns()//1_000_000)
    )
    return True


def require_verified_delivery(db, verified):
    """Cross-check an internal backfill event against provider-verified placement.

    This is called by the normal VERIFY_RELEASE persistence BEFORE its release
    verification is saved. A search match cannot bypass AniDB/TVDB.
    """
    from src.nextgen.release_contracts import ReleaseConflict, VerifiedRelease
    from src.nextgen.job_contracts import parse_payload
    if type(verified) is not VerifiedRelease:
        raise ReleaseConflict()
    source=verified.source
    try:
        document=parse_payload(source.raw_body.decode('utf-8'))
        digest=infohash(document.get('torrent_infohash'))
    except (ValueError,TypeError,UnicodeError):
        raise ReleaseConflict() from None
    rows=db.execute(
        "SELECT p.anidb_id,p.tvdb_series_id,s.tvdb_season,"
        "s.tvdb_episode,s.tvdb_episode_id,s.anidb_episode_number "
        "FROM jobs j JOIN jobs parent ON parent.job_id=j.created_by_job_id "
        "JOIN trial_plans p ON p.plan_id=json_extract(parent.payload_json,'$.plan_id') "
        "JOIN sonarr_episode_selections s ON s.plan_id=p.plan_id "
        "WHERE j.event_id=? AND j.kind='VERIFY_RELEASE' "
        "AND parent.kind='DISCOVER_SONARR_BACKFILL' AND s.info_hash=?",
        (source.event_id,digest)).fetchall()
    # The selected AniDB episode and its own placement must both be what
    # VERIFY_RELEASE independently resolved for this delivery.
    if len(rows)!=1 or rows[0]!=(
            source.anidb_id,source.tvdb_series_id,verified.tvdb_season,
            verified.tvdb_episode,verified.tvdb_episode_id,verified.anidb_episode_number):
        raise ReleaseConflict()

def acquisition_identity(database, source):
    """Return the selected episode identity only for an active, verified source.

    Hash-only and episode-scoped. No magnet URL or provider credentials are
    stored in SQLite. The acquisition reservation remains authoritative.
    """
    from src.nextgen.acquisition.contracts import AcquisitionInput
    from src.nextgen.storage.migrations import LATEST_VERSION, _open
    from src.nextgen.storage.job_queue import QueueUnavailable
    if type(source) is not AcquisitionInput:
        raise SelectionConflict()
    db=_open(database,readonly=True,timeout_ms=5000)
    try:
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:
            raise QueueUnavailable()
        rows=db.execute(
            "SELECT p.tvdb_series_id,s.tvdb_episode_id "
            "FROM incoming_events e "
            "JOIN jobs j ON j.event_id=e.event_id AND j.kind='VERIFY_RELEASE' "
            "JOIN jobs parent ON parent.job_id=j.created_by_job_id "
            "JOIN trial_plans p ON p.plan_id=json_extract(parent.payload_json,'$.plan_id') "
            "JOIN sonarr_episode_selections s ON s.plan_id=p.plan_id "
            "JOIN sonarr_episode_candidates c ON c.plan_id=s.plan_id "
            " AND c.anidb_episode_number=s.anidb_episode_number AND c.info_hash=s.info_hash "
            "WHERE e.event_id=? AND e.source='sonarr-discovery' "
            "AND parent.kind='DISCOVER_SONARR_BACKFILL' "
            "AND s.anidb_episode_number=? AND s.tvdb_season=? AND s.tvdb_episode=? "
            "AND s.info_hash=? AND c.state='ACTIVE'",
            (source.event_id,source.anidb_episode_number,source.tvdb_season,
             source.tvdb_episode,source.info_hash)
        ).fetchall()
    finally:
        db.close()
    if not rows:return None
    if len(rows)!=1:raise SelectionConflict()
    return VerifiedEpisode(rows[0][0],rows[0][1],
                           source.tvdb_season,source.tvdb_episode)
