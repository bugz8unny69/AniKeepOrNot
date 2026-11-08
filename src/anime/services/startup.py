"""Startup policy: the worker does not start while a required service is misconfigured.

Required services (adapter.required): qBittorrent, AniDB and TVDB. The worker
refuses to start when one of them is not set up, rejects the login, client
name or API key, or is an unsupported version. Waiting would never fix those.

A required service that is only unavailable does not stop startup: the jobs
that need it wait (SERVICE_UNAVAILABLE) and continue once it is back.
Optional services never stop startup: Prowlarr and Sonarr turn off automatic
search and Keep, Jellyfin and Shoko turn off their refresh and metadata jobs.
"""
from src.anime.services.base import AUTH_FAILED, NOT_CONFIGURED, UNSUPPORTED

REFUSING = frozenset({NOT_CONFIGURED, AUTH_FAILED, UNSUPPORTED})

# EX_CONFIG from sysexits.h. The worker unit lists it in
# RestartPreventExitStatus, so systemd does not restart the worker every few
# seconds (and send AniDB a request each time) until the settings are fixed.
EXIT_CODE = 78


class StartupRefused(RuntimeError):
    reason_code = 'SERVICE_SETUP_INVALID'

    def __init__(self, states):
        self.states = tuple(states)
        super().__init__('A required service is not set up correctly.')

    def lines(self):
        """One descriptive line per refused service, plus what to do."""
        return (*(state.message() for state in self.states),
                'Fix these in Settings (or run "adm services check" for details), then start the worker again.')


def refusals(health):
    """States of required services that must stop startup."""
    return tuple(state for state in health.snapshot()
                 if health.registry.get(state.service).required and state.status in REFUSING)
