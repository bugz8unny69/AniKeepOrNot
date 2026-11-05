"""In-process service health monitor.

Tracks one state per registered service. A service changes state when:
- a probe result is recorded (record), or
- a real request fails or succeeds (mark_failed / mark_succeeded). This is
  passive and needs no extra network call.

Each change is returned once as a Transition, so callers log "went down" and
"came back" a single time instead of on every attempt.
"""
from dataclasses import dataclass, replace
import threading
import time

from src.nextgen.services.base import DOWN, NOT_A_FAILURE, UNKNOWN, UP, USABLE, ProbeResult


@dataclass(frozen=True)
class ServiceState:
    service: str
    label: str
    status: str = UNKNOWN
    detail: str = ''
    since_ms: int = 0          # When the current status began.
    checked_at_ms: int = 0     # Last probe or real request observation.
    next_probe_ms: int = 0     # Earliest time a network probe may run again.
    failures: int = 0          # Consecutive failed observations.

    @property
    def usable(self):
        return self.status in USABLE

    def message(self):
        return ProbeResult(self.service, self.status, self.detail, self.checked_at_ms).message(self.label)


@dataclass(frozen=True)
class Transition:
    service: str
    old: str
    new: str
    state: ServiceState


class ServiceHealth:
    def __init__(self, registry, *, clock=None, recheck_ms=60000,
                 min_backoff_ms=15000, max_backoff_ms=300000):
        if not 1000 <= min_backoff_ms <= max_backoff_ms or recheck_ms < 1000:
            raise ValueError('invalid health timing')
        self.registry = registry
        self.clock = clock or (lambda: time.time_ns() // 1000000)
        self.recheck_ms = recheck_ms
        self.min_backoff_ms = min_backoff_ms
        self.max_backoff_ms = max_backoff_ms
        self._lock = threading.Lock()
        self._states = {a.name: ServiceState(a.name, a.label) for a in registry}

    def now(self):
        return int(self.clock())

    def state(self, service):
        with self._lock:
            return self._get(service)

    def snapshot(self):
        with self._lock:
            return tuple(self._states.values())

    def is_available(self, service):
        return self.state(service).usable

    def unavailable(self, services):
        """The subset of services that currently block work, in the given order."""
        return tuple(s for s in services if not self.is_available(s))

    def _get(self, service):
        try:
            return self._states[service]
        except KeyError:
            raise ValueError(f'unknown service: {service}') from None

    def _apply(self, service, status, detail, now, *, probed):
        old = self._get(service)
        failed = status not in USABLE
        failures = old.failures + 1 if failed else 0
        if failed:
            delay = min(self.max_backoff_ms, self.min_backoff_ms * 2 ** min(failures - 1, 16))
        else:
            delay = self.recheck_ms
        new = replace(old, status=status, detail=detail,
                      since_ms=old.since_ms if status == old.status else now,
                      checked_at_ms=now, failures=failures,
                      next_probe_ms=now + delay if (probed or failed) else old.next_probe_ms)
        self._states[service] = new
        return Transition(service, old.status, status, new) if status != old.status else None

    def record(self, result):
        """Store a probe result. Returns a Transition when the status changed."""
        if type(result) is not ProbeResult:
            raise ValueError('probe result required')
        with self._lock:
            return self._apply(result.service, result.status, result.detail,
                               result.checked_at_ms or self.now(), probed=True)

    def mark_failed(self, service, exc=None, *, detail=None):
        """A real request failed. Uses the adapter's classify() for the status."""
        adapter = self.registry.get(service)
        status = adapter.classify(exc) if exc is not None else None
        if status == NOT_A_FAILURE:
            return None
        status = status or DOWN
        if status in USABLE:
            return None
        text = detail if detail is not None else (str(exc)[:200] if exc is not None else '')
        with self._lock:
            return self._apply(service, status, text, self.now(), probed=False)

    def mark_succeeded(self, service):
        """A real request succeeded, so the service is reachable again."""
        with self._lock:
            if self._get(service).status == UP:
                return None
            return self._apply(service, UP, '', self.now(), probed=False)

    def due(self, service):
        """Whether this service's probe may run now.

        Adapters with network_probe=False only read local records written by
        real requests, so scheduling them never adds traffic to the service.
        """
        self.registry.get(service)
        return self.now() >= self.state(service).next_probe_ms

    def probe(self, service, configuration, secrets):
        """Run one adapter probe and record it. Never raises for service errors."""
        adapter = self.registry.get(service)
        now = self.now()
        try:
            result = adapter.probe(configuration, secrets, now_ms=now)
        except Exception as exc:  # A probe bug must not stop the caller.
            status = adapter.classify(exc)
            status = DOWN if status in (None, NOT_A_FAILURE) else status
            result = ProbeResult(service, status, f'check failed: {type(exc).__name__}', now)
        return self.record(result)

    def probe_due(self, configuration, secrets):
        """Probe every service whose recheck time has come. Returns the transitions."""
        changes = []
        for adapter in self.registry:
            if self.due(adapter.name):
                change = self.probe(adapter.name, configuration, secrets)
                if change is not None:
                    changes.append(change)
        return tuple(changes)
