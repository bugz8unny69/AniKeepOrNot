"""Backfill/continue discovery through Prowlarr; no Sonarr, grabs or automatic waiting.

The job kind keeps its historical name so existing jobs and history stay valid.
"""
from src.nextgen.acquisition.audio import dual_audio_tagged, wants_english
from src.nextgen.acquisition.prowlarr_search import (
    Client, CandidateSelection, SearchConfiguration, SearchUnauthorized, SearchReview,
    SearchUnavailable, _magnet_hash,
)
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import (
    CompletionRejected, Outcome, Registration, Successor,
)
from src.nextgen.providers.errors import (
    ProviderAuthenticationFailed, ProviderDeferred, ProviderInvalidResponse, ProviderUnavailable,
)
from src.nextgen.release_contracts import ReleaseConflict, ReleaseNotReady
from src.nextgen.storage.sonarr_backfill import (
    PAGE_SIZE, BackfillConflict, BackfillCompletion,
    load_batch, persist_observations, FallbackCompletion, load_fallback,
    persist_fallback, FallbackMissing, persist_fallback_missing,
    AUTOMATIC_RETRY_MS, automatic_retry_used,
    WAITING_FOR_DUB, DubFound, load_dub_search, persist_dub_found,
)
from src.nextgen.worker import blocking

KIND = 'DISCOVER_SONARR_BACKFILL'
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
        except (ValueError, TypeError):
            return Outcome('review', 'JOB_PAYLOAD_INVALID')
        log = context.logger
        try:
            plan = await blocking(load_batch,
                context.configuration.value('storage.database_path'),plan_id,start,retry_episode)
            def connect():
                client = client_factory(context.configuration,secrets)
                if isinstance(client, Client): client.logger = log
                return client
            if exhausted_episode is not None:
                if not acquire_candidates or exhausted_episode not in plan.numbers:
                    raise BackfillConflict()
                # Only when English is required, all releases failed and one failed audio:
                # wait for a dub instead of review. Retry runs this again.
                target = (await blocking(load_dub_search,
                    context.configuration.value('storage.database_path'),plan_id,exhausted_episode)
                    if wants_english(context.configuration.value('acquisition.audio_check')) else None)
                if target is None:
                    return Outcome('review','SONARR_CANDIDATES_EXHAUSTED')
                client = connect()
                result = await blocking(client.search,plan.anidb_id,target.identity)
                fresh = _new_dub_releases(result,client.policy,target.tried)
                if not fresh:
                    log.info('Backfill episode %s: waiting for a dub', exhausted_episode)
                    return Outcome('hold',WAITING_FOR_DUB)
                log.info('Backfill episode %s: %s new Dual/Multi Audio release(s)',
                         exhausted_episode, len(fresh))
                return Outcome(value=DubFound(plan,exhausted_episode,target,fresh))
            log.info('Backfill search: anidb_id=%s tvdb_series_id=%s season=%s episodes=%s reserved=%s',
                     plan.anidb_id, plan.tvdb_series_id, plan.season, plan.numbers, plan.reserved)
            client = connect()
            if fallback_episode is not None:
                if not acquire_candidates or fallback_episode not in plan.numbers:
                    raise BackfillConflict()
                target=await blocking(load_fallback,
                    context.configuration.value('storage.database_path'),
                    plan_id,fallback_episode)
                result=await blocking(client.search,plan.anidb_id,target.identity)
                release=next((row for row in result.candidates
                    if row.info_hash==target.next_hash
                    and client.policy.accepts(row)
                    and row.tvdb_episode_id==result.tvdb_episode_id
                    and _magnet_hash(row.magnet_url)==target.next_hash),None)
                if release is None:
                    return Outcome(value=FallbackMissing(plan,fallback_episode,target))
                return Outcome(value=FallbackCompletion(
                    plan,fallback_episode,target,
                    CandidateSelection(target.identity,result,release,client.policy)))
            observed = await blocking(
                client.backfill,plan.anidb_id,plan.tvdb_series_id,plan.season,plan.numbers,
                reserved=plan.reserved,verified_ids=dict(plan.verified),
                **({'select_candidates': True} if select_candidates else {}))
            if len(observed) != len(plan.numbers):
                raise BackfillConflict()
            # Report each episode's actual result; unavailable episodes never
            # hold back the ones that have releases.
            for item in observed:
                log.info('Backfill episode %s: status=%s matching=%s accepted=%s',
                         item.number, item.status, item.candidates, item.accepted)
            return Outcome(value=BackfillCompletion(plan,observed,retry_episode))
        except BackfillConflict:
            return Outcome('review','SONARR_BACKFILL_PLAN_CONFLICT')
        except SearchUnavailable as exc:   # includes SearchTimeout
            # The client already tried three times. Wait 30 minutes once, then
            # stop for a manual Retry; nothing is published for this page.
            reason = exc.reason_code
            used = await blocking(automatic_retry_used,
                context.configuration.value('storage.database_path'),context.claim.job_id)
            if used: return Outcome('blocked',reason)
            log.warning('Backfill search will run again in %s minutes: %s',
                        AUTOMATIC_RETRY_MS//60000, reason)
            return Outcome('defer',reason,delay_ms=AUTOMATIC_RETRY_MS)
        except SearchUnauthorized:
            return Outcome('review','PROWLARR_AUTH_FAILED')
        except SearchConfiguration:
            return Outcome('review','PROWLARR_CONFIGURATION_INVALID')
        except SearchReview:
            return Outcome('review','PROWLARR_SEARCH_REVIEW')
        except ProviderDeferred as exc:
            return Outcome('defer','JOB_WAITING',delay_ms=exc.delay_ms)
        except (ProviderUnavailable,ProviderInvalidResponse) as exc:
            return Outcome('retry',exc.reason_code)
        except ProviderAuthenticationFailed:
            return Outcome('review','PROVIDER_AUTH_FAILED')
        except (ReleaseNotReady,ReleaseConflict) as exc:
            return Outcome('review',exc.reason_code)

    def persist(db,claim,value):
        try:
            if type(value) is FallbackCompletion:
                return persist_fallback(db,claim,value)
            if type(value) is FallbackMissing:
                return persist_fallback_missing(db,claim,value)
            if type(value) is DubFound:
                return persist_dub_found(db,claim,value)
            return persist_observations(db,claim,value,dispatch=acquire_candidates)
        except BackfillConflict:
            raise CompletionRejected(Outcome('review','SONARR_BACKFILL_PLAN_CONFLICT')) from None

    def route(value):
        if type(value) in (FallbackCompletion,FallbackMissing,DubFound): return ()
        if type(value) is not BackfillCompletion: raise ValueError('typed backfill result required')
        if value.retry_episode is not None: return ()
        next_start=value.plan.start+len(value.plan.numbers)
        if next_start>value.plan.window_size: return ()
        return (Successor.create(KIND,{'plan_id':value.plan.plan_id,'start':next_start}),)

    return Registration(KIND,DEFINITION,execute,persist=persist,route=route,
                        successor_kinds=(KIND,))
