"""Validated durable audit inserts, independent of log levels and scheduling.

Call only inside the same explicit transaction as the corresponding mutation.
Receipts are safe to log after a successful commit; they are not proof of commit.
"""
from dataclasses import dataclass
import json

from src.anime.handlers import EMPTY_HANDLERS, HANDLER_EVENTS, HandlerCatalog, HandlerDefinition, invocation_id as validate_invocation_id
from src.anime.messages import validate_event, validate_job_activity
from src.anime.reasons import safe_details
from src.anime.matching import validated as validate_matching
from src.anime.settings import COMPONENTS

MAX_INTEGER = (1 << 63) - 1
MAX_DETAILS_BYTES = 4096  # The previous bound still applies to non-matching events.


def integer(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_INTEGER:
        raise ValueError("invalid diagnostic integer")
    return value


def component_name(component):
    if type(component) is not str or component not in COMPONENTS:
        raise ValueError("invalid diagnostic component")
    return component


def encode_details(details=None):
    if details is None:
        details = {}
    if type(details) is not dict:
        raise ValueError("invalid diagnostic details")
    allowed = {"missing_fields", "invalid_fields", "attempt", "max_attempts",
               "retry_at_ms", "cause_reason_code", "matching",
               "video_mb", "limit_mb", "plan_used_mb", "failed_check", "failed_path", "metadata_evidence",
               "delete_cache", "cache_entries_deleted", "cached_replay",
               "observed_audio", "missing_audio", "blocking_job_id", "operation_id",
               "service", "service_detail", "service_status", "waiting_since_ms", "context_left", "context_right",
               "storage_error"}
    if any(type(key) is not str or key not in allowed for key in details):
        raise ValueError("invalid diagnostic detail key")
    safe = safe_details(**{k: v for k, v in details.items() if k not in ("matching", "metadata_evidence")})
    if "metadata_evidence" in details:
        from src.anime.acquisition.metadata_evidence import validated
        safe["metadata_evidence"] = validated(details["metadata_evidence"])
    if "matching" in details:
        # Only the tightly validated public matching format may exceed the
        # ordinary audit bound; never persist arbitrary provider responses.
        validate_matching(details["matching"])
        safe["matching"] = details["matching"]
    encoded = json.dumps(safe, sort_keys=True, ensure_ascii="matching" not in details,
                         separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > (9216 if "matching" in details else MAX_DETAILS_BYTES):
        raise ValueError("diagnostic details too large")
    return encoded


def trace_id(*, event_id=None, job_id=None):
    if event_id is not None:
        return "AD-" + str(integer(event_id, positive=True))
    if job_id is not None:
        return "JOB-" + str(integer(job_id, positive=True))
    return None


@dataclass(frozen=True)
class HistoryRecord:
    history_id: int
    scope: str
    event_code: str
    reason_code: str | None
    component: str
    occurred_at_ms: int
    event_id: int | None
    job_id: int | None
    created_by_job_id: int | None
    details_json: str
    activity_code: str | None = None
    handler_code: str | None = None
    handler_label: str | None = None
    invocation_id: str | None = None


def _validate(db, scope, target_id, event_code, reason_code, component, occurred_at_ms, details):
    if not db.in_transaction:
        raise ValueError("history requires an active transaction")
    integer(target_id, positive=True)
    integer(occurred_at_ms)
    component_name(component)
    validate_event(event_code, reason_code, scope=scope)
    encoded = encode_details(details)
    # Attempts are observations of the current attempt, never scheduling policy.
    attempt = json.loads(encoded).get("attempt")
    if scope == "job" and event_code != "CREATED" and attempt is None:
        raise ValueError("attempt required for job lifecycle event")
    if (scope == "job" and event_code in ({"CLAIMED", "LEASE_EXPIRED", "RETRY_SCHEDULED", "DEFERRED", "DONE"} | HANDLER_EVENTS)
            and (attempt is None or attempt < 1)):
        raise ValueError("positive attempt required for processing event")
    if event_code == "CREATED" and attempt not in (None, 0):
        raise ValueError("created job must have zero attempts")
    return encoded
