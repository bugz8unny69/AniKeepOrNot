"""After a Keep: AKON finds dub releases for Sonarr's missing episodes; Sonarr downloads them.

Owner, 2026-11-23: Sonarr searches indexers for the series name and episode
only, so the dub releases AKON's search finds (it adds dub and dual-audio
terms) rarely reach Sonarr. This job asks Sonarr which monitored, aired
episodes are still missing (and not already downloading), searches Prowlarr
with AKON's rules for each, and pushes the best release to Sonarr
(`POST /api/v3/release/push`). Sonarr judges it with its own profile and, if
it passes, grabs and imports it as if its own search had found it. A rejected
release is logged with Sonarr's reason and the next candidate is tried.

Only when the audio check is japanese_english: otherwise Sonarr's own search is
enough and the job does nothing.

It starts DELAY_MS after the Keep, so Sonarr's own search (sent by the Keep)
finishes first and nothing is grabbed twice.
"""
from functools import partial
import logging
import time

from src.anime.acquisition.audio import wants_english
from src.anime.acquisition.prowlarr_search import (Client as Prowlarr, SearchConfiguration, SearchReview,
                                                   SearchUnavailable)
from src.anime.async_io import blocking
from src.anime.handlers import HandlerDefinition
from src.anime.providers.errors import ProviderDeferred, ProviderUnavailable
from src.anime.job_contracts import Outcome, Registration
from src.anime.services.base import service_facts
from src.anime.sonarr import KeepConfiguration, KeepReview, KeepUnavailable, api_client
from src.anime.storage.sqlite import enqueue_job

KIND = 'FIND_DUBS_FOR_SONARR'
SERVICES = ('sonarr', 'prowlarr')
DEFINITION = HandlerDefinition(KIND, "Find dub releases for Sonarr's missing episodes")
DELAY_MS = 10 * 60_000
TRIES_PER_EPISODE = 3
PAGE = 4          # Prowlarr backfill takes at most four episodes per call.
LOG = logging.getLogger('anime.worker')


def enqueue(db, anidb_id, tvdb_series_id, *, claim=None, delay_ms=DELAY_MS):
    """Queue the dub search for a series (one waiting at a time), inside the caller's transaction."""
    waiting = db.execute("SELECT job_id FROM jobs WHERE kind=? AND state IN ('PENDING','RETRY_WAIT') "
                         "AND json_extract(payload_json,'$.anidb_id')=?", (KIND, anidb_id)).fetchone()
    if waiting:
        return waiting[0]
    return enqueue_job(db, kind=KIND, payload={'anidb_id': anidb_id, 'tvdb_series_id': tvdb_series_id},
                       component='worker', event_id=None if claim is None else claim.event_id,
                       created_by_job_id=None if claim is None else claim.job_id,
                       available_at_ms=time.time_ns() // 1_000_000 + delay_ms)


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


def registration(secrets, *, catalog_factory=None, prowlarr_factory=Prowlarr.from_context, sonarr_factory=api_client,
                 mapping_factory=None):
    from src.anime.providers.identity import ClientPool
    pool = ClientPool()
    if catalog_factory is None:
        from src.anime.providers.backfill_catalog import make_catalog_factory
        from src.anime.providers.configuration import episode_placement_credentials, options_from_configuration
        catalog_factory = make_catalog_factory(options_from_configuration,
                                               partial(episode_placement_credentials, secrets), client_pool=pool)
    if mapping_factory is None:
        mapping_factory = partial(mappings, pool)

    async def execute(context):
        payload = context.data()
        anidb_id, tvdb_series_id = payload['anidb_id'], payload['tvdb_series_id']
        audio = context.configuration.value('acquisition.audio_check')
        if not wants_english(audio):
            # Only dubs need AKON's help; Sonarr's own search finds the rest (owner, 2026-11-23).
            LOG.info('Dub search for AniDB %s skipped: the audio check is %s, not japanese_english', anidb_id, audio)
            return Outcome()
        catalog = catalog_factory(context.configuration)
        try:
            sonarr = sonarr_factory(context.configuration, secrets.sonarr_api_key())
            prowlarr = prowlarr_factory(context.configuration, secrets, catalog=catalog)
            prowlarr.logger = LOG
            prowlarr.set_context(anidb_id=anidb_id, tvdb_series_id=tvdb_series_id, search_mode='sonarr_missing')
            mapping = await blocking(mapping_factory, context.configuration, anidb_id, tvdb_series_id)
            grabbed, not_found = await blocking(find_and_push, sonarr, prowlarr, catalog, anidb_id, tvdb_series_id,
                                                mapping)
        except KeepConfiguration:
            return Outcome('review', 'SERVICE_SETUP_INVALID', facts=service_facts('sonarr', 'NOT_CONFIGURED'))
        except KeepUnavailable:
            return Outcome('retry', 'SERVICE_UNAVAILABLE', facts=service_facts('sonarr'))
        except SearchConfiguration:
            return Outcome('review', 'SERVICE_SETUP_INVALID', facts=service_facts('prowlarr', 'NOT_CONFIGURED'))
        except SearchUnavailable:
            return Outcome('retry', 'SERVICE_UNAVAILABLE', facts=service_facts('prowlarr'))
        except ProviderDeferred as exc:
            return Outcome('defer', 'JOB_WAITING', delay_ms=exc.delay_ms)      # AniDB pacing.
        except ProviderUnavailable:
            return Outcome('retry', 'SERVICE_UNAVAILABLE', facts=service_facts('anidb'))
        LOG.info('Dub search for AniDB %s done: Sonarr grabbed %s; nothing usable for %s',
                 anidb_id, len(grabbed), ', '.join(not_found) or 'none')
        return Outcome()
    return Registration(KIND, DEFINITION, execute, services=SERVICES)
