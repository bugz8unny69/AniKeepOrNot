"""Trusted internal backfill delivery, routed into the ORIGINAL acquisition pipeline.

Called only in the backfill worker's fenced SQLite completion transaction.
No download, extra torrent submission implementation, or caller-supplied event.
"""
import json
import time

from src.anime.acquisition.prowlarr_search import CandidateSelection
from src.anime.history import append_event_history
from src.anime.job_contracts import parse_payload
from src.anime.storage.sqlite import enqueue_job_with_receipt, persist_identity
from src.anime.storage.release_selection import SelectionConflict


def dispatch(db, claim, plan, choice):
    """Turn one verified source choice into an auditable internal delivery.

    Identity is copied from the episode's *already verified* source and
    revalidated by persist_identity. The normal VERIFY_RELEASE handler still
    checks the release title against the recognized episode before reservation.
    """
    if (not db.in_transaction or type(choice) is not CandidateSelection
            or claim.event_id is None or claim.kind != 'DISCOVER_EPISODE_RELEASES'
            or choice.identity.tvdb_series_id != plan.tvdb_series_id):
        raise SelectionConflict()
    return _dispatch_verified(db, claim, plan, choice.identity,
                              choice.release.declared_title, choice.release.title,
                              choice.release.info_hash, choice.release.magnet_url)


def dispatch_cached(db, claim, plan, identity, source):
    """Replay a verified historical source as a NEW internal delivery."""
    from src.anime.storage.cached_release_retry import CachedTorrent
    if (type(source) is not CachedTorrent or not db.in_transaction
            or claim.kind!='DISCOVER_EPISODE_RELEASES' or claim.event_id is None
            or identity.tvdb_series_id!=plan.tvdb_series_id):
        raise SelectionConflict()
    return _dispatch_verified(db,claim,plan,identity,source.declared_title,
                              source.title,source.info_hash,source.magnet_uri)


def _dispatch_verified(db,claim,plan,identity,declared_title,title,digest,source_magnet):
    row=db.execute(
        "SELECT i.series_json, r.anidb_episode_number "
        "FROM recognized_episodes e "
        "JOIN release_verifications r ON r.verification_id=e.evidence_verification_id "
        "JOIN identity_results i ON i.result_id=r.identity_result_id "
        "WHERE e.anidb_id=? AND e.tvdb_series_id=? AND e.tvdb_season=? "
        "AND e.tvdb_episode=? AND e.tvdb_episode_id=? "
        "AND r.anidb_id=e.anidb_id AND r.tvdb_series_id=e.tvdb_series_id "
        "AND r.tvdb_episode_id=e.tvdb_episode_id",
        (plan.anidb_id,identity.tvdb_series_id,identity.tvdb_season,
         identity.tvdb_episode,identity.tvdb_episode_id)).fetchone()
    if row is None:
        # Newly discovered backfill episodes have no incoming verification yet.
        # Copy only the already verified series identity from the frozen anchor;
        # never its TVDB season. VERIFY_RELEASE still checks this episode.
        row=db.execute(
            "SELECT i.series_json, NULL FROM user_evaluation_plans p "
            "JOIN release_verifications r ON r.verification_id=p.anchor_verification_id "
            "JOIN identity_results i ON i.result_id=r.identity_result_id "
            "WHERE p.plan_id=? AND p.anidb_id=r.anidb_id "
            "AND p.tvdb_series_id=r.tvdb_series_id",
            (plan.plan_id,)
        ).fetchone()
    if row is None: raise SelectionConflict()
    try:
        # The release declaration is rechecked by VERIFY_RELEASE and then the
        # manifest filename gate before any stopped submission is authorized.
        from src.anime.providers.release_mapping import declared_episode
        origin,season,episode=declared_episode(declared_title)
        if ((origin=='tvdb' and (season,episode)!=(identity.tvdb_season,identity.tvdb_episode))
                or (origin=='anidb' and row[1] is not None and episode!=row[1])):
            raise SelectionConflict()
        validated=parse_payload(row[0])
        if (validated.get('anime',{}).get('anidb_id')!=plan.anidb_id
                or validated['anime'].get('tvdb_series_id')!=plan.tvdb_series_id):
            raise SelectionConflict()
        # The sanitized, hash-bound magnet travels with the delivery exactly
        # like a webhook magnet; qBittorrent resolves it downstream.
        from src.anime.acquisition.metadata import hash_magnet
        magnet=hash_magnet(digest,source_magnet)
        if magnet is None: raise SelectionConflict()
        # VERIFY_RELEASE reads the plain declared title; the group's own
        # title is kept for diagnostics only.
        body=json.dumps({'release_name':declared_title,
                         'source_release_name':title,
                         'torrent_infohash':digest,
                         'magnet_uri':magnet},
                        sort_keys=True,separators=(',',':')).encode('utf-8')
        if len(body)>65536: raise SelectionConflict()
    except (ValueError,TypeError,KeyError,AttributeError):
        raise SelectionConflict() from None
    now=time.time_ns()//1_000_000
    event_id=db.execute(
        "INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES(?,?,?)",
        ('release-discovery',body,now)).lastrowid
    receipt=append_event_history(db,event_id=event_id,event_code='ACCEPTED',
                                 component='worker',occurred_at_ms=now)
    # Publish already verified anime identity for the NEW, transparently
    # marked delivery. It is NOT a copy of the old torrent or old event.
    _,audit=persist_identity(db,event_id,validated,validated_at_ms=now)
    job_id,created=enqueue_job_with_receipt(
        db,kind='VERIFY_RELEASE',payload={'anidb_id':plan.anidb_id},
        component='worker',event_id=event_id,created_by_job_id=claim.job_id,
        # Each chosen torrent has its own cleanup/cancellation scope.
        operation_id=None)
    return (receipt, *((audit,) if audit is not None else ()), created)
