"""One durable Keep execution protocol for single files and season packs.

The Sonarr import service is shared. A batch differs only in the collection
of episode-scoped journals it feeds to that service, not in its management,
retry, review, or completion semantics. No feature switch exists.
"""
from src.nextgen.async_io import blocking
from src.nextgen.job_contracts import Outcome, failure_facts
from src.nextgen.storage.management_actions import ManagementActions, ActionUnavailable
from src.nextgen.sonarr import KeepReview, KeepUnavailable
from src.nextgen.staging import StagingReview, StagingBusy
from src.nextgen.keep_selection import KeepSelectionInvalid
from src.nextgen.acquisition.media import MediaInvalid


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
            raise KeepReview(failed_check='src/nextgen/jobs/keep_execution.py:33 execute JOURNALS_INVALID')
        for factory in factories:
            options = {} if factory is None else {'journal_factory': factory}
            service = service_factory(context.configuration, secrets, **options)
            if not await blocking(service.run, context.claim, target, context.cancelled):
                return Outcome('defer', 'KEEP_IMPORT_WAITING', delay_ms=30000)
        value = {'refresh': bool(context.configuration.value('jellyfin.base_url'))}
        if shoko:
            # Optional Shoko enrichment currently has a single-file journal.
            value['shoko'] = bool(context.configuration.value('shoko.base_url'))
        return Outcome(value=value)
    except StagingBusy:
        return Outcome('defer', 'KEEP_IMPORT_WAITING', delay_ms=30000)
    except KeepUnavailable:
        return Outcome('retry', 'KEEP_SERVICE_UNAVAILABLE')
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
        return Outcome('retry', 'KEEP_SERVICE_UNAVAILABLE')
