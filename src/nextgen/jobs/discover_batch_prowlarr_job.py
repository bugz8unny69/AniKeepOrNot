"""Batch discovery through Prowlarr season pack fallback (WI-05)."""
from src.nextgen.acquisition.prowlarr_search import Client, SearchConfiguration, SearchReview, SearchUnavailable
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import Outcome, Registration
from src.nextgen.storage.batch_candidates import execute_batch_prowlarr_fallback
from src.nextgen.worker import blocking

KIND = 'DISCOVER_BATCH_PROWLARR'
DEFINITION = HandlerDefinition(KIND, 'Search Prowlarr for batch season pack candidates')


def registration(secrets, *, client_factory=None):
    if client_factory is None:
        def client_factory(configuration, secrets):
            return Client.from_context(configuration, secrets, catalog=None)

    async def execute(context):
        payload = context.data()
        try:
            scope_id = integer(payload['scope_id'], positive=True)
            anidb_id = integer(payload['anidb_id'], positive=True)
            tvdb_series_id = integer(payload['tvdb_series_id'], positive=True)
            season = integer(payload['tvdb_season'], positive=True)
        except (ValueError, TypeError, KeyError):
            return Outcome('review', 'JOB_PAYLOAD_INVALID')

        client = client_factory(context.configuration, secrets)
        if hasattr(client, 'logger'):
            client.logger = context.logger
        audio_check = 'japanese_english'
        try:
            audio_check = context.configuration.value('acquisition.audio_check')
        except Exception:
            pass

        def _do_search(db):
            return execute_batch_prowlarr_fallback(
                db, scope_id, client, audio_check=audio_check,
                save_snapshot_job_id=context.claim.job_id,
                save_snapshot_attempt=context.claim.attempt_count,
            )

        with context.claim.queue.connection() as db, context.claim.queue.transaction(db):
            try:
                receipts = await blocking(_do_search, db)
            except SearchConfiguration:
                from src.nextgen.storage.batch_candidates import exhaust_scope
                exhaust_scope(db, scope_id)
                return Outcome('fail', 'PROWLARR_CONFIGURATION_INVALID')
            except SearchUnavailable:
                return Outcome('retry')

        return Outcome('pass')

    return Registration(KIND, DEFINITION, execute)
