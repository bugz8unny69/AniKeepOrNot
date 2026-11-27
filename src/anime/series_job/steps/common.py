"""Shared step types and results for the series job."""
from dataclasses import dataclass
import errno
import sqlite3

STEP_SEQUENCE = (
    "get_metadata",
    "check_release",
    "reserve_episodes",
    "download",
    "check_media",
    "transcode",
    "stage",
)


@dataclass(frozen=True)
class StepResult:
    action: str  # "done", "wait", "drop"
    next_step: str | None = None
    wake_at_ms: int | None = None
    why: str | None = None

    @classmethod
    def done(cls, next_step: str | None = None):
        return cls(action="done", next_step=next_step)

    @classmethod
    def wait(cls, until_ms: int | None = None, why: str | None = None):
        return cls(action="wait", wake_at_ms=until_ms, why=why)

    @classmethod
    def drop(cls, why: str):
        return cls(action="drop", why=why)


def next_step_name(current_step: str) -> str:
    """Return the name of the next step following current_step, or 'done' if at the end."""
    try:
        idx = STEP_SEQUENCE.index(current_step)
        if idx + 1 < len(STEP_SEQUENCE):
            return STEP_SEQUENCE[idx + 1]
    except ValueError:
        pass
    return "done"


SERVICE_WAIT_MS = 60_000           # qBittorrent down: try again in a minute.
SETUP_WAIT_MS = 10 * 60_000        # Not set up (address, login, folder): the series sleeps; Retry wakes it.

# Errors of the machine or the network, not the code: a full, failing or unreachable disk
# (the staging share is a network mount), memory, a network that is down.
_OUTAGE_ERRNOS = frozenset(getattr(errno, name) for name in (
    "ENOSPC", "EDQUOT", "EIO", "EROFS", "ENOMEM", "EAGAIN", "ESTALE", "ETIMEDOUT",
    "ENETDOWN", "ENETUNREACH", "EHOSTDOWN", "EHOSTUNREACH", "ECONNABORTED") if hasattr(errno, name))
# SQLite busy, locked, full or failing; other SQLite errors (no such table, a broken constraint) are bugs.
_OUTAGE_SQLITE = ("SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_FULL", "SQLITE_IOERR", "SQLITE_CANTOPEN",
                  "SQLITE_PROTOCOL")


def outage(exc):
    """The reason code when exc is an outage no step typed, or None when it is a program error.

    Owner, 2026-11-27: a program error stops the series, so an outage must never look like
    one. Every service client raises its own typed error; this is the net under them for
    what reaches the worker raw: a connection reset or timeout, a disk that is full or gone,
    a busy database. The series waits and tries again; nothing is stopped.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "NETWORK_UNAVAILABLE"
    if isinstance(exc, OSError) and exc.errno in _OUTAGE_ERRNOS:
        return "STORAGE_UNAVAILABLE"
    if isinstance(exc, sqlite3.OperationalError):
        name = getattr(exc, "sqlite_errorname", None) or ""
        # Without an error name (older SQLite), busy and locked are told apart by the message.
        if name.startswith(_OUTAGE_SQLITE) or (not name and any(
                word in str(exc).lower() for word in ("locked", "busy", "disk", "i/o"))):
            return "DATABASE_ERROR"
    return None


def where(exc):
    """file:line (ErrorType) where exc was raised, for the series history and the bell."""
    import os
    import traceback
    frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
    at = f"{os.path.basename(frame.filename)}:{frame.lineno} " if frame else ""
    return f"{at}({type(exc).__name__})"


async def with_client(context, torrent, work):
    """Run work(client) in a thread with a logged-in client for this torrent.

    Returns work's StepResult. qBittorrent being down or not set up is a wait
    (the series sleeps, the torrent keeps its place); an answer that doesn't fit
    (ClientInvalid: not our torrent, a bad response) drops the torrent with why.
    Anything else is a program error and passes through.
    """
    from src.anime.acquisition.settings import DownloadConfigurationError
    from src.anime.acquisition.submission import (ClientAuthenticationFailed, ClientInvalid,
                                                  ClientUnavailable, ClientUnsupported)
    from src.anime.async_io import blocking
    from src.anime.configuration_schema import ConfigurationError
    from src.anime.series_job.torrents import marker
    from src.anime.secrets import SecretStorageError
    from src.anime.acquisition.metadata import MetadataInvalid

    def run():
        m = marker(torrent)
        prefix = torrent.get("qb", {}).get("prefix", "akon")
        client = (context.torrents.client(context.configuration, m, prefix=prefix)
                  if prefix != "akon" else context.torrents.client(context.configuration, m))
        return work(client)
    try:
        return await blocking(run)
    except ClientUnavailable:
        return StepResult.wait(context.now + SERVICE_WAIT_MS, why="SERVICE_UNAVAILABLE")
    except (ClientAuthenticationFailed, ClientUnsupported, DownloadConfigurationError, ConfigurationError,
            SecretStorageError):
        return StepResult.wait(context.now + SETUP_WAIT_MS, why="SERVICE_SETUP_INVALID")
    except ClientInvalid as exc:
        return StepResult.drop(f"SERVICE_RESPONSE_INVALID: {exc}"[:200])
    except MetadataInvalid:
        return StepResult.drop("ACQUISITION_METADATA_INVALID")
