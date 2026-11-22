"""The SQLite adapter: every connection and write transaction goes through here.

The database is on a local disk: a write transaction takes milliseconds. One
writer that waits seconds means another transaction is doing slow work while
it holds the write lock (NFS file operations, network calls). The adapter
does not hide that with retries:

- Writers in this process take one write lock per database file before
  BEGIN IMMEDIATE, so the worker's job threads queue in order instead of
  racing SQLite's busy handler.
- Other processes (API, adm) wait through SQLite's busy timeout.
- A write transaction held longer than SLOW_TRANSACTION_S is logged with the
  code that opened it, so the slow work can be moved out of the transaction.
- Every connection gets the same pragmas.

A thread that already holds the write lock and opens a second write
transaction on another connection would wait on itself; SQLite answers that
at once, as it always did.
"""
from contextlib import contextmanager
import logging
import sqlite3
import threading
import time
import traceback

BUSY_TIMEOUT_MS = 60000
SLOW_TRANSACTION_S = 1.0
_logger = logging.getLogger("anime.storage")
_locks = {}
_locks_guard = threading.Lock()
_held = threading.local()


def configure(db, *, readonly=False, busy_timeout_ms=BUSY_TIMEOUT_MS):
    """The pragmas every connection uses."""
    db.execute("PRAGMA foreign_keys=ON")
    db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if not readonly:
        db.execute("PRAGMA synchronous=FULL")
    return db


def database_file(db):
    """The main database path of a connection ('' for :memory:)."""
    row = db.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""


def write_lock(path):
    with _locks_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = _locks[path] = threading.Lock()
        return lock


def _held_paths():
    paths = getattr(_held, "paths", None)
    if paths is None:
        paths = _held.paths = set()
    return paths


def _opened_by():
    """The application frame that opened the transaction, for the slow log."""
    for frame in reversed(traceback.extract_stack(limit=12)[:-3]):
        if "/storage/adapter.py" not in frame.filename and "contextlib" not in frame.filename \
                and not frame.filename.endswith("storage/sqlite.py"):
            return f"{frame.filename.rsplit('/src/', 1)[-1]}:{frame.lineno} {frame.name}"
    return "unknown"


@contextmanager
def write_transaction(db):
    """Own one atomic unit; reject nesting rather than committing caller work."""
    if db.in_transaction:
        raise ValueError("requires a connection outside an active transaction")
    path = database_file(db)
    held = _held_paths()
    lock = write_lock(path) if path and path not in held else None
    if lock is not None:
        lock.acquire()
        held.add(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            took = time.monotonic() - started
            if took > SLOW_TRANSACTION_S:
                _logger.warning("Slow database write: held the write lock %.1fs, opened at %s",
                                took, _opened_by())
    finally:
        if lock is not None:
            held.discard(path)
            lock.release()


def _migration_unsupported():
    from src.anime.storage.migrations import MigrationUnsupported
    return MigrationUnsupported


class DatabaseVersionMismatch(_migration_unsupported()):
    """The database schema is not the version this code was built for (an upgrade is half done).

    A MigrationUnsupported, so maintenance and the upgrade gate treat it as before.
    """
    reason_code = "DATABASE_VERSION_MISMATCH"

    def __init__(self, found, expected):
        RuntimeError.__init__(self, f"Database schema is v{found}; this code needs v{expected}.")
        self.found, self.expected = found, expected


def require_current(db):
    """Refuse to read or write a database at another schema version."""
    from src.anime.storage.migrations import LATEST_VERSION
    found = db.execute("PRAGMA user_version").fetchone()[0]
    if found != LATEST_VERSION:
        raise DatabaseVersionMismatch(found, LATEST_VERSION)
