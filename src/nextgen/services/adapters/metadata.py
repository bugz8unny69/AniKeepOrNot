"""Metadata providers: AniDB, Anime-Lists and TVDB.

These adapters never contact the provider. AniDB bans clients that send too
many requests, so health comes from the local records that every real request
already maintains (providers/http.py):
- provider_limits: consecutive failures and the cooldown before the next request;
- provider_public_cache: the downloaded data and when it was fetched.
"""
import sqlite3

from src.nextgen.services.base import DOWN, NOT_A_FAILURE, NOT_CONFIGURED, UNKNOWN, UP, ServiceAdapter


def _age(ms):
    minutes = max(0, ms) // 60000
    if minutes < 60:
        return f'{minutes} min'
    if minutes < 48 * 60:
        return f'{minutes // 60} h'
    return f'{minutes // 1440} days'


class _ProviderAdapter(ServiceAdapter):
    network_probe = False
    providers = ()          # Rows in provider_limits that belong to this service.
    cache_providers = ()    # Rows in provider_public_cache that hold its data.

    def configured(self, configuration, secrets):
        return None         # None means configured; otherwise a detail string.

    def _open(self, configuration):
        from src.nextgen.storage.migrations import _open
        return _open(configuration.value('storage.database_path'), readonly=True, timeout_ms=2000)

    def probe(self, configuration, secrets, *, now_ms=0):
        problem = self.configured(configuration, secrets)
        if problem:
            return self.result(NOT_CONFIGURED, problem, now_ms=now_ms)
        try:
            db = self._open(configuration)
        except Exception:
            return self.result(UNKNOWN, 'the local provider cache could not be read', now_ms=now_ms)
        try:
            marks = ','.join('?' * len(self.providers))
            rows = db.execute(f'SELECT provider,next_allowed_at_ms,failures FROM provider_limits '
                              f'WHERE provider IN ({marks})', self.providers).fetchall()
            cached = None
            if self.cache_providers:
                marks = ','.join('?' * len(self.cache_providers))
                cached = db.execute(f'SELECT max(fetched_at_ms),count(*) FROM provider_public_cache '
                                    f'WHERE provider IN ({marks})', self.cache_providers).fetchone()
        except sqlite3.Error:
            return self.result(UNKNOWN, 'the local provider cache could not be read', now_ms=now_ms)
        finally:
            db.close()
        failing = [(p, until, n) for p, until, n in rows if n > 0]
        if failing:
            until = max(u for _, u, _ in failing)
            count = max(n for _, _, n in failing)
            wait = f'; next request allowed in {_age(until - now_ms)}' if until > now_ms else ''
            return self.result(DOWN, f'{count} recent failed request(s){wait}', now_ms=now_ms)
        if not rows:
            return self.result(UNKNOWN, 'no requests have been made yet', now_ms=now_ms)
        if cached and cached[1]:
            return self.result(UP, f'{cached[1]} cached item(s), newest {_age(now_ms - cached[0])} old',
                               now_ms=now_ms)
        return self.result(UP, 'last requests succeeded', now_ms=now_ms)

    def classify(self, exc):
        from src.nextgen.providers.errors import (
            ProviderAuthenticationFailed, ProviderConfigurationError, ProviderDeferred,
            ProviderUnavailable,
        )
        if isinstance(exc, ProviderConfigurationError):
            return NOT_CONFIGURED
        if isinstance(exc, ProviderAuthenticationFailed):
            from src.nextgen.services.base import AUTH_FAILED
            return AUTH_FAILED
        if isinstance(exc, ProviderDeferred):
            return NOT_A_FAILURE     # A planned pacing wait, not an outage.
        if isinstance(exc, ProviderUnavailable):
            return DOWN
        return None


class AnidbAdapter(_ProviderAdapter):
    name = 'anidb'
    label = 'AniDB'
    providers = ('anidb', 'anidb_titles')
    cache_providers = ('anidb', 'anidb_titles')

    def configured(self, configuration, secrets):
        if not configuration.value('anidb.client_name'):
            return 'set the registered AniDB client name in Settings'
        return None


class AnimeListsAdapter(_ProviderAdapter):
    name = 'anime_lists'
    label = 'Anime-Lists'
    providers = ('anime_lists',)
    cache_providers = ('anime_lists',)


class TvdbAdapter(_ProviderAdapter):
    name = 'tvdb'
    label = 'TVDB'
    providers = ('tvdb',)

    def probe(self, configuration, secrets, *, now_ms=0):
        _, problem = self.secret(secrets, 'tvdb_credentials', 'set the TVDB API key in Settings',
                                 now_ms=now_ms)
        return problem or super().probe(configuration, secrets, now_ms=now_ms)
