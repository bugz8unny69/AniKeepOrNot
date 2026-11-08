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

_QUIET_EVENTS = frozenset({"CLAIMED", "HANDLER_STARTED", "HANDLER_COMPLETED"})

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
    formatter = SafeFormatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    target = logger if logger is not None else logging.getLogger("nextgen." + component)
    if not target.handlers:
        target.addHandler(logging.StreamHandler())
    for handler in target.handlers:
        handler.setLevel(selected_level)
        handler.setFormatter(formatter)
    target.setLevel(selected_level)
    target.propagate = False
    return target


def _sentence(text):
    """Catalog copy as a log phrase: no final period."""
    return text[:-1] if text.endswith('.') else text


def _short_hash(value):
    return value[:8] + '…' if type(value) is str and re.fullmatch('[0-9a-f]{40}', value) else value


def _check_phrase(check, result=None):
    """'path.py:12 func NAME' -> 'check NAME passed · path.py:12'."""
    match = re.fullmatch(r'(\S+:\d+) \S+(?: ([A-Z0-9_]+))?', check)
    if not match:
        return 'check ' + _bounded(check)
    where, name = match.groups()
    return 'check ' + (name or 'unnamed') + (' ' + result if result else '') + ' · ' + where.replace('src/nextgen/', '')


def _message(name, fields, context=None):
    """One log line: who · what for · activity · what happened · facts (JD-1d).

    `context` is the job logger's current activity and subject, if set.
    Codes stay in History; the line uses the catalog's words.
    """
    reason = fields.get("reason_code")
    validate_event(name, reason)
    context = context or {}
    what = [_sentence(DEFAULT_CATALOG.event(name)["message"])]
    if reason is not None:
        what.append(_sentence(DEFAULT_CATALOG.reason(reason)["message"]))
    cause = fields.get("cause_reason_code")
    if cause is not None:
        if get_reason(cause) is None:
            raise ValueError("unregistered diagnostic cause")
        what.append("because: " + _sentence(DEFAULT_CATALOG.reason(cause)["message"]))
    handler = fields.get("handler_code")
    label = fields.get("handler_label")
    invocation = fields.get("invocation_id")
    activity = None
    if name in HANDLER_EVENTS:
        definition = HandlerDefinition(handler, label)
        invocation_id(invocation)
        activity = DEFAULT_CATALOG.handler(definition.code, recorded_label=definition.label)["message"]
    elif any(value is not None for value in (handler, label, invocation)):
        raise ValueError("handler metadata requires a handler event")
    activity_code = fields.get("activity_code")
    if activity_code is not None:
        if type(activity_code) is not str or activity_code not in JOB_ACTIVITIES:
            raise ValueError("unregistered diagnostic activity")
        what.append(_sentence(DEFAULT_CATALOG.state("job_activity", activity_code)["message"]))
    ids = {}
    for key in ("event_id", "job_id", "attempt", "elapsed_ms"):
        value = fields.get(key)
        if value is None:
            continue
        try:
            ids[key] = integer(value, positive=key.endswith("_id"))
        except ValueError:
            continue
    if "job_id" in ids:
        head = "job #" + str(ids["job_id"])
    elif "event_id" in ids:
        head = "release #" + str(ids["event_id"])
    else:
        try:
            head = component_name(fields.get("component"))
        except ValueError:
            head = "system"
    subject = fields.get("subject") or (context.get("subject") if context.get("job_id") in (None, ids.get("job_id")) else None)
    if type(subject) is str and subject:
        head += " " + _bounded(subject, 120)
    line = [head]
    # The logger's activity belongs to its own job, not to a job it creates.
    if context.get("job_id") in (None, ids.get("job_id")):
        activity = activity or context.get("activity")
    if type(activity) is str and activity:
        line.append(_sentence(_bounded(activity, 120)))
    if name not in ("CHECK_PASSED", "CHECK_FAILED"):         # the check phrase says it
        line.append(" · ".join(what))
    facts = []
    check = fields.get("check") or fields.get("failed_check")
    if type(check) is str and check and check != "not recorded":
        facts.append(_check_phrase(check, fields.get("result") if fields.get("check") else "failed"))
    elif check == "not recorded":
        facts.append("no check recorded")
    actual, expected = fields.get("actual"), fields.get("expected")
    if type(actual) is str and actual:
        facts.append(_bounded(str(_short_hash(actual))) + ("" if not expected else " (expected " + _bounded(str(_short_hash(expected))) + ")"))
    elif type(expected) is str and expected:
        facts.append("expected " + _bounded(expected))
    for key in ("path", "failed_path"):
        value = fields.get(key)
        if type(value) is str and value:
            facts.append(_bounded(value))
    sizes = [(label_, fields.get(key)) for key, label_ in (("video_mb", "video"), ("plan_used_mb", "used"), ("limit_mb", "limit"))]
    sizes = [f"{label_} {value} MB" for label_, value in sizes if type(value) is int and value >= 0]
    if sizes:
        facts.append(", ".join(sizes))
    for key, phrase in (("blocking_job_id", "waiting on job #"), ("operation_id", "operation #")):
        value = fields.get(key)
        if type(value) is int and value > 0:
            facts.append(phrase + str(value))
    service = fields.get("service")
    if type(service) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", service):
        status = fields.get("service_status")
        status = " " + status if type(status) is str and re.fullmatch(r"[A-Z_]{2,20}", status) else ""
        detail = fields.get("service_detail")
        detail = ": " + redact(detail)[:300] if type(detail) is str and detail else ""
        facts.append("service " + service + status + _bounded(detail))
    outcome = fields.get("outcome")
    if type(outcome) is str and any(outcome in STATES[entity] for entity in ("job", "incoming_event")):
        facts.append("ended " + outcome)
    if type(fields.get("retryable")) is bool and fields["retryable"]:
        facts.append("will retry")
    try:
        emitter = component_name(fields.get("component"))
    except ValueError:
        emitter = None
    if emitter not in (None, "worker") and not head.startswith(emitter):
        facts.append("from " + emitter)
    if ids.get("attempt", 0) > 1:
        facts.append("attempt " + str(ids["attempt"]))
    if "elapsed_ms" in ids:
        facts.append(_duration(ids["elapsed_ms"]))
    return " · ".join(line + facts)


def _duration(ms):
    seconds = ms // 1000
    if seconds < 60:
        return f"in {ms / 1000:.1f}s" if ms < 10000 else f"in {seconds}s"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"in {hours}h{minutes:02d}m" if hours else f"in {minutes}m{seconds:02d}s"


def _bounded(text, limit=300):
    """A log-safe value: no control characters, at most `limit` characters."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(text))
    return text if len(text) <= limit else text[:limit - 1] + "…"


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
    return _emit(logger, level, _message(name, fields, getattr(logger, "nextgen_context", None)))


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
    return _emit(logger, level, _message(name, fields, getattr(logger, "nextgen_context", None))
                 + " · error: " + exact_error(exc, database=database))


def log_history(logger, record):
    """Call after commit only; a receipt is not an automatic after-commit hook.

    Callers must discard receipts on rollback; this function never queries SQL.
    """
    details = json.loads(record.details_json)
    reason = get_reason(record.reason_code)
    level = _LEVELS[reason.severity] if reason else logging.INFO
    # INFO shows lifecycle changes only (JD-1d). Claiming and handler start/end
    # repeat every attempt; a wait repeated on later attempts is a poll.
    if record.event_code in _QUIET_EVENTS or (record.event_code == "DEFERRED" and (details.get("attempt") or 0) > 1):
        level = min(level, logging.DEBUG)
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

