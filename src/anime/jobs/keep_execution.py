"""One durable Keep execution protocol for single files and season packs.

The Sonarr import service is shared. A batch differs only in the collection
of episode-scoped journals it feeds to that service, not in its management,
retry, review, or completion semantics. No feature switch exists.
"""
from src.anime.async_io import blocking
from src.anime.job_contracts import Outcome, failure_facts
from src.anime.storage.management_actions import ManagementActions, ActionUnavailable
from src.anime.sonarr import KeepReview, KeepUnavailable
from src.anime.staging import StagingReview, StagingBusy
from src.anime.keep_selection import KeepSelectionInvalid
from src.anime.acquisition.media import MediaInvalid
from src.anime.services.base import service_facts
from src.anime.checks import here


async def execute_keep(context, secrets, *, service_factory, journals=None, shoko=False):
    """Apply the same outcome contract to one or more durable Sonarr imports.

    journals, when provided, is a trusted synchronous callable returning
    factories for independently persisted episode import journals. A false
    completion stops at the first pending episode; completed episodes resume
    idempotently from their existing journals on the next attempt.
    """
    actions = ManagementActions(context.configuration.value('storage.database_path'))
    try:
        target = await blocking(actions.bind, context.claim, 'KEEP')
    except (ActionUnavailable, ValueError, TypeError):
        return Outcome('review', 'JOB_PAYLOAD_INVALID')
    try:
        factories = (None,) if journals is None else await blocking(journals, actions, target)
        if (not isinstance(factories, tuple) or not 1 <= len(factories) <= 32
                or (journals is not None and any(not callable(factory) for factory in factories))):
            raise KeepReview(failed_check=here('JOURNALS_INVALID'))
        for factory in factories:
            options = {} if factory is None else {'journal_factory': factory}
            service = service_factory(context.configuration, secrets, **options)
            if not await blocking(service.run, context.claim, target, context.cancelled):
                return Outcome('defer', 'KEEP_IMPORT_WAITING', delay_ms=30000)
        value = {'refresh': bool(context.configuration.value('jellyfin.base_url'))}
        # Every import of this job is done: the series' last Keep asks Sonarr once
        # for the missing episodes. A failed search never fails the Keep.
        request_search = getattr(service, 'request_search', None)
        searched = None if request_search is None else await blocking(request_search, context.claim)
        if searched is not None:
            value['search'] = searched
        if shoko:
            # Optional Shoko enrichment currently has a single-file journal.
            value['shoko'] = bool(context.configuration.value('shoko.base_url'))
        return Outcome(value=value)
    except StagingBusy:
        return Outcome('defer', 'KEEP_IMPORT_WAITING', delay_ms=30000)
    except KeepUnavailable:
        return Outcome('retry', 'SERVICE_UNAVAILABLE', facts=service_facts('sonarr'))
    except KeepReview as exc:
        facts = None
        if getattr(exc, 'failed_check', None):
            facts = {'failed_check': exc.failed_check}
            if getattr(exc, 'failed_path', None):
                facts['failed_path'] = exc.failed_path
        if not facts:
            facts = failure_facts(exc)
        return Outcome('review', exc.reason_code, facts=facts)
    except (KeepSelectionInvalid, StagingReview, MediaInvalid,
            PermissionError, ValueError, KeyError, TypeError) as exc:
        facts = failure_facts(exc)
        return Outcome('review', 'KEEP_IMPORT_REVIEW', facts=facts)
    except OSError:
        return Outcome('retry', 'SERVICE_UNAVAILABLE', facts=service_facts('sonarr'))


def library_history(db, claim, value):
    """History for what Keep changed in Sonarr: a series it added, and the search."""
    import time
    from src.anime.history import job_attempt, append_job_history
    now = time.time_ns() // 1000000
    attempt = {'attempt': job_attempt(db, claim.job_id)}
    records = []
    added = db.execute(
        "SELECT json_extract(data_json,'$.series_add.monitor') FROM keep_imports WHERE job_id=? "
        "UNION ALL SELECT json_extract(data_json,'$.series_add.monitor') FROM batch_keep_imports WHERE job_id=?",
        (claim.job_id, claim.job_id)).fetchall()
    monitor = next((row[0] for row in added if row[0] is not None), None)
    if monitor is not None:
        records.append(append_job_history(
            db, job_id=claim.job_id, event_code='KEEP_SERIES_ADDED', component='worker', occurred_at_ms=now,
            reason_code='KEEP_SERIES_ADDED_NEW_ONLY' if monitor == 'future' else 'KEEP_SERIES_ADDED',
            details=attempt))
    search = (value or {}).get('search')
    if search in ('SENT', 'FAILED'):
        records.append(append_job_history(
            db, job_id=claim.job_id, event_code='KEEP_SEARCH_REQUESTED', component='worker', occurred_at_ms=now,
            reason_code='KEEP_SEARCH_SENT' if search == 'SENT' else 'KEEP_SEARCH_FAILED', details=attempt))
    return tuple(records)
