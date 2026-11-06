"""Batch discovery through Prowlarr season pack fallback (WI-05)."""
from src.nextgen.acquisition.prowlarr_search import Client, SearchConfiguration, SearchReview, SearchUnavailable
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import Outcome, Registration
from src.nextgen.storage.batch_candidates import (
    exhaust_scope, prepare_batch_prowlarr_search, ingest_batch_prowlarr_results,
)
from src.nextgen.storage.job_queue import JobQueue
from src.nextgen.storage.sqlite import transaction
from src.nextgen.worker import blocking

KIND = 'DISCOVER_BATCH_PROWLARR'
DEFINITION = HandlerDefinition(KIND, 'Search Prowlarr for batch season pack candidates')


def registration(secrets, *, catalog_factory=None, client_factory=None):
    if client_factory is None:
        def client_factory(configuration, secrets):
            cat = catalog_factory(configuration) if catalog_factory else (lambda *args: None)
            return Client.from_context(configuration, secrets, catalog=cat)

    async def execute(context):
        payload = context.data()
        try:
            scope_id = integer(payload['scope_id'], positive=True)
            anidb_id = integer(payload['anidb_id'], positive=True)
            tvdb_series_id = integer(payload['tvdb_series_id'], positive=True)
            season = integer(payload['tvdb_season'], positive=True)
        except (ValueError, TypeError, KeyError):
            return Outcome('review', 'JOB_PAYLOAD_INVALID')

        database = context.configuration.value('storage.database_path')

        # 1. Read scope search parameters (read-only)
        def _read_params():
            with JobQueue(database).connection() as db:
                return prepare_batch_prowlarr_search(db, scope_id)

        params = await blocking(_read_params)
        if params is None:
            return Outcome()

        scope_id, anidb_id, tvdb_series_id, season, titles, tried = params

        try:
            client = client_factory(context.configuration, secrets)
        except SearchConfiguration:
            def _fail_conf():
                with JobQueue(database).connection() as db, transaction(db):
                    exhaust_scope(db, scope_id)
            await blocking(_fail_conf)
            return Outcome('fail', 'PROWLARR_CONFIGURATION_INVALID')

        if hasattr(client, 'logger'):
            client.logger = context.logger
        if hasattr(client, 'set_context'):
            client.set_context(search_mode='batch_fallback', anidb_id=anidb_id, tvdb_series_id=tvdb_series_id)

        audio_check = 'japanese_english'
        try:
            audio_check = context.configuration.value('acquisition.audio_check')
        except Exception:
            pass

        # 2. NETWORK SEARCH: performed outside of any SQLite write transaction!
        try:
            matching_releases = await blocking(client.search_batch, titles, season, tried=tried)
        except SearchConfiguration:
            def _fail_conf():
                with JobQueue(database).connection() as db, transaction(db):
                    exhaust_scope(db, scope_id)
            await blocking(_fail_conf)
            return Outcome('fail', 'PROWLARR_CONFIGURATION_INVALID')
        except SearchUnavailable as exc:
            return Outcome('retry', exc.reason_code, delay_ms=30000)

        # 3. INGESTION: persist candidates, identity, snapshots and schedule winner under write transaction
        snapshots = getattr(client, 'snapshots', None)

        def _do_ingest():
            with JobQueue(database).connection() as db, transaction(db):
                return ingest_batch_prowlarr_results(
                    db, scope_id, anidb_id, tvdb_series_id, season, matching_releases,
                    snapshots=snapshots,
                    save_snapshot_job_id=context.claim.job_id,
                    save_snapshot_attempt=context.claim.attempt,
                    audio_check=audio_check,
                )

        await blocking(_do_ingest)
        return Outcome()

    return Registration(KIND, DEFINITION, execute)
