"""The last known status of each service, shared between the worker and the API.

The worker keeps service health in memory and writes it here whenever it
checks a service. The API runs in another process: it reads this table for
the dashboard status bar and to refuse a Keep while Sonarr is unavailable.
"""
from dataclasses import dataclass
import sqlite3
import time

from src.anime.services.base import ProbeResult, USABLE

# A status older than this was written by a worker that is no longer
# checking (stopped or stuck); the dashboard says so instead of trusting it.
STALE_MS = 15 * 60 * 1000


@dataclass(frozen=True)
class StoredStatus:
    service: str
    label: str
    status: str
    detail: str
    since_ms: int
    checked_at_ms: int
    updated_at_ms: int

    @property
    def usable(self):
        return self.status in USABLE

    def message(self):
        return ProbeResult(self.service, self.status, self.detail, self.checked_at_ms).message(self.label)

    def stale(self, now_ms):
        return now_ms - self.updated_at_ms > STALE_MS


def _now():
    return time.time_ns() // 1000000


def _open(database, *, readonly):
    from src.anime.storage.migrations import _open as open_database
    return open_database(database, readonly=readonly, timeout_ms=2000)


def save(database, states, *, now_ms=None):
    """Write the given ServiceState values. Unchanged rows only get a new updated_at_ms."""
    now = _now() if now_ms is None else now_ms
    db = _open(database, readonly=False)
    try:
        with db:
            db.executemany(
                'INSERT INTO service_status(service,label,status,detail,since_ms,checked_at_ms,updated_at_ms) '
                'VALUES(?,?,?,?,?,?,?) ON CONFLICT(service) DO UPDATE SET label=excluded.label,'
                'status=excluded.status,detail=excluded.detail,since_ms=excluded.since_ms,'
                'checked_at_ms=excluded.checked_at_ms,updated_at_ms=excluded.updated_at_ms',
                [(s.service, s.label, s.status, s.detail[:500], max(0, s.since_ms),
                  max(0, s.checked_at_ms), now) for s in states])
    finally:
        db.close()


def load(database):
    """Every stored status by service name; empty if the worker has not written yet."""
    try:
        db = _open(database, readonly=True)
    except (OSError, sqlite3.Error, RuntimeError):  # Missing file or not a database.
        return {}
    try:
        rows = db.execute('SELECT service,label,status,detail,since_ms,checked_at_ms,updated_at_ms '
                          'FROM service_status ORDER BY service').fetchall()
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    return {row[0]: StoredStatus(*row) for row in rows}


def read(db, service):
    """One stored status through the caller's connection, or None."""
    try:
        row = db.execute('SELECT service,label,status,detail,since_ms,checked_at_ms,updated_at_ms '
                         'FROM service_status WHERE service=?', (service,)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else StoredStatus(*row)


def blocking(db, service, *, now_ms=None):
    """The stored status if it rules out a feature that needs this service right now.

    Used where a person asks for work, such as the Keep button: the request is
    refused with the reason instead of queueing a job that cannot run. A
    service that is turned off blocks here (Keep needs Sonarr), though it never
    blocks the worker. UNKNOWN, no record, or a record from a worker that
    stopped checking (stale) do not block: the job then decides.
    """
    from src.anime.services.base import UNKNOWN, UP
    stored = read(db, service)
    if stored is None or stored.status in (UP, UNKNOWN) or stored.stale(_now() if now_ms is None else now_ms):
        return None
    return stored
