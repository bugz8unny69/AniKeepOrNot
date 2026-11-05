"""Central authoritative qBittorrent readiness validation and snapshot caching."""
import logging
import time

from src.nextgen.acquisition.qbittorrent import Client
from src.nextgen.acquisition.settings import (
    download_options, DownloadConfigurationError, DownloadOptions, DownloadCredentials,
)
from src.nextgen.acquisition.submission import (
    ClientAuthenticationFailed, ClientInvalid, ClientUnavailable, ClientUnsupported,
)
from src.nextgen.configuration_schema import ConfigurationError
from src.nextgen.diagnostics import event

ACQUISITION_KINDS = (
    'PREPARE_ACQUISITION', 'SUBMIT_TORRENT', 'DOWNLOAD_TORRENT',
    'BATCH_SUBMIT', 'BATCH_TRANSFER', 'BATCH_UNLOCK',
)


def error_reason(exc):
    if isinstance(exc, (DownloadConfigurationError, ConfigurationError)):
        return 'DOWNLOAD_CONFIGURATION_INVALID'
    if isinstance(exc, ClientAuthenticationFailed):
        return 'DOWNLOAD_CLIENT_AUTH_FAILED'
    if isinstance(exc, ClientUnsupported):
        return 'DOWNLOAD_CLIENT_UNSUPPORTED'
    if isinstance(exc, ClientUnavailable):
        return 'DOWNLOAD_CLIENT_UNAVAILABLE'
    if isinstance(exc, ClientInvalid):
        return 'DOWNLOAD_CLIENT_RESPONSE_INVALID'
    return getattr(exc, 'reason_code', 'DOWNLOAD_CONFIGURATION_INVALID')


class QbittorrentReadiness:
    """Authoritative qBittorrent readiness validator with snapshot caching and bounded backoff."""

    def __init__(self, *, client_factory=Client, min_backoff_ms=1000, max_backoff_ms=30000):
        self.client_factory = client_factory
        self.min_backoff_ms = min_backoff_ms
        self.max_backoff_ms = max_backoff_ms
        self._cached_snapshot = None
        self._cached_ready = False
        self._cached_strategy = None
        self._last_error = None
        self._last_check_ms = 0
        self._backoff_ms = min_backoff_ms
        self._check_count = 0

    @property
    def check_count(self):
        return self._check_count

    def snapshot(self, configuration, secrets):
        """Extract validated immutable (options, credentials) snapshot."""
        if secrets is None:
            raise DownloadConfigurationError()
        options = download_options(configuration)
        credentials = secrets.qbittorrent_credentials()
        return options, credentials

    def check(self, configuration, secrets, *, now_ms=None):
        """Authoritatively validate readiness.

        Validates settings, authentication, supported versions, and metadata strategy.
        Caches successful readiness for the configuration/credential snapshot.
        """
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        try:
            current_snapshot = self.snapshot(configuration, secrets)
        except (ConfigurationError, DownloadConfigurationError) as exc:
            self._cached_ready = False
            self._cached_snapshot = None
            self._last_error = exc
            self._last_check_ms = now
            raise

        # Configuration or credential revision changes invalidate cached readiness
        if self._cached_snapshot != current_snapshot:
            self._cached_snapshot = current_snapshot
            self._cached_ready = False
            self._cached_strategy = None
            self._last_error = None
            self._backoff_ms = self.min_backoff_ms

        if self._cached_ready:
            return self._cached_strategy

        # If previous check failed and backoff hasn't elapsed, reuse the error without network requests
        if self._last_error is not None and now < self._last_check_ms + self._backoff_ms:
            raise self._last_error

        # Remote readiness check
        options, credentials = current_snapshot
        client = self.client_factory(options, credentials)
        self._check_count += 1
        try:
            self._last_check_ms = now
            client.login()
            strategy = client.metadata_strategy
            if strategy not in ('fetch', 'guarded_export'):
                raise ClientUnsupported()
            self._cached_ready = True
            self._cached_strategy = strategy
            self._last_error = None
            self._backoff_ms = self.min_backoff_ms
            return strategy
        except ClientUnavailable as exc:
            self._cached_ready = False
            self._last_error = exc
            self._backoff_ms = min(self._backoff_ms * 2, self.max_backoff_ms)
            raise
        except (ClientAuthenticationFailed, ClientUnsupported, ClientInvalid, Exception) as exc:
            self._cached_ready = False
            self._last_error = exc
            raise

    def is_ready(self, configuration, secrets, *, now_ms=None):
        """Non-raising readiness check returning bool."""
        try:
            self.check(configuration, secrets, now_ms=now_ms)
            return True
        except Exception:
            return False

    def last_error(self):
        return self._last_error
