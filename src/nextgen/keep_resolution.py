"""Resolve verified TVDB placement at Keep time for unmapped staged episodes."""
import time
from src.nextgen.providers.release_mapping import forward_mappings
from src.nextgen.providers.http import Options
from src.nextgen.providers.identity import ClientPool, Clients
from src.nextgen.providers.configuration import options_from_configuration
from src.nextgen.secrets import Credentials


def resolve_tvdb_placement(db, aid, eid, tid, target_job_id, *, client=None, secret=None):
    """Attempt late TVDB placement resolution for an episode staged without TVDB IDs.

    Returns (tvdb_episode_id, tvdb_season, tvdb_episode) if resolved, or None if still unavailable.
    When resolved, commits placement to release_verifications, episode_reservations,
    sonarr_episode_selections, recognized_seasons, and recognized_episodes.
    """
    if client is None or secret is None or type(secret) is not Credentials:
        return None

    # Determine AniDB episode number
    row = db.execute(
        "SELECT anidb_episode_number FROM release_verifications WHERE anidb_id=? AND anidb_episode_id=?",
        (aid, eid)
    ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT episode_number FROM identified_episodes WHERE anidb_id=? AND anidb_episode_id=?",
            (aid, eid)
        ).fetchone()
    if row is None:
        return None
    anidb_number = row[0]

    try:
        catalog = client.episodes(aid)
        mappings = forward_mappings(client.mapping_xml(), aid, tid, catalog)
    except Exception:
        return None

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
    except Exception:
        return None

    if tvdb_eid is None:
        return None

    now_ms = time.time_ns() // 1000000

    # Commit placement to release_verifications
    db.execute(
        "UPDATE release_verifications SET tvdb_season=?, tvdb_episode=?, tvdb_episode_id=? "
        "WHERE anidb_id=? AND anidb_episode_id=? AND tvdb_episode_id IS NULL",
        (target_season, target_episode, tvdb_eid, aid, eid)
    )

    # Commit placement to active episode_reservations
    db.execute(
        "UPDATE episode_reservations SET tvdb_season=?, tvdb_episode=?, tvdb_episode_id=? "
        "WHERE anidb_id=? AND anidb_episode_id=? AND state='ACTIVE' AND tvdb_episode_id IS NULL",
        (target_season, target_episode, tvdb_eid, aid, eid)
    )

    # Commit placement to sonarr_episode_selections
    db.execute(
        "UPDATE sonarr_episode_selections SET tvdb_season=?, tvdb_episode=?, tvdb_episode_id=? "
        "WHERE anidb_episode_number=? AND tvdb_episode_id IS NULL",
        (target_season, target_episode, tvdb_eid, anidb_number)
    )

    # Insert into recognized_seasons and recognized_episodes
    anime = db.execute("SELECT title FROM identified_anime WHERE anidb_id=?", (aid,)).fetchone()
    display_title = anime[0] if anime else f"Anime {aid}"
    ver_row = db.execute(
        "SELECT verification_id FROM release_verifications WHERE anidb_id=? AND anidb_episode_id=?",
        (aid, eid)
    ).fetchone()
    ver_id = ver_row[0] if ver_row else None

    db.execute(
        "INSERT OR IGNORE INTO recognized_seasons "
        "(tvdb_series_id,tvdb_season,anidb_id,display_title,evidence_verification_id,recognized_at_ms) "
        "VALUES(?,?,?,?,?,?)",
        (tid, target_season, aid, display_title, ver_id, now_ms)
    )

    db.execute(
        "INSERT OR IGNORE INTO recognized_episodes "
        "(tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,anidb_episode_id,tvdb_episode,tvdb_episode_id,evidence_verification_id,recognized_at_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (tid, target_season, aid, anidb_number, eid, target_episode, tvdb_eid, ver_id, now_ms)
    )

    return (tvdb_eid, target_season, target_episode)


def make_keep_resolver(configuration, secrets, *, client_pool=None):
    """Create a resolver function bound to configuration and secrets."""
    def resolve(db, aid, eid, tid, target_job_id):
        try:
            actual = options_from_configuration(configuration)
            secret = secrets.tvdb_credentials() if secrets is not None else None
            if not isinstance(secret, Credentials):
                return None
            pool = client_pool or ClientPool(Clients)
            client = pool.get(configuration.value('storage.database_path'), actual)
            return resolve_tvdb_placement(db, aid, eid, tid, target_job_id, client=client, secret=secret)
        except Exception:
            return None
    return resolve
