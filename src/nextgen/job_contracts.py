"""Trusted worker registration and typed outcomes; no business handlers."""
from dataclasses import dataclass, field
import inspect
import json
from types import MappingProxyType

from src.nextgen.handlers import HandlerCatalog, HandlerDefinition
from src.nextgen.messages import validate_job_activity
from src.nextgen.reasons import get_reason
from src.nextgen.matching import from_json as matching_from_json


@dataclass(frozen=True)
class Policy:
    lease_ms: int = 300000
    poll_ms: int = 1000
    max_retries: int = 3
    backoff_ms: int = 1000
    max_backoff_ms: int = 60000

    def __post_init__(self):
        for name, lower, upper in (("lease_ms",1000,86400000), ("poll_ms",10,60000),
                ("max_retries",0,100), ("backoff_ms",1,86400000), ("max_backoff_ms",1,86400000)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid worker policy")
        if self.max_backoff_ms < self.backoff_ms:
            raise ValueError("invalid worker backoff bounds")


def payload_json(value):
    if type(value) is not dict:
        raise ValueError("job payload must be an object")
    raw = json.dumps(value, allow_nan=False, sort_keys=True)
    if len(raw.encode("utf-8")) > 65536:
        raise ValueError("job payload exceeds worker limit")
    return raw


def parse_payload(raw):
    if type(raw) is not str or len(raw.encode("utf-8")) > 65536:
        raise ValueError("invalid job payload")
    def pairs(entries):
        result = {}
        for key,value in entries:
            if key in result:
                raise ValueError("duplicate job payload key")
            result[key] = value
        return result
    value = json.loads(raw,object_pairs_hook=pairs)
    payload_json(value)  # Object, finite values and encoded-size validation.
    return value


@dataclass(frozen=True)
class Successor:
    kind: str
    payload: str = field(repr=False)  # Detached canonical JSON, not a mutable map.

    @classmethod
    def create(cls, kind, payload):
        return cls(kind, payload_json(payload))

    def __post_init__(self):
        if type(self.kind) is not str or not self.kind.strip() or len(self.kind) > 64:
            raise ValueError("invalid successor kind")
        if type(self.payload) is not str or len(self.payload.encode("utf-8")) > 65536:
            raise ValueError("invalid successor payload")
        parse_payload(self.payload)


_STATES = {"complete":"DONE", "skip":"DONE", "retry":"RETRY_WAIT", "defer":"RETRY_WAIT",
           "hold":"ON_HOLD", "review":"NEEDS_REVIEW", "blocked":"BLOCKED", "cancel":"CANCELED"}


@dataclass(frozen=True)
class Outcome:
    kind: str = "complete"
    reason_code: str | None = None
    delay_ms: int | None = None
    activity_code: str | None = None
    value: object = field(default=None, repr=False)
    matching: str | None = field(default=None, repr=False)
    facts: dict | None = field(default=None, repr=False)
    search_snapshots: tuple | list | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.search_snapshots is not None and not isinstance(self.search_snapshots, (list, tuple)):
            raise ValueError("invalid outcome search snapshots")
        if self.facts is not None and (type(self.facts) is not dict or self.kind not in ("review", "hold", "blocked", "retry", "cancel")
                or set(self.facts) - {"video_mb", "limit_mb", "plan_used_mb", "failed_check", "failed_path", "observed_audio", "missing_audio"}
                or any((type(v) is not str) if k in ("failed_check", "failed_path", "observed_audio", "missing_audio") else (type(v) is not int or v < 0)
                       for k, v in self.facts.items())):
            raise ValueError("invalid outcome facts")
        if type(self.kind) is not str or self.kind not in _STATES:
            raise ValueError("invalid handler outcome")
        if self.kind in ("complete", "skip"):
            if self.reason_code is not None:
                raise ValueError("completed outcome cannot carry a reason")
        elif get_reason(self.reason_code) is None:
            raise ValueError("registered outcome reason required")
        if self.kind == "skip" and self.activity_code != "SKIPPED_DUPLICATE":
            raise ValueError("skip requires duplicate activity")
        if self.delay_ms is not None and (self.kind not in ("retry", "defer")
                or type(self.delay_ms) is not int or not 1 <= self.delay_ms <= 86400000):
            raise ValueError("invalid outcome delay")
        if self.kind == "defer" and self.delay_ms is None:
            raise ValueError("deferred work requires a delay")
        if self.kind not in ("complete", "cancel") and self.value is not None:
            raise ValueError("only completion can publish result facts")
        if self.matching is not None:
            matching_from_json(self.matching)
        validate_job_activity(_STATES[self.kind], self.activity_code)


class CompletionRejected(ValueError):
    """Expected domain rejection; worker rolls back facts then records outcome."""
    def __init__(self, outcome):
        if type(outcome) is not Outcome or outcome.kind not in ("review", "hold", "blocked", "skip"):
            raise ValueError("invalid completion rejection")
        self.outcome = outcome
        self.reason_code = outcome.reason_code
        super().__init__("Completion requires review of its recorded reason.")


@dataclass(frozen=True)
class Registration:
    kind: str
    definition: HandlerDefinition
    execute: object = field(repr=False)
    mode: str = "async"
    successor_kinds: tuple[str, ...] = ()
    route: object = field(default=None, repr=False)
    persist: object = field(default=None, repr=False)

    def __post_init__(self):
        if (type(self.kind) is not str or not self.kind.strip() or len(self.kind) > 64
                or type(self.definition) is not HandlerDefinition or not callable(self.execute)
                or self.mode not in ("async", "thread")):
            raise ValueError("invalid handler registration")
        if inspect.iscoroutinefunction(self.execute) != (self.mode == "async"):
            raise ValueError("handler execution mode does not match callable")
        if type(self.successor_kinds) is not tuple or any(type(x) is not str for x in self.successor_kinds):
            raise ValueError("invalid allowed successors")
        for callback in (self.route, self.persist):
            if callback is not None and (not callable(callback) or inspect.iscoroutinefunction(callback)):
                raise ValueError("completion adapters must be synchronous trusted callables")


class Registry:
    def __init__(self, registrations):
        entries, labels = {}, {}
        for spec in tuple(registrations):
            if type(spec) is not Registration or spec.kind in entries:
                raise ValueError("duplicate or invalid handler registration")
            if spec.definition.code in labels and labels[spec.definition.code] != spec.definition:
                raise ValueError("conflicting handler display registration")
            entries[spec.kind] = spec
            labels[spec.definition.code] = spec.definition
        if any(kind not in entries for spec in entries.values() for kind in spec.successor_kinds):
            raise ValueError("allowed successor is not installed")
        self.entries = MappingProxyType(entries)
        self.labels = HandlerCatalog(tuple(labels.values()))
