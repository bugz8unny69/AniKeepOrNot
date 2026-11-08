"""Resolve verified TVDB placement at Keep time for unmapped staged episodes."""
import time
import urllib.error
from src.nextgen.providers.release_mapping import forward_mappings
from src.nextgen.providers.identity import ClientPool, Clients
from src.nextgen.providers.configuration import options_from_configuration
from src.nextgen.providers.errors import (
    ProviderUnavailable, ProviderAuthenticationFailed, ProviderConfigurationError,
)
from src.nextgen.release_contracts import ReleaseNotReady, ReleaseConflict
from src.nextgen.secrets import Credentials
from src.nextgen.sonarr import KeepReview, KeepUnavailable, KeepConfiguration


def resolve_tvdb_placement(db, aid, eid, tid, target_job_id, *, client=None, secret=None):
    """Attempt late TVDB placement resolution for an episode staged without TVDB IDs.

    Returns (tvdb_episode_id, tvdb_season, tvdb_episode) if resolved, or None if still unavailable.
    When resolved, commits placement to release_verifications, episode_reservations,
    sonarr_episode_selections, recognized_seasons, and recognized_episodes.
    """
    if client is None:
        raise KeepUnavailable()
    if secret is None or type(secret) is not Credentials:
        raise KeepConfiguration()

    # Determine AniDB episode number
    row = db.execute(
        "SELECT anidb_episode_number FROM release_verifications WHERE anidb_id=? AND anidb_episode_id=?",
        (aid, eid)
    ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT anidb_episode_number FROM batch_verified_files WHERE anidb_episode_id=?",
            (eid,)
        ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT anidb_episode_number FROM recognized_episodes WHERE anidb_id=? AND anidb_episode_id=?",
            (aid, eid)
        ).fetchone()
    anidb_number = row[0] if row is not None else None

    try:
        catalog = client.episodes(aid)
        if anidb_number is None and isinstance(catalog, dict):
            for num, ep_id in catalog.items():
                if ep_id == eid:
                    anidb_number = num
                    break
        if anidb_number is None:
            return None
        mappings = forward_mappings(client.mapping_xml(), aid, tid, catalog)
    except (ProviderUnavailable, TimeoutError, OSError, urllib.error.URLError) as exc:
        raise KeepUnavailable() from exc
    except (ProviderAuthenticationFailed, ProviderConfigurationError) as exc:
        raise KeepConfiguration() from exc
    except ReleaseNotReady:
        return None
    except ReleaseConflict as exc:
        raise KeepReview(failed_check='src/nextgen/keep_resolution.py:51 resolve_tvdb_placement MAPPING_CONFLICT') from exc

    target = mappings.get(anidb_number)
    if target is None:
        return None
    target_season, target_episode = target

    tvdb_eid = None
    seen = set()
    try:
        for page in range(20):
            records, has_next = client.episode_page(tid, page, secret)
            for row_eid, s, ep in records:
                if row_eid in seen:
                    return None
                seen.add(row_eid)
                if (s, ep) == (target_season, target_episode):
                    tvdb_eid = row_eid
                    break
            if tvdb_eid is not None or not has_next:
                break
    except (ProviderUnavailable, TimeoutError, OSError, urllib.error.URLError) as exc:
        raise KeepUnavailable() from exc
    except (ProviderAuthenticationFailed, ProviderConfigurationError) as exc:
        raise KeepConfiguration() from exc

    if tvdb_eid is None:
        return None

    now_ms = time.time_ns() // 1000000

    # Commit placement to release_verifications
    db.execute(
        "UPDATE release_verifications SET tvdb_season=?, tvdb_episode=?, tvdb_episode_id=? "
        "WHERE anidb_id=? AND anidb_episode_id=? AND tvdb_episode_id IS NULL",
        (target_season, target_episode, tvdb_eid, aid, eid)
    )

    # Resolve plan IDs: single trial plan vs batch plan handled separately
    user_evaluation_plan_id = None
    batch_plan_id = None

    if target_job_id:
        # Resolve trial_plans.plan_id through acquisition_plans and episode_reservations
        row = db.execute(
            "SELECT r.plan_id FROM staging_publications sp "
            "JOIN acquisition_plans ap ON ap.plan_id = sp.plan_id "
            "JOIN episode_reservations r ON r.reservation_id = ap.reservation_id "
            "WHERE sp.job_id=?",
            (target_job_id,)
        ).fetchone()
        if row is not None:
            user_evaluation_plan_id = row[0]
        else:
            # Check if this target_job_id belongs to batch staging publications
            batch_row = db.execute(
                "SELECT plan_id FROM batch_staging_publications WHERE job_id=?",
                (target_job_id,)
            ).fetchone()
            if batch_row is not None:
                batch_plan_id = batch_row[0]

    if user_evaluation_plan_id is None and batch_plan_id is None:
        # Fall back to active reservations for this exact episode
        res_row = db.execute(
            "SELECT plan_id FROM episode_reservations "
            "WHERE anidb_id=? AND anidb_episode_id=? AND state='ACTIVE'",
            (aid, eid)
        ).fetchone()
        if res_row is not None and res_row[0] is not None:
            user_evaluation_plan_id = res_row[0]
        else:
            batch_res_row = db.execute(
                "SELECT plan_id FROM batch_episode_reservations "
                "WHERE anidb_id=? AND anidb_episode_id=? AND state='ACTIVE'",
                (aid, eid)
            ).fetchone()
            if batch_res_row is not None and batch_res_row[0] is not None:
                batch_plan_id = batch_res_row[0]

    if user_evaluation_plan_id is None and batch_plan_id is None:
        raise KeepReview(failed_check='src/nextgen/keep_resolution.py:125 resolve_tvdb_placement UNRESOLVED_PLAN')

    # Commit placement to active reservations and selections for the resolved plan type
    if user_evaluation_plan_id is not None:
        db.execute(
            "UPDATE episode_reservations SET tvdb_season=?, tvdb_episode=?, tvdb_episode_id=? "
            "WHERE plan_id=? AND anidb_id=? AND anidb_episode_id=? AND state='ACTIVE' AND tvdb_episode_id IS NULL",
            (target_season, target_episode, tvdb_eid, user_evaluation_plan_id, aid, eid)
        )
        existing_sel = db.execute(
            "SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM sonarr_episode_selections "
            "WHERE plan_id=? AND anidb_episode_number=?",
            (user_evaluation_plan_id, anidb_number)
        ).fetchone()
        if existing_sel is not None:
            curr_s, curr_ep, curr_eid = existing_sel
            if curr_eid is not None and (curr_s, curr_ep, curr_eid) != (target_season, target_episode, tvdb_eid):
                raise KeepReview(failed_check='src/nextgen/keep_resolution.py:140 resolve_tvdb_placement SELECTION_MISMATCH')
        db.execute(
            "UPDATE sonarr_episode_selections SET tvdb_season=?, tvdb_episode=?, tvdb_episode_id=? "
            "WHERE plan_id=? AND anidb_episode_number=? AND tvdb_episode_id IS NULL",
            (target_season, target_episode, tvdb_eid, user_evaluation_plan_id, anidb_number)
        )

    if batch_plan_id is not None:
        db.execute(
            "UPDATE batch_episode_reservations SET tvdb_season=?, tvdb_episode=?, tvdb_episode_id=? "
            "WHERE plan_id=? AND anidb_id=? AND anidb_episode_id=? AND state='ACTIVE'",
            (target_season, target_episode, tvdb_eid, batch_plan_id, aid, eid)
        )

    # Insert into recognized_seasons and recognized_episodes
    anime = db.execute("SELECT title FROM identified_anime WHERE anidb_id=?", (aid,)).fetchone()
    display_title = anime[0] if anime else f"Anime {aid}"
    ver_row = db.execute(
        "SELECT verification_id FROM release_verifications WHERE anidb_id=? AND anidb_episode_id=?",
        (aid, eid)
    ).fetchone()
    ver_id = ver_row[0] if ver_row else None

    if ver_id is not None:
        db.execute(
            "INSERT OR IGNORE INTO recognized_seasons "
            "(tvdb_series_id,tvdb_season,anidb_id,display_title,evidence_verification_id,recognized_at_ms) "
            "VALUES(?,?,?,?,?,?)",
            (tid, target_season, aid, display_title, ver_id, now_ms)
        )

        # Explicit conflict check on recognized_episodes before inserting
        existing_rec = db.execute(
            "SELECT tvdb_series_id, tvdb_season, tvdb_episode, tvdb_episode_id FROM recognized_episodes "
            "WHERE anidb_id=? AND anidb_episode_number=?",
            (aid, anidb_number)
        ).fetchone()
        if existing_rec is not None:
            if existing_rec != (tid, target_season, target_episode, tvdb_eid):
                raise KeepReview()
        else:
            slot_clash = db.execute(
                "SELECT anidb_id, anidb_episode_number FROM recognized_episodes "
                "WHERE tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=?",
                (tid, target_season, target_episode)
            ).fetchone()
            if slot_clash is not None and (slot_clash[0], slot_clash[1]) != (aid, anidb_number):
                raise KeepReview()

            db.execute(
                "INSERT INTO recognized_episodes "
                "(tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,anidb_episode_id,"
                "tvdb_episode,tvdb_episode_id,evidence_verification_id,recognized_at_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, target_season, aid, anidb_number, eid, target_episode, tvdb_eid, ver_id, now_ms)
            )

    return (tvdb_eid, target_season, target_episode)


def make_keep_resolver(configuration, secrets, *, client_pool=None):
    """Create a resolver function bound to configuration and secrets."""
    def resolve(db, aid, eid, tid, target_job_id):
        actual = options_from_configuration(configuration)
        if secrets is None:
            raise KeepConfiguration()
        try:
            secret = secrets.tvdb_credentials()
        except ProviderConfigurationError as exc:
            raise KeepConfiguration() from exc
        if not isinstance(secret, Credentials):
            raise KeepConfiguration()
        pool = client_pool or ClientPool(Clients)
        client = pool.get(configuration.value('storage.database_path'), actual)
        return resolve_tvdb_placement(db, aid, eid, tid, target_job_id, client=client, secret=secret)
    return resolve
