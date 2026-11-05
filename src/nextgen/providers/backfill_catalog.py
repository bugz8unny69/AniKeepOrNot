"""Which AniDB episodes can be searched, and at which verified TVDB placement.

Uses the same cached AniDB record, Anime-Lists mapping and TVDB official episode
pages as VERIFY_RELEASE, so search only targets placements that verification
will accept. No release search or acquisition happens here.
"""
import datetime

from src.nextgen.acquisition.prowlarr_search import EpisodeCatalog, EpisodeTarget
from src.nextgen.providers.errors import ProviderInvalidResponse
from src.nextgen.providers.release_mapping import forward_mappings


def tvdb_episode_ids(client, tid, secret, cancelled=None):
    """(season, number) -> TVDB episode ID across all official pages; duplicates fail closed."""
    placements, seen, total = {}, set(), 0
    for page in range(20):
        if cancelled is not None and cancelled.is_set():
            raise ProviderInvalidResponse()
        records, has_next = client.episode_page(tid, page, secret)
        total += len(records)
        if total > 10000: raise ProviderInvalidResponse()
        for eid, season, number in records:
            if eid in seen: raise ProviderInvalidResponse()
            seen.add(eid)
            placements.setdefault((season, number), []).append(eid)
        if not has_next: break
    else:
        raise ProviderInvalidResponse()
    return placements


def build_catalog(client, aid, tid, secret=None, *, today=None):
    """AniDB episode number -> EpisodeTarget with its own TVDB placement.

    AniDB decides which episodes exist and have aired. TVDB placement is
    attached when available; otherwise aired episodes are SEARCHABLE without TVDB IDs.
    """
    record = client.anime(aid)
    titles = (record['title'], *(t for t in record['titles'] if t != record['title']))
    episodes = client.episodes(aid)
    airdates = client.airdates(aid)
    mappings = {}
    placements = {}
    if tid is not None:
        try:
            mappings = forward_mappings(client.mapping_xml(), aid, tid, episodes)
        except Exception:
            mappings = {}
        try:
            placements = tvdb_episode_ids(client, tid, secret)
        except Exception:
            placements = {}
    today = (today or datetime.date.today()).isoformat()
    targets = {}
    for number in episodes:
        aired = airdates.get(number)
        if aired is None or aired > today:
            targets[number] = EpisodeTarget(None, 'NOT_AIRED', number)
            continue
        placement = mappings.get(number)
        ids = placements.get(placement, ()) if placement else ()
        if (placement is None or len(ids) != 1
                or sum(1 for other in mappings.values() if other == placement) != 1):
            targets[number] = EpisodeTarget(None, 'SEARCHABLE', number)
            continue
        season, episode = placement
        # Absolute number across regular seasons, as many groups number sequels.
        earlier = sum(1 for (record_season, _) in placements if 1 <= record_season < season)
        targets[number] = EpisodeTarget(episode, 'SEARCHABLE', number, ids[0],
                                        earlier + episode if season >= 1 else None, season)
    english_title = record.get('english_title')
    return EpisodeCatalog(titles, targets, english_title=english_title)


def make_catalog_factory(options, credentials=None, *, client_pool):
    """configuration -> catalog(aid, tid), sharing the worker's provider pool."""
    def for_configuration(configuration):
        def catalog(aid, tid):
            actual = options(configuration) if callable(options) else options
            secret = None
            if callable(credentials):
                try: secret = credentials()
                except Exception: secret = None
            elif credentials is not None:
                secret = credentials
            client = client_pool.get(configuration.value('storage.database_path'), actual)
            return build_catalog(client, aid, tid, secret)
        return catalog
    return for_configuration
