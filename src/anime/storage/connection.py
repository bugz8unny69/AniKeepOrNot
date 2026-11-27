"""One database connection for the API, adm and the identity bridge: current schema only.

Errors pass through as themselves (sqlite's own message and the full trace).
"""
from contextlib import contextmanager


@contextmanager
def database(path):
    from src.anime.storage.adapter import require_current
    from src.anime.storage.migrations import _open
    db = _open(path, readonly=False, timeout_ms=5000)
    try:
        require_current(db)
        yield db
    finally:
        db.close()
