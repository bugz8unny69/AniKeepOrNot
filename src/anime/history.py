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
               "service", "service_detail", "service_status", "waiting_since_ms", "context_left", "context_right"}
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


def append_event_history(db, *, event_id, event_code, component, occurred_at_ms,
                         reason_code=None, details=None):
    encoded = _validate(db, "incoming_event", event_id, event_code, reason_code,
                        component, occurred_at_ms, details)
    if db.execute("SELECT 1 FROM incoming_events WHERE event_id=?", (event_id,)).fetchone() is None:
        raise ValueError("unknown event")
    cursor = db.execute("""INSERT INTO event_history
        (event_id,occurred_at_ms,event_code,reason_code,details_json,component)
        VALUES(?,?,?,?,?,?)""", (event_id, occurred_at_ms, event_code, reason_code, encoded, component))
    return HistoryRecord(cursor.lastrowid, "incoming_event", event_code, reason_code,
                         component, occurred_at_ms, event_id, None, None, encoded)


def job_attempt(db,job_id):
    """Observe the target job's attempt for a cross-operation control audit."""
    if not db.in_transaction:raise ValueError("history requires an active transaction")
    row=db.execute('SELECT attempt_count FROM jobs WHERE job_id=?',(integer(job_id,positive=True),)).fetchone()
    if row is None:raise ValueError("unknown job")
    return row[0]


def append_job_history(db, *, job_id, event_code, component, occurred_at_ms,
                       reason_code=None, details=None, handler_code=None,
                       invocation_id=None, handlers=EMPTY_HANDLERS):
    encoded = _validate(db, "job", job_id, event_code, reason_code,
                        component, occurred_at_ms, details)
    row = db.execute("SELECT event_id,created_by_job_id,attempt_count,state,activity_code FROM jobs WHERE job_id=?",
                     (job_id,)).fetchone()
    if row is None:
        raise ValueError("unknown job")
    attempt = json.loads(encoded).get("attempt")
    if attempt is not None and attempt != row[2]:
        raise ValueError("audit attempt does not match job")
    validate_job_activity(row[3], row[4])
    handler_label = None
    if event_code in HANDLER_EVENTS:
        if type(handlers) is not HandlerCatalog:
            raise ValueError("invalid handler catalog")
        definition = handlers.require(handler_code)
        handler_label = definition.label
        validate_invocation_id(invocation_id)
        if component != "worker" or row[3] != "RUNNING":
            raise ValueError("handler events require worker attribution and a running job")
        start = db.execute(
            "SELECT job_id,handler_code,handler_label,occurred_at_ms,details_json FROM job_history "
            "WHERE invocation_id=? AND event_code='HANDLER_STARTED'", (invocation_id,)).fetchone()
        if event_code == "HANDLER_STARTED":
            if start is not None or db.execute(
                    "SELECT 1 FROM job_history WHERE invocation_id=?", (invocation_id,)).fetchone() is not None:
                raise ValueError("handler invocation already exists")
        else:
            if (start is None or start[:2] != (job_id, handler_code)
                    or occurred_at_ms < start[3]
                    or json.loads(start[4]).get("attempt") != attempt):
                raise ValueError("handler completion does not match its start")
            # A readable start snapshot survives label/catalog changes.
            handler_label = HandlerDefinition(handler_code, start[2]).label
            if db.execute("SELECT 1 FROM job_history WHERE invocation_id=? "
                          "AND event_code IN ('HANDLER_COMPLETED','HANDLER_FAILED')",
                          (invocation_id,)).fetchone() is not None:
                raise ValueError("handler invocation already finished")
    elif handler_code is not None or invocation_id is not None:
        raise ValueError("handler metadata requires a handler event")
    cursor = db.execute("""INSERT INTO job_history
        (job_id,occurred_at_ms,event_code,reason_code,details_json,component,activity_code,
         handler_code,handler_label,invocation_id)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (job_id, occurred_at_ms, event_code, reason_code,
        encoded, component, row[4], handler_code, handler_label, invocation_id))
    return HistoryRecord(cursor.lastrowid, "job", event_code, reason_code, component,
                         occurred_at_ms, row[0], job_id, row[1], encoded, row[4],
                         handler_code, handler_label, invocation_id)
