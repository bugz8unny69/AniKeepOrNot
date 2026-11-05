"""Service health contract: statuses, probe results and the adapter base class.

Every external service Anime Discovery depends on (qBittorrent, Prowlarr,
Sonarr, Jellyfin, Shoko, AniDB, TVDB, Anime-Lists) is described by one
adapter. Adding a service means writing one adapter and registering it; see
README.md in this package.
"""
from dataclasses import dataclass
import re

UP = 'UP'                          # Reachable and accepted our credentials.
DOWN = 'DOWN'                      # Could not be reached or did not answer properly.
NOT_CONFIGURED = 'NOT_CONFIGURED'  # Required address or credentials are missing or invalid.
AUTH_FAILED = 'AUTH_FAILED'        # Reachable, but rejected our credentials.
UNSUPPORTED = 'UNSUPPORTED'        # Reachable, but the version is not supported.
DISABLED = 'DISABLED'              # Optional service that is intentionally not set up.
UNKNOWN = 'UNKNOWN'                # Not checked yet, or its health record could not be read.

STATUSES = (UP, DOWN, NOT_CONFIGURED, AUTH_FAILED, UNSUPPORTED, DISABLED, UNKNOWN)
# Not a status: classify() returns this for errors that are expected waits,
# such as a planned AniDB pacing delay, so they never mark a service down.
NOT_A_FAILURE = 'NOT_A_FAILURE'
# Statuses that do not block work. UNKNOWN does not block: a real request
# then decides, and a failure marks the service down at once.
USABLE = frozenset({UP, DISABLED, UNKNOWN})

_NAME = re.compile(r'[a-z][a-z0-9_]{0,31}')
_SUMMARY = {
    UP: '{label} is available.',
    DOWN: '{label} is unavailable.',
    NOT_CONFIGURED: '{label} is not set up.',
    AUTH_FAILED: '{label} rejected the login or API key.',
    UNSUPPORTED: 'This {label} version is not supported.',
    DISABLED: '{label} is turned off (optional).',
    UNKNOWN: 'The {label} status is unknown.',
}


def valid_name(name):
    return type(name) is str and _NAME.fullmatch(name) is not None


@dataclass(frozen=True)
class ProbeResult:
    """One observation of a service. detail is plain text for people."""
    service: str
    status: str
    detail: str = ''
    checked_at_ms: int = 0

    def __post_init__(self):
        if not valid_name(self.service) or self.status not in STATUSES:
            raise ValueError('invalid probe result')
        if type(self.detail) is not str or len(self.detail) > 500:
            raise ValueError('invalid probe detail')
        if type(self.checked_at_ms) is not int or self.checked_at_ms < 0:
            raise ValueError('invalid probe time')

    @property
    def usable(self):
        return self.status in USABLE

    def message(self, label):
        """Descriptive status line naming the service and, if known, why."""
        summary = _SUMMARY[self.status].format(label=label)
        return f'{summary[:-1]}: {self.detail}' if self.detail else summary


class ServiceAdapter:
    """Base class for one external service.

    Subclasses set name/label and implement probe(). classify() maps the
    service client's own exceptions to a status so a failed real request
    can mark the service down without another network call.
    """
    name = ''
    label = ''
    # Optional services report DISABLED instead of NOT_CONFIGURED when blank.
    optional = False
    # False when probe() never contacts the service and only reads local
    # records written by real requests, such as the provider cache and pacing
    # table. Required for AniDB, which bans clients that send too many requests.
    network_probe = True

    def probe(self, configuration, secrets, *, now_ms=0):
        raise NotImplementedError

    def classify(self, exc):
        """Status for an exception raised by this service's client.

        Return NOT_A_FAILURE for expected waits, or None when the error is
        not recognised (treated as DOWN by ServiceHealth.mark_failed).
        """
        return None

    def result(self, status, detail='', *, now_ms=0):
        return ProbeResult(self.name, status, detail, now_ms)

    def secret(self, secrets, getter, missing, *, now_ms=0):
        """Read one credential. Returns (value, None) or (None, ProbeResult).

        An unreadable secrets.json is UNKNOWN, not "not set up": the key may
        well be configured, and saying "set the API key" would mislead.
        """
        from src.nextgen.secrets import SecretStorageError
        try:
            return getattr(secrets, getter)(), None
        except SecretStorageError:
            return None, self.result(UNKNOWN, 'saved credentials (secrets.json) could not be read',
                                     now_ms=now_ms)
        except (ValueError, RuntimeError):
            return None, self.result(NOT_CONFIGURED, missing, now_ms=now_ms)
