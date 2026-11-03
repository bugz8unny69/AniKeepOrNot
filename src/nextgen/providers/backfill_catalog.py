"""Which TVDB episodes of a verified season can be searched, and as which AniDB number.

Uses the same cached AniDB record, Anime-Lists mapping and TVDB official episode
pages as VERIFY_RELEASE, so search only targets placements that verification
will accept. No release search or acquisition happens here.
"""
import datetime

from src.nextgen.acquisition.prowlarr_search import EpisodeCatalog, EpisodeTarget
from src.nextgen.providers.errors import ProviderAuthenticationFailed, ProviderInvalidResponse
from src.nextgen.providers.http import Credentials
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


def build_catalog(client, aid, tid, season, secret, *, today=None):
    record = client.anime(aid)
    titles = (record['title'], *(t for t in record['titles'] if t != record['title']))
    episodes = client.episodes(aid)
    airdates = client.airdates(aid)
    mappings = forward_mappings(client.mapping_xml(), aid, tid, episodes)
    placements = tvdb_episode_ids(client, tid, secret)
    today = (today or datetime.date.today()).isoformat()
    # Absolute number across regular seasons, as many groups number sequels.
    earlier = sum(1 for (record_season, _) in placements if 1 <= record_season < season)
    targets = {}
    for (record_season, number), ids in placements.items():
        if record_season != season: continue
        anidb = [a for a, placement in mappings.items() if placement == (season, number)]
        if len(ids) != 1 or len(anidb) != 1:
            targets[number] = EpisodeTarget(number, 'MAPPING_MISSING')
            continue
        aired = airdates.get(anidb[0])
        status = 'SEARCHABLE' if aired is not None and aired <= today else 'NOT_AIRED'
        targets[number] = EpisodeTarget(number, status, anidb[0], ids[0],
                                        earlier + number if season >= 1 else None)
    english_title = record.get('english_title')
    return EpisodeCatalog(titles, targets, english_title=english_title)


def make_catalog_factory(options, credentials, *, client_pool):
    """configuration -> catalog(aid, tid, season), sharing the worker's provider pool."""
    def for_configuration(configuration):
        def catalog(aid, tid, season):
            actual = options(configuration) if callable(options) else options
            secret = credentials()
            if type(secret) is not Credentials: raise ProviderAuthenticationFailed()
            client = client_pool.get(configuration.value('storage.database_path'), actual)
            return build_catalog(client, aid, tid, season, secret)
        return catalog
    return for_configuration
