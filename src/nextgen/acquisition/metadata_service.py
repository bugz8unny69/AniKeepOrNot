"""Shared metadata acquisition service for single-episode and batch torrents.

Unifies common responsibilities across single-episode intake (legacy_magnet,
sonarr_magnet_intake) and batch candidate intake (metainfo_source,
batch_metadata_admission):
- Concurrency limiting for metadata polling/discovery
- Durable metadata timeout checking (TORRENT_METADATA_TIMEOUT, 10 minutes)
- Strict torrent ownership and tag/folder validation
- Unified stopping and polling across qBittorrent client implementations
- Restart recovery and reason code mapping
"""
import asyncio
import inspect
from typing import Any

from src.nextgen.async_io import blocking

METADATA_TIMEOUT_SECONDS = 600
METADATA_TIMEOUT_MS = 600000
DEFAULT_METADATA_CONCURRENCY = 2
STOPPED = frozenset(('pausedDL', 'pausedUP', 'stoppedDL', 'stoppedUP'))


class MetadataSourceTimeout(RuntimeError):
    reason_code = 'TORRENT_METADATA_TIMEOUT'

    def __init__(self, message='An admitted magnet did not provide metadata within ten minutes.'):
        super().__init__(message)


class MetadataConcurrencyLimiter:
    """Worker-level concurrency limiter for metadata operations."""

    def __init__(self, limit: int = DEFAULT_METADATA_CONCURRENCY):
        self._limit = limit
        self._semaphore = asyncio.Semaphore(limit)

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    def slot(self):
        return self._semaphore


_SHARED_LIMITER = MetadataConcurrencyLimiter()


def shared_limiter() -> MetadataConcurrencyLimiter:
    return _SHARED_LIMITER


def is_metadata_expired(created_at_ms: int, now_ms: int, *, timeout_ms: int = METADATA_TIMEOUT_MS) -> bool:
    """True if metadata acquisition duration has exceeded the durable deadline."""
    if type(timeout_ms) is not int or timeout_ms < 60000:
        raise ValueError('Invalid metadata timeout value')
    return (now_ms - created_at_ms) >= timeout_ms


def verify_torrent_ownership(observation: Any, marker: str, download_root: str, category: str) -> bool:
    """Verify that an observed torrent matches our explicit ownership tags and isolation root.

    Prevents adopting foreign or external torrents, cross-category torrents, or
    torrents that belong to another instance or run.
    """
    if observation is None:
        return False
    expected_tag = 'ad-' + marker
    tags = getattr(observation, 'tags', ()) or ()
    save_path = getattr(observation, 'save_path', '') or ''
    cat = getattr(observation, 'category', '') or ''
    auto_tmm = getattr(observation, 'auto_tmm', None)

    if (save_path.rstrip('/') != download_root.rstrip('/')
            or cat != category
            or auto_tmm is not False
            or expected_tag not in tags
            or any(t.startswith('ad-') and t != expected_tag for t in tags)):
        return False
    return True


async def poll_or_stop_metadata(client: Any, intent: Any, current: Any, cancelled: asyncio.Event,
                                configuration: Any, *, semaphore: asyncio.Semaphore | None = None,
                                trace: dict | None = None) -> Any:
    """Shared polling and stopping sequence for legacy qBittorrent metadata acquisition."""
    if current.state not in STOPPED or getattr(intent, 'state', None) == 'OWNED_STOPPED':
        if trace is not None:
            trace['phase'] = 'recover_stop'
        return await blocking(client.stop_legacy_metadata, intent)

    if getattr(current, 'files', None):
        if trace is not None:
            trace['phase'] = 'inspect_existing'
        if hasattr(client, 'inspect_legacy_metadata'):
            return await blocking(client.inspect_legacy_metadata,
                                  intent.info_hash, intent.marker, intent.options.download_root)
        return current

    if trace is not None:
        trace['phase'] = 'poll'

    attempts = configuration.value('qbittorrent.metadata_poll_attempts')
    interval = configuration.value('qbittorrent.metadata_poll_seconds')

    async def _do_poll():
        if cancelled.is_set():
            return None
        kwargs = {'attempts': attempts, 'interval': interval}
        if trace is not None and 'trace' in inspect.signature(client.poll_legacy_metadata).parameters:
            kwargs['trace'] = trace
        return await blocking(client.poll_legacy_metadata, intent, cancelled, **kwargs)

    if semaphore is not None:
        async with semaphore:
            if cancelled.is_set():
                return None
            return await _do_poll()
    return await _do_poll()
