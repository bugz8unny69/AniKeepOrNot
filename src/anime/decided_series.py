"""A delivery for a decided series (Accepted or Rejected) is refused (work item P4).

At the webhook the series is found from local data only (cached_anidb_id); a
series never seen before always goes through the normal pipeline. Every
refusal is an API_REJECTION notification, so a wrong one can be fixed from
the bell (Process anyway, Add torrent/magnet, Open series).
"""
from types import SimpleNamespace

from src.anime.release_names import norm

REASONS = {'ACCEPT': 'ANIME_ACCEPTED', 'REJECT': 'ANIME_REJECTED'}
WORDS = {'ACCEPT': 'accepted (Sonarr handles it)', 'REJECT': 'rejected'}


def release_name(raw_body):
    from src.anime.providers.identity import delivery_title
    from src.anime.storage.sqlite import IdentityNotReady
    try:
        return delivery_title(SimpleNamespace(raw_body=raw_body))
    except IdentityNotReady:
        return None


def webhook_decision(configuration, raw_body):
    """(anidb_id, 'ACCEPT'|'REJECT') when the delivery is for a decided series, else None."""
    from src.anime.providers.configuration import options_from_configuration
    from src.anime.providers.identity import cached_anidb_id
    from src.anime.storage.job_queue import JobQueue
    from src.anime.storage.review_decisions import decision
    name = release_name(raw_body)
    if name is None:
        return None
    database = configuration.value('storage.database_path')
    try:
        options = options_from_configuration(configuration)
    except (ValueError, TypeError):
        options = None                      # manual matches and Recognized names still apply
    try:
        threshold = int(configuration.value('identity.match_threshold_pct'))
    except (ValueError, TypeError):
        threshold = 90
    aid = cached_anidb_id(database, options, name, threshold_pct=threshold)
    if aid is None:
        return None
    with JobQueue(database).connection() as db:
        found = decision(db, aid)
    return None if found is None else (aid, found)


def notify_refused(db, anidb_id, found, raw_body, *, job_id=None, now=None):
    """The API_REJECTION bell item for one refused delivery."""
    from src.anime.notifications import notify
    name = release_name(raw_body) or 'A delivery'
    row = db.execute('SELECT title FROM identified_anime WHERE anidb_id=?', (anidb_id,)).fetchone()
    anime = row[0].strip()[:80] if row else f'AniDB {anidb_id}'
    notify(db, {'type': 'API_REJECTION', 'title': f'Not processed: {anime}'[:120],
                'message': f'{name[:300]} was not processed: {anime} was {WORDS[found]}.'[:500],
                'anidb_id': anidb_id, 'job_id': job_id, 'release_name': name[:1024],
                'group_key': ('release:' + (norm(name) or name))[:200], 'raw_body': raw_body}, now=now)
