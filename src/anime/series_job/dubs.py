"""AKON finds dub releases for Sonarr's missing episodes; Sonarr downloads them (part of Keep).

Owner, 2026-11-23: Sonarr searches indexers for the series name and episode
only, so the dub releases AKON's search finds (it adds dub and dual-audio
terms) rarely reach Sonarr. For each monitored, aired episode Sonarr is still
missing (and not already downloading), AKON searches Prowlarr with its rules
and pushes the best release to Sonarr (POST /api/v3/release/push). Sonarr judges
it with its own profile and, if it passes, grabs it. A rejected release is
logged with Sonarr's reason and the next candidate is tried. Only when the
audio check is japanese_english (series_job/keep.py decides).
"""
import logging

TRIES_PER_EPISODE = 3
PAGE = 4          # Prowlarr backfill takes at most four episodes per call.
LOG = logging.getLogger('akon.series')


def why_not(prowlarr, number):
    """Why AKON's rules accepted none of an episode's results, from the search's in-memory snapshot."""
    from collections import Counter
    rows = [row for snapshot in getattr(prowlarr, 'snapshots', ())
            if snapshot.get('anidb_episode') == number for row in snapshot.get('results') or ()]
    reasons = Counter(check for row in rows for check in row.get('failed_checks') or ()
                      if check != 'PASSES_POLICY')
    if not reasons:
        return ''
    policy = prowlarr.policy
    return (f'; {len(rows)} results, failed: ' + ', '.join(f'{check} x{n}' for check, n in reasons.most_common(4))
            + f' (AKON needs >= {policy.min_seeders} seeders, <= {policy.max_resolution}p'
            + (', a Dual Audio tag' if policy.prefer_audio_tag else '') + ')')


def find_and_push(sonarr, prowlarr, catalog, anidb_id, tvdb_series_id, mapping=None):
    """Push a dub release to Sonarr for each missing episode. Returns (grabbed, not found) episode labels.

    mapping: AniDB episode number -> (TVDB season, episode) from Anime-Lists. Matching
    Sonarr's episodes needs only that, not TVDB's episode IDs (paced, often deferred).
    """
    series = sonarr.series(tvdb_series_id)
    if series is None:
        LOG.info('Sonarr has no series with TVDB %s (AniDB %s); nothing to find', tvdb_series_id, anidb_id)
        return [], []
    missing = sonarr.missing_episodes(series['id'])
    if not missing:
        LOG.info('Sonarr is missing no episodes of %s', series['title'])
        return [], []
    mapping = mapping or {}
    placed = {number: (target.tvdb_season, target.tvdb_episode) if target.tvdb_season is not None
              else mapping.get(number)
              for number, target in catalog(anidb_id, tvdb_series_id).episodes.items()
              if target.status == 'SEARCHABLE'}
    targets = {number: place for number, place in placed.items() if place in missing}
    LOG.info('Sonarr is missing %s episode(s) of %s; AKON searches for dubs of %s', len(missing),
             series['title'], len(targets))
    numbers, grabbed, pushed = sorted(targets), [], set()
    for start in range(0, len(numbers), PAGE):
        for found in prowlarr.backfill(anidb_id, tvdb_series_id, tuple(numbers[start:start + PAGE]),
                                       select_candidates=True):
            season, episode = targets[found.number]
            label = f"{series['title']} S{season:02d}E{episode:02d}"
            if found.selection is None:
                LOG.info('%s: no dub release found (%s)%s', label, found.status, why_not(prowlarr, found.number))
                continue
            accepted = [r for r in found.selection.result.candidates if prowlarr.policy.accepts(r)]
            for release in accepted[:TRIES_PER_EPISODE]:
                if release.info_hash in pushed:
                    grabbed.append(label)        # A pack already pushed for an earlier episode.
                    break
                rejections = sonarr.push_release(release.title, release.magnet_url, release.info_hash,
                                                 release.indexer, release.size, release.seeders)
                if not rejections:
                    pushed.add(release.info_hash)
                    grabbed.append(label)
                    LOG.info('%s: Sonarr grabbed %s', label, release.title)
                    break
                LOG.info('%s: Sonarr rejected %s: %s', label, release.title, '; '.join(rejections))
    not_found = [f"S{s:02d}E{e:02d}" for n, (s, e) in sorted(targets.items())
                 if f"{series['title']} S{s:02d}E{e:02d}" not in grabbed]
    return grabbed, not_found


def mappings(pool, configuration, anidb_id, tvdb_series_id):
    """AniDB episode number -> (TVDB season, episode) from the cached Anime-Lists map; no TVDB call."""
    from src.anime.providers.configuration import options_from_configuration
    from src.anime.providers.release_mapping import forward_mappings
    client = pool.get(configuration.value('storage.database_path'), options_from_configuration(configuration))
    return forward_mappings(client.mapping_xml(), anidb_id, tvdb_series_id, client.episodes(anidb_id))
