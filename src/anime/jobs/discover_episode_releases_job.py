"""Backfill/continue discovery through Prowlarr; no Sonarr, grabs or automatic waiting.

The job kind keeps its historical name so existing jobs and history stay valid.
"""
from src.anime.acquisition.audio import dual_audio_tagged, wants_english
from src.anime.acquisition.episode_context import EpisodeContextConflict
from src.anime.acquisition.prowlarr_search import (
    Client, CandidateSelection, SearchConfiguration, SearchUnauthorized, SearchReview,
    SearchTimeout, SearchUnavailable, _magnet_hash,
)
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import (
    CompletionRejected, Outcome, Registration, Successor,
)
from src.anime.providers.errors import (
    ProviderAuthenticationFailed, ProviderDeferred, ProviderInvalidResponse, ProviderUnavailable,
)
from src.anime.release_contracts import ReleaseConflict, ReleaseNotReady
from src.anime.storage.episode_discovery import (
    PAGE_SIZE, BackfillConflict, BackfillCompletion,
    load_batch, persist_observations, FallbackCompletion, load_fallback,
    persist_fallback, FallbackMissing, persist_fallback_missing,
    AUTOMATIC_RETRY_MS, automatic_retry_used,
    WAITING_FOR_DUB, DubFound, DubExhausted, CandidatesExhausted,
    load_dub_search, persist_dub_found, persist_dub_exhausted,
    persist_candidates_exhausted, load_tried_hashes,
)
from src.anime.storage.cached_release_retry import CachedTorrent, load_cached, persist_cached
from src.anime.worker import blocking
from src.anime.services.base import service_facts
from src.anime.checks import here


def _check_facts(exc):
    """The failed check (and file) a search error named, for History."""
    facts = {}
    if getattr(exc, 'failed_check', None): facts['failed_check'] = exc.failed_check
    if getattr(exc, 'failed_path', None): facts['failed_path'] = exc.failed_path
    return facts

KIND = 'DISCOVER_EPISODE_RELEASES'
SERVICES=('prowlarr',)  # External services this job needs; see services/README.md.
DEFINITION = HandlerDefinition(KIND, 'Search Prowlarr for backfill and continue episodes')


def _new_dub_releases(result, policy, tried):
    """Accepted Dual/Multi Audio releases not tried before, best first."""
    found = []
    for release in policy.rank(result.candidates):
        digest = release.info_hash
        if (policy.accepts(release) and dual_audio_tagged(release.title) and digest
                and digest not in tried and digest not in found
                and release.tvdb_episode_id == result.tvdb_episode_id
                and _magnet_hash(release.magnet_url) == digest):
            found.append(digest)
    return tuple(found)


def registration(secrets, *, catalog_factory=None, client_factory=None,
                 select_candidates=False, acquire_candidates=False):
    if (type(select_candidates) is not bool or type(acquire_candidates) is not bool
            or acquire_candidates and not select_candidates):
        raise ValueError('acquisition requires opted-in source selection')
    if client_factory is None:
        if catalog_factory is None: raise ValueError('episode catalog required')
        def client_factory(configuration, secrets):
            return Client.from_context(configuration, secrets,
                                       catalog=catalog_factory(configuration))

    async def execute(context):
        payload = context.data()
        try:
            if (set(payload) not in ({'plan_id','start'},
                                    {'plan_id','start','fallback_episode'},
                                    {'plan_id','start','exhausted_episode'},
                                    {'plan_id','start','exhausted_episode','delete_cache'},
                                    {'plan_id','start','retry_episode'})
                    or context.claim.event_id is None):
                raise ValueError()
            plan_id = integer(payload['plan_id'], positive=True)
            start = integer(payload['start'], positive=True)
            if (start-1) % PAGE_SIZE: raise ValueError()
            fallback_episode = (integer(payload['fallback_episode'],positive=True)
                                if 'fallback_episode' in payload else None)
            exhausted_episode = (integer(payload['exhausted_episode'],positive=True)
                                 if 'exhausted_episode' in payload else None)
            retry_episode = (integer(payload['retry_episode'],positive=True)
                             if 'retry_episode' in payload else None)
            delete_cache = payload.get('delete_cache', True)
            if type(delete_cache) is not bool or ('delete_cache' in payload and exhausted_episode is None):
                raise ValueError()
        except (ValueError, TypeError):
            return Outcome('review', 'JOB_PAYLOAD_INVALID',
                           facts={'failed_check': here('JOB_PAYLOAD_INVALID')})
        log = context.logger
        try:
            plan = await blocking(load_batch,
                context.configuration.value('storage.database_path'),plan_id,start,retry_episode)
            def connect(search_mode='initial'):
                client = client_factory(context.configuration,secrets)
                if isinstance(client, Client):
                    client.logger = log
                    client.set_context(plan_id=plan.plan_id, search_mode=search_mode)
                return client
            if exhausted_episode is not None:
                if not acquire_candidates or exhausted_episode not in plan.numbers:
                    raise BackfillConflict()
                req_eng = wants_english(context.configuration.value('acquisition.audio_check'))
                if not delete_cache:
                    cached = await blocking(
                        load_cached,context.configuration.value('storage.database_path'),
                        plan_id,exhausted_episode,req_eng)
                    if cached is not None:
                        log.info('Backfill episode %s: retrying cached metadata source',exhausted_episode)
                        return Outcome(value=(plan,exhausted_episode,cached,req_eng))
                    # A cache miss is not proof that there is no dub. Search
                    # Prowlarr instead of putting the same episode back on hold
                    # without ever checking for new releases.
                    log.info('Backfill episode %s: no eligible cached magnet; searching Prowlarr',
                             exhausted_episode)
                target = (await blocking(load_dub_search,
                    context.configuration.value('storage.database_path'),plan_id,exhausted_episode)
                    if req_eng else None)
                if target is None:
                    log.info('Backfill episode %s: candidates exhausted without usable releases', exhausted_episode)
                    return Outcome('cancel', 'RELEASE_CANDIDATES_EXHAUSTED',
                                   value=CandidatesExhausted(plan, exhausted_episode))
                client = connect('exhausted')
                search_kwargs = {'tried': target.tried} if isinstance(client, Client) else {}
                result = await blocking(client.search,plan.anidb_id,exhausted_episode,target.identity,**search_kwargs)
                fresh = _new_dub_releases(result,client.policy,target.tried)
                snapshots = getattr(client, 'snapshots', None)
                if not fresh:
                    log.info('Backfill episode %s: waiting for a dub', exhausted_episode)
                    return Outcome('cancel', WAITING_FOR_DUB,
                                   value=DubExhausted(plan, exhausted_episode),
                                   search_snapshots=snapshots)
                log.info('Backfill episode %s: %s new Dual/Multi Audio release(s)',
                         exhausted_episode, len(fresh))
                return Outcome(value=DubFound(plan,exhausted_episode,target,fresh),search_snapshots=snapshots)
            log.info('Backfill search: anidb_id=%s tvdb_series_id=%s anidb_episodes=%s reserved=%s',
                     plan.anidb_id, plan.tvdb_series_id, plan.numbers, plan.reserved)
            if fallback_episode is not None:
                if not acquire_candidates or fallback_episode not in plan.numbers:
                    raise BackfillConflict()
                client = connect('fallback')
                target=await blocking(load_fallback,
                    context.configuration.value('storage.database_path'),
                    plan_id,fallback_episode)
                search_kwargs = {'tried': {target.failed_hash}, 'target_hash': target.next_hash} if isinstance(client, Client) else {}
                result=await blocking(client.search,plan.anidb_id,fallback_episode,target.identity,**search_kwargs)
                release=next((row for row in result.candidates
                    if row.info_hash==target.next_hash
                    and client.policy.accepts(row)
                    and row.tvdb_episode_id==result.tvdb_episode_id
                    and _magnet_hash(row.magnet_url)==target.next_hash),None)
                snapshots = getattr(client, 'snapshots', None)
                if release is None:
                    return Outcome(value=FallbackMissing(plan,fallback_episode,target),search_snapshots=snapshots)
                return Outcome(value=FallbackCompletion(
                    plan,fallback_episode,target,
                    CandidateSelection(target.identity,result,release,client.policy)),search_snapshots=snapshots)
            client = connect('initial')
            tried_by_ep = await blocking(load_tried_hashes,
                context.configuration.value('storage.database_path'),plan.plan_id,plan.numbers)
            backfill_kwargs = {}
            if select_candidates:
                backfill_kwargs['select_candidates'] = True
            if isinstance(client, Client) and tried_by_ep:
                backfill_kwargs['tried_by_episode'] = tried_by_ep
            observed = await blocking(
                client.backfill,plan.anidb_id,plan.tvdb_series_id,plan.numbers,
                reserved=plan.reserved,reserved_placements=plan.reserved_placements,
                verified_ids=dict(plan.verified),
                **backfill_kwargs)
            if len(observed) != len(plan.numbers):
                raise BackfillConflict()
            # Report each episode's actual result; unavailable episodes never
            # hold back the ones that have releases.
            for item in observed:
                log.info('Backfill episode %s: status=%s matching=%s accepted=%s',
                         item.number, item.status, item.candidates, item.accepted)
            snapshots = getattr(client, 'snapshots', None)
            return Outcome(value=BackfillCompletion(plan,observed,retry_episode),search_snapshots=snapshots)
        except EpisodeContextConflict as exc:
            log.warning('Persisted episode identity conflict: %s != %s',
                        exc.context_left, exc.context_right)
            return Outcome('review', exc.reason_code, facts={
                'failed_check': exc.failed_check,
                'context_left': exc.context_left,
                'context_right': exc.context_right,
            })
        except BackfillConflict:
            return Outcome('review','EPISODE_DISCOVERY_PLAN_CONFLICT',facts={'failed_check':here('PLAN_CONFLICT')})
        except SearchUnavailable as exc:   # includes SearchTimeout
            snapshots = getattr(client, 'snapshots', None) if 'client' in locals() else None
            if not isinstance(exc, SearchTimeout):
                # Prowlarr is unavailable: wait for it like every service outage
                # (no retry spent, resumes when it is back, review after 72 hours).
                return Outcome('retry','SERVICE_UNAVAILABLE',facts={**service_facts('prowlarr'), **_check_facts(exc)},
                               search_snapshots=snapshots)
            # Timed out three times in the client: wait 30 minutes once, then
            # stop for a manual Retry; nothing is published for this page.
            reason = exc.reason_code
            used = await blocking(automatic_retry_used,
                context.configuration.value('storage.database_path'),context.claim.job_id)
            snapshots = getattr(client, 'snapshots', None) if 'client' in locals() else None
            facts = {}
            if getattr(exc, 'failed_check', None): facts['failed_check'] = exc.failed_check
            if getattr(exc, 'failed_path', None): facts['failed_path'] = exc.failed_path
            if used: return Outcome('blocked', reason, search_snapshots=snapshots, facts=facts or None)
            log.warning('Backfill search will run again in %s minutes: %s',
                        AUTOMATIC_RETRY_MS//60000, reason)
            return Outcome('defer', reason, delay_ms=AUTOMATIC_RETRY_MS, search_snapshots=snapshots, facts=facts or None)
        except SearchUnauthorized as exc:
            snapshots = getattr(client, 'snapshots', None) if 'client' in locals() else None
            facts = {**service_facts('prowlarr','AUTH_FAILED'), **_check_facts(exc)}
            return Outcome('review','SERVICE_SETUP_INVALID',facts=facts,search_snapshots=snapshots)
        except SearchConfiguration as exc:
            snapshots = getattr(client, 'snapshots', None) if 'client' in locals() else None
            facts = {**service_facts('prowlarr','NOT_CONFIGURED'), **_check_facts(exc)}
            return Outcome('review','SERVICE_SETUP_INVALID',facts=facts,search_snapshots=snapshots)
        except SearchReview as exc:
            snapshots = getattr(client, 'snapshots', None) if 'client' in locals() else None
            facts = {**service_facts('prowlarr'), **_check_facts(exc)}
            return Outcome('review','SERVICE_RESPONSE_INVALID',facts=facts,search_snapshots=snapshots)
        except ProviderDeferred as exc:
            return Outcome('defer','JOB_WAITING',delay_ms=exc.delay_ms)
        except (ProviderUnavailable,ProviderInvalidResponse) as exc:
            return Outcome('retry',exc.reason_code,facts={'failed_check':here('SERVICE_UNAVAILABLE')})
        except ProviderAuthenticationFailed as exc:
            # AniDB or TVDB, not Prowlarr: no service is named, so Prowlarr is not marked.
            return Outcome('review',exc.reason_code,facts={'service_status':'AUTH_FAILED','failed_check':here('SERVICE_SETUP_INVALID')})
        except (ReleaseNotReady,ReleaseConflict) as exc:
            return Outcome('review',exc.reason_code,facts={'failed_check':here('RELEASE_NOT_READY')})

    def persist(db,claim,value):
        try:
            if type(value) is FallbackCompletion:
                return persist_fallback(db,claim,value)
            if type(value) is FallbackMissing:
                return persist_fallback_missing(db,claim,value)
            if type(value) is DubFound:
                return persist_dub_found(db,claim,value)
            if type(value) is DubExhausted:
                return persist_dub_exhausted(db,claim,value)
            if type(value) is CandidatesExhausted:
                return persist_candidates_exhausted(db,claim,value)
            if type(value) is tuple and len(value) in (3, 4) and type(value[2]) is CachedTorrent:
                eng = value[3] if len(value) == 4 else True
                return persist_cached(db,claim,value[0],value[1],value[2],eng)
            return persist_observations(db,claim,value,dispatch=acquire_candidates)
        except BackfillConflict:
            raise CompletionRejected(Outcome('review','EPISODE_DISCOVERY_PLAN_CONFLICT',
                                             facts={'failed_check':here('PERSIST_PLAN_CONFLICT')})) from None

    def route(value):
        if (type(value) in (FallbackCompletion,FallbackMissing,DubFound,DubExhausted,CandidatesExhausted)
                or type(value) is tuple and len(value) in (3, 4) and type(value[2]) is CachedTorrent): return ()
        if type(value) is not BackfillCompletion: raise ValueError('typed backfill result required')
        if value.retry_episode is not None: return ()
        next_start=value.plan.start+len(value.plan.numbers)
        if next_start>value.plan.window_size: return ()
        return (Successor.create(KIND,{'plan_id':value.plan.plan_id,'start':next_start}),)

    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=persist,route=route,
                        successor_kinds=(KIND,))
