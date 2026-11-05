"""Readable, bounded NextGen diagnostics; audit persistence lives in history.py.

Reuse stdlib logging/levels and the existing reason catalog. Public messages
are static catalog copy; no raw payloads or exception strings are accepted.
"""
import copy
import json
import logging
import re
import sqlite3
import time

from src.nextgen.handlers import HANDLER_EVENTS, HandlerDefinition, invocation_id
from src.nextgen.history import component_name, integer, trace_id
from src.nextgen.messages import DEFAULT_CATALOG, JOB_ACTIVITIES, STATES, validate_event
from src.nextgen.reasons import get_reason
from src.nextgen.settings import Settings

TRACE = 5
logging.addLevelName(TRACE, "TRACE")
_LEVELS = {"TRACE": TRACE, "DEBUG": logging.DEBUG, "INFO": logging.INFO,
           "WARNING": logging.WARNING, "ERROR": logging.ERROR,
           "CRITICAL": logging.CRITICAL}
_SECRET_NAME = r"password|passkey|access[_-]?token|token|api[_-]?key|apikey|x-emby-token|secret|cookie|authorization|signature"
_SECRETS = (
    (re.compile(r"(?i)\bBearer\s+[^\s,;]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(" + _SECRET_NAME + r")\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(https?://)[^/@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)([?&](?:" + _SECRET_NAME + r"|key|auth)=)[^&#\s]+"), r"\1[REDACTED]"),
)
_ERROR_TYPES = {kind: kind.__name__ for kind in (
    Exception, RuntimeError, ValueError, TypeError, KeyError, LookupError,
    OSError, PermissionError, FileNotFoundError, TimeoutError, ConnectionError,
    sqlite3.Error, sqlite3.OperationalError, sqlite3.IntegrityError,
)}


def redact(value) -> str:
    """Compatibility defense for direct logging; not a payload sanitizer."""
    result = str(value)
    for pattern, replacement in _SECRETS:
        result = pattern.sub(replacement, result)
    return result


def safe_traceback(exc, tb=None):
    """Bounded line numbers; no messages, paths, source, names or locals.

    Unknown exception class names are not trusted output either. Chained
    exception text is omitted. Full source tracebacks are deliberately avoided.
    """
    if tb is None and isinstance(exc, BaseException):
        tb = exc.__traceback__
    lines = []
    while tb is not None and len(lines) < 16:
        lines.append(str(tb.tb_lineno))
        tb = tb.tb_next
    return ("Traceback (safe): exception=" + _ERROR_TYPES.get(type(exc), "Exception")
            + " frame_lines=" + ",".join(lines) + (" truncated=true" if tb else ""))


class SafeFormatter(logging.Formatter):
    def formatException(self, exc_info):
        return safe_traceback(exc_info[1], exc_info[2])

    def formatStack(self, stack_info):
        return "Stack details omitted."

    def format(self, record):
        # Another handler may have cached an unsafe full traceback on record.
        safe_record = copy.copy(record)
        if safe_record.name == "nextgen" or safe_record.name.startswith("nextgen."):
            safe_record.name = "anime-discovery" + safe_record.name[len("nextgen"): ]
        safe_record.exc_text = None
        if not safe_record.exc_info and record.exc_text:
            safe_record.exc_text = "Exception details omitted."
        return redact(super().format(safe_record))


def configure_logging(settings: Settings, component: str, *, logger=None):
    """Validate then apply a local verbosity snapshot, without new handlers."""
    component_name(component)
    if not isinstance(settings, Settings):
        raise ValueError("invalid logging settings")
    selected_level = _LEVELS[settings.logging.level_for(component)]
    formatter = SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    target = logger if logger is not None else logging.getLogger("nextgen." + component)
    if not target.handlers:
        target.addHandler(logging.StreamHandler())
    for handler in target.handlers:
        handler.setLevel(selected_level)
        handler.setFormatter(formatter)
    target.setLevel(selected_level)
    target.propagate = False
    return target


def _message(name, fields):
    reason = fields.get("reason_code")
    validate_event(name, reason)
    view = DEFAULT_CATALOG.event(name)
    parts = ["event_code=" + name, "message=" + json.dumps(view["message"])]
    if reason is not None:
        parts.extend(("reason_code=" + reason,
                      "reason_message=" + json.dumps(DEFAULT_CATALOG.reason(reason)["message"])))
    cause = fields.get("cause_reason_code")
    if cause is not None:
        if get_reason(cause) is None:
            raise ValueError("unregistered diagnostic cause")
        parts.extend(("cause_reason_code=" + cause,
                      "cause_message=" + json.dumps(DEFAULT_CATALOG.reason(cause)["message"])))
    handler = fields.get("handler_code")
    label = fields.get("handler_label")
    invocation = fields.get("invocation_id")
    if name in HANDLER_EVENTS:
        definition = HandlerDefinition(handler, label)
        invocation_id(invocation)
        parts.extend(("handler_code=" + definition.code,
                      "handler_message=" + json.dumps(DEFAULT_CATALOG.handler(
                          definition.code, recorded_label=definition.label)["message"]),
                      "invocation_id=" + invocation))
    elif any(value is not None for value in (handler, label, invocation)):
        raise ValueError("handler metadata requires a handler event")
    activity = fields.get("activity_code")
    if activity is not None:
        if type(activity) is not str or activity not in JOB_ACTIVITIES:
            raise ValueError("unregistered diagnostic activity")
        parts.extend(("activity_code=" + activity, "activity_message=" + json.dumps(
            DEFAULT_CATALOG.state("job_activity", activity)["message"])))
    service = fields.get("service")
    if type(service) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", service):
        parts.append("service=" + service)
        status = fields.get("service_status")
        if type(status) is str and re.fullmatch(r"[A-Z_]{2,20}", status):
            parts.append("service_status=" + status)
        detail = fields.get("service_detail")
        if type(detail) is str and detail:
            parts.append("service_detail=" + json.dumps(redact(detail)[:300]))
    selected = {}
    for key in ("event_id", "job_id", "created_by_job_id", "attempt", "elapsed_ms", "config_revision"):
        value = fields.get(key)
        if value is None:
            continue
        try:
            selected[key] = integer(value, positive=key.endswith("_id") or key == "config_revision")
        except ValueError:
            continue
    correlation = trace_id(event_id=selected.get("event_id"), job_id=selected.get("job_id"))
    if correlation:
        selected["trace_id"] = correlation
    try:
        selected["component"] = component_name(fields.get("component"))
    except ValueError:
        pass
    if type(fields.get("retryable")) is bool:
        selected["retryable"] = str(fields["retryable"]).lower()
    outcome = fields.get("outcome")
    if type(outcome) is str and any(outcome in STATES[entity] for entity in ("job", "incoming_event")):
        selected["outcome"] = outcome
    error_type = fields.get("error_type")
    if type(error_type) is str and error_type in _ERROR_TYPES.values():
        selected["error_type"] = error_type
    parts.extend(key + "=" + str(value) for key, value in selected.items())
    return " ".join(parts)


def _emit(logger, level, text):
    if type(level) is not int or level not in _LEVELS.values():
        raise ValueError("invalid diagnostic level")
    try:
        if not logger.isEnabledFor(level):
            return False
        logger.log(level, text)
        return True
    except Exception:
        # A broken log sink cannot cause replay of already committed work.
        return False


def event(logger, level, name: str, **fields):
    """Validate registered emission even when filtered; drop unsafe extra facts."""
    return _emit(logger, level, _message(name, fields))


def exact_error(exc, *, database=None):
    """The real failure, including errors re-raised "from None".

    Each exception in the chain with its message and the file:line that raised
    it; OSError messages name the file. Only credential patterns are redacted.
    """
    import traceback
    parts, seen, current, is_sqlite = [], set(), exc, False
    while current is not None and id(current) not in seen and len(parts) < 8:
        seen.add(id(current))
        is_sqlite = is_sqlite or isinstance(current, sqlite3.Error)
        frames = traceback.extract_tb(current.__traceback__)
        # The raising line and the line that called it (e.g. which _check call).
        where = (" at " + " <- ".join(f.filename.rsplit("/src/", 1)[-1] + ":" + str(f.lineno) + " in " + f.name
                                      for f in reversed(frames[-2:]))) if frames else ""
        parts.append(type(current).__name__ + ": " + str(current) + where)
        current = current.__cause__ or current.__context__
    text = " <- ".join(parts)
    if is_sqlite and database:
        text += " (database " + str(database) + ")"
    return redact(text)[:2000]


def exception(logger, name: str, exc: Exception, *, level=logging.ERROR, database=None, **fields):
    """Catch-boundary diagnostic with the exact error."""
    fields["error_type"] = _ERROR_TYPES.get(type(exc), "Exception")
    return _emit(logger, level, _message(name, fields) + " error=" + exact_error(exc, database=database))


def log_history(logger, record):
    """Call after commit only; a receipt is not an automatic after-commit hook.

    Callers must discard receipts on rollback; this function never queries SQL.
    """
    details = json.loads(record.details_json)
    reason = get_reason(record.reason_code)
    level = _LEVELS[reason.severity] if reason else logging.INFO
    return event(logger, level, record.event_code,
                 reason_code=record.reason_code, component=record.component,
                 event_id=record.event_id, job_id=record.job_id,
                 created_by_job_id=record.created_by_job_id, attempt=details.get("attempt"),
                 cause_reason_code=details.get("cause_reason_code"), activity_code=record.activity_code,
                 handler_code=record.handler_code, handler_label=record.handler_label,
                 invocation_id=record.invocation_id)


class Span:
    def __init__(self):
        self.started = time.monotonic()

    def milliseconds(self):
        return max(0, int((time.monotonic() - self.started) * 1000))

