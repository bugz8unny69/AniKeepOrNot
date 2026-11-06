"""Batch discovery through a Prowlarr season-pack search (WI-05, WI-15).

Runs once the known candidates of a season are exhausted. Three steps:
read the scope (read-only), search Prowlarr (no transaction open), then
record and decide in one short write transaction that re-reads the
controller. Only a search in which every query succeeded and nothing
eligible was found marks the season EXHAUSTED.
"""
from src.nextgen.acquisition.prowlarr_search import (
    Client, SearchConfiguration, SearchReview, SearchUnauthorized, SearchUnavailable,
)
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import Outcome, Registration
from src.nextgen.storage.batch_candidates import (
    DISCOVERY_KIND as KIND, exhaust_scope, ingest_batch_prowlarr_results, prepare_batch_prowlarr_search,
)
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction
from src.nextgen.diagnostics import log_history
from src.nextgen.storage.sonarr_backfill import AUTOMATIC_RETRY_MS, automatic_retry_used
from src.nextgen.worker import blocking

DEFINITION = HandlerDefinition(KIND, 'Search Prowlarr for batch season pack candidates')


def registration(secrets, *, catalog_factory=None, client_factory=None):
    if client_factory is None:
        def client_factory(configuration, secrets):
            catalog = catalog_factory(configuration) if catalog_factory else None
            return Client.from_context(configuration, secrets, catalog=catalog)

    async def execute(context):
        payload = context.data()
        try:
            if set(payload) != {'scope_id', 'anidb_id', 'tvdb_series_id', 'tvdb_season'}:
                raise ValueError()
            scope_id = integer(payload['scope_id'], positive=True)
            for key in ('anidb_id', 'tvdb_series_id', 'tvdb_season'):
                integer(payload[key], positive=True)
        except (ValueError, TypeError):
            return Outcome('review', 'JOB_PAYLOAD_INVALID')
        database = context.configuration.value('storage.database_path')
        queue = JobQueue(database)

        async def wait(reason, snapshots):
            # Same limit as single-episode backfill: one delayed retry, then a
            # manual Retry. The scope is never marked EXHAUSTED for this.
            if await blocking(automatic_retry_used, database, context.claim.job_id):
                return Outcome('blocked', reason, search_snapshots=snapshots)
            return Outcome('defer', reason, delay_ms=AUTOMATIC_RETRY_MS, search_snapshots=snapshots)

        def read():
            with queue.connection() as db:
                return prepare_batch_prowlarr_search(db, scope_id)
        params = await blocking(read)
        if params is None:
            return Outcome()        # A release arrived or the season finished meanwhile.

        if not context.configuration.value('prowlarr.base_url'):
            # Automatic search is off: nothing more can be found for the season.
            def off():
                with queue.connection() as db, transaction(db):
                    exhaust_scope(db, scope_id)
            await blocking(off)
            return Outcome()

        try:
            client = client_factory(context.configuration, secrets)
            if hasattr(client, 'logger'):
                client.logger = context.logger
            client.set_context(search_mode='batch_fallback', anidb_id=params.anidb_id,
                               tvdb_series_id=params.tvdb_series_id)
            # Network search: no transaction is open.
            result = await blocking(client.search_batch, params.titles, params.season, tried=params.tried)
        except SearchUnavailable as exc:      # Every query failed; includes timeouts.
            return await wait(exc.reason_code, getattr(client, 'snapshots', None))
        except SearchUnauthorized:
            return Outcome('review', 'PROWLARR_AUTH_FAILED')
        except SearchConfiguration:
            return Outcome('review', 'PROWLARR_CONFIGURATION_INVALID')
        except SearchReview:
            return Outcome('review', 'PROWLARR_SEARCH_REVIEW')
        snapshots = getattr(client, 'snapshots', None)
        if not result.complete and not result.releases:
            # Some queries failed and the rest found nothing: not proof of
            # exhaustion. Keep the diagnostics and search again later.
            return await wait(result.failed[-1][1], snapshots)

        def ingest():
            with queue.connection() as db, transaction(db):
                return ingest_batch_prowlarr_results(
                    db, params, result, snapshots=snapshots, job_id=context.claim.job_id,
                    attempt=context.claim.attempt,
                    audio_check=context.configuration.value('acquisition.audio_check'))
        for record in await blocking(ingest):
            log_history(context.logger, record)
        return Outcome()

    return Registration(KIND, DEFINITION, execute)
