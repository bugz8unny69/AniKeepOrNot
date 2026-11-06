"""Metadata providers: AniDB, Anime-Lists and TVDB.

The regular checks never contact the provider. AniDB bans clients that send
too many requests, so health comes from the local records that every real
request already maintains (providers/http.py):
- provider_limits: consecutive failures and the cooldown before the next request;
- provider_public_cache: the downloaded data and when it was fetched.

At worker startup, AniDB and TVDB are required and are checked for real
(startup_probe), because a client name or API key being set does not prove it
is accepted. The AniDB check sends at most one request, through the same
pacing as real requests, and none at all when a request succeeded in the last
24 hours or a cooldown is active.
"""
import sqlite3

from src.nextgen.services.base import (
    AUTH_FAILED, DOWN, NOT_A_FAILURE, NOT_CONFIGURED, UNKNOWN, UP, ServiceAdapter,
)


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

    def __init__(self, *, opener=None):
        self.opener = opener   # Tests only: replaces the network for startup checks.

    def _client(self, configuration):
        from src.nextgen.providers.configuration import options_from_configuration
        from src.nextgen.providers.http import Clients
        return Clients(configuration.value('storage.database_path'), options_from_configuration(configuration),
                      opener=self.opener)

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
        cooling = [(p, until, n) for p, until, n in failing if until > now_ms]
        if cooling:
            # Down only while a cooldown is active. Only a successful request
            # resets the failure count, so treating an expired cooldown as down
            # would stop the very requests that can recover the service.
            until = max(u for _, u, _ in cooling)
            count = max(n for _, _, n in cooling)
            return self.result(DOWN, f'{count} recent failed request(s); next request allowed in '
                               f'{_age(until - now_ms)}', now_ms=now_ms)
        if failing:
            count = max(n for _, _, n in failing)
            return self.result(UNKNOWN, f'{count} recent failed request(s); the next request will '
                               'show whether it has recovered', now_ms=now_ms)
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
            return AUTH_FAILED
        if isinstance(exc, ProviderDeferred):
            return NOT_A_FAILURE     # A planned pacing wait, not an outage.
        if isinstance(exc, ProviderUnavailable):
            return DOWN
        return None


CONFIRMED_MS = 24 * 3600 * 1000   # An AniDB answer this recent proves the setup.


class AnidbAdapter(_ProviderAdapter):
    name = 'anidb'
    label = 'AniDB'
    required = True
    providers = ('anidb', 'anidb_titles')
    cache_providers = ('anidb', 'anidb_titles')

    def configured(self, configuration, secrets):
        if not configuration.value('anidb.client_name'):
            return 'set the registered AniDB client name in Settings'
        return None

    def _record(self, configuration):
        """(next_allowed_at_ms, failures, newest anime answer) for the AniDB HTTP API, or None."""
        db = self._open(configuration)
        try:
            limit = db.execute("SELECT next_allowed_at_ms,failures FROM provider_limits "
                               "WHERE provider='anidb'").fetchone()
            newest = db.execute("SELECT max(fetched_at_ms) FROM provider_public_cache "
                                "WHERE provider='anidb'").fetchone()[0]
        finally:
            db.close()
        return (*(limit or (0, 0)), newest)

    def startup_probe(self, configuration, secrets, *, now_ms=0, previous=None):
        from src.nextgen.providers.errors import (
            ProviderConfigurationError, ProviderDeferred, ProviderInvalidResponse, ProviderUnavailable,
        )
        problem = self.configured(configuration, secrets)
        if problem:
            return self.result(NOT_CONFIGURED, problem, now_ms=now_ms)
        try:
            next_allowed, failures, newest = self._record(configuration)
        except Exception:
            return self.result(UNKNOWN, 'the local provider cache could not be read', now_ms=now_ms)
        # Only the anime lookups prove the client is accepted; the titles
        # download does not send the client name.
        if not failures and newest is not None and now_ms - newest < CONFIRMED_MS:
            return self.result(UP, f'a request succeeded {_age(now_ms - newest)} ago', now_ms=now_ms)
        if failures and next_allowed > now_ms:
            # Never send during a cooldown, so restarts cannot add requests.
            # Keep the verdict the last worker reached, if it had one.
            wait = _age(next_allowed - now_ms)
            if previous is not None and previous.status == AUTH_FAILED:
                return self.result(AUTH_FAILED, previous.detail, now_ms=now_ms)
            return self.result(DOWN, f'{failures} recent failed request(s); next request allowed in {wait}',
                               now_ms=now_ms)
        try:
            client = self._client(configuration)
            error = client.anidb_check()
        except ProviderConfigurationError:
            return self.result(NOT_CONFIGURED, 'the AniDB client name or version is not valid', now_ms=now_ms)
        except ProviderDeferred:
            return self.result(UNKNOWN, 'a request was sent moments ago; the next one will show the status',
                               now_ms=now_ms)
        except ProviderUnavailable:
            return self.result(DOWN, 'the AniDB HTTP API did not answer', now_ms=now_ms)
        except ProviderInvalidResponse:
            return self.result(DOWN, 'the AniDB HTTP API sent an unexpected answer', now_ms=now_ms)
        if error is None:
            return self.result(UP, 'a test request succeeded', now_ms=now_ms)
        name = configuration.value('anidb.client_name')
        if 'client' in error.lower():
            return self.result(AUTH_FAILED, f'AniDB did not accept the client "{name}" (it said: {error}); '
                               'check the registered client name and version in Settings', now_ms=now_ms)
        if 'banned' in error.lower():
            return self.result(DOWN, f'AniDB has banned this client for now (it said: {error}); '
                               'requests pause until the cooldown ends', now_ms=now_ms)
        return self.result(DOWN, f'AniDB answered with an error: {error}', now_ms=now_ms)


class AnimeListsAdapter(_ProviderAdapter):
    name = 'anime_lists'
    label = 'Anime-Lists'
    providers = ('anime_lists',)
    cache_providers = ('anime_lists',)


class TvdbAdapter(_ProviderAdapter):
    name = 'tvdb'
    label = 'TVDB'
    required = True
    providers = ('tvdb',)
    missing = 'set the TVDB API key in Settings'

    def probe(self, configuration, secrets, *, now_ms=0):
        _, problem = self.secret(secrets, 'tvdb_credentials', self.missing, now_ms=now_ms)
        return problem or super().probe(configuration, secrets, now_ms=now_ms)

    def startup_probe(self, configuration, secrets, *, now_ms=0, previous=None):
        """Log in once. TVDB has no ban risk, and the login proves the API key."""
        from src.nextgen.providers.errors import (
            ProviderAuthenticationFailed, ProviderConfigurationError, ProviderDeferred,
            ProviderInvalidResponse, ProviderUnavailable,
        )
        credentials, problem = self.secret(secrets, 'tvdb_credentials', self.missing, now_ms=now_ms)
        if problem:
            return problem
        try:
            self._client(configuration).tvdb_login(credentials)
        except ProviderAuthenticationFailed:
            return self.result(AUTH_FAILED, 'TVDB rejected the API key; check it in Settings', now_ms=now_ms)
        except ProviderConfigurationError:
            return self.result(NOT_CONFIGURED, 'the provider settings are not valid', now_ms=now_ms)
        except ProviderDeferred:
            return self.result(UNKNOWN, 'a request was sent moments ago; the next one will show the status',
                               now_ms=now_ms)
        except (ProviderUnavailable, ProviderInvalidResponse):
            return self.result(DOWN, 'the TVDB login did not get a usable answer', now_ms=now_ms)
        return self.result(UP, 'login succeeded', now_ms=now_ms)
