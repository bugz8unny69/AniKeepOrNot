"""The identity process: identifies waiting releases for the API (see identity_bridge.py).

Started by the API with `python -m src.anime.identity_process --config-dir DIR`.
It takes releases from identity_requests one at a time, in order, and runs the
same verified AniDB-first resolver the identification job used, with the same
shared provider pacing. For each release it writes one JSON line on stdout:

  {"request_id": N, "result": "identified", "series": {...}}
  {"request_id": N, "result": "rejected", "reason_code": "..."}

It never creates jobs or deliveries; the API does that with the result. A
release that must wait (AniDB pacing, a provider down, credentials not set
up) stays queued with its next time. The process exits when nothing is due
within a minute; the API starts it again when something is.
"""
from dataclasses import dataclass, field
import argparse
import asyncio
from functools import partial
import json
import logging
import sys
import threading
import time

from src.anime.identity_bridge import REPORT_GRACE_MS, now_ms, trace

IDLE_EXIT_MS = 60_000
MAX_SERVICE_TRIES = 5
RETRY_MS = 60_000
MAX_RETRY_MS = 3_600_000
SETUP_WAIT_MS = 300_000


def _next(database, now):
    """The earliest waiting release, and how long until it is due (0 when due now)."""
    from src.anime.storage.connection import database as connection
    with connection(database) as db:
        row = db.execute('SELECT request_id,raw_body,process_anyway,next_try_at_ms,tries FROM identity_requests '
                         'WHERE outcome IS NULL ORDER BY next_try_at_ms,request_id LIMIT 1').fetchone()
    if row is None:
        return None, None
    return row, max(0, row[3] - now)


def _schedule(database, request_id, at_ms, reason_code=None, *, spend=False):
    from src.anime.storage.connection import database as connection
    from src.anime.storage.sqlite import transaction
    with connection(database) as db, transaction(db):
        db.execute('UPDATE identity_requests SET next_try_at_ms=?,last_reason_code=?,tries=tries+? '
                   'WHERE request_id=? AND outcome IS NULL', (at_ms, reason_code, 1 if spend else 0, request_id))


async def identify(resolve, configuration, row, logger):
    """('report', line) for a finished release, or ('wait', at_ms, reason, spend) to try it later."""
    from src.anime.matching import MatchingTrace
    from src.anime.providers.errors import (ProviderAuthenticationFailed, ProviderConfigurationError,
                                            ProviderDeferred, ProviderInvalidResponse, ProviderUnavailable)
    from src.anime.schema.anime_series import AnimeSeries
    from src.anime.storage.sqlite import IdentityConflict, IdentityNotReady
    request_id, raw, tries = row[0], bytes(row[1]), row[4]
    request = IdentityRequest(request_id, 'autobrr', raw)
    now = now_ms()

    def rejected(code):
        return 'report', {'request_id': request_id, 'result': 'rejected', 'reason_code': code}
    try:
        candidate = await resolve(request, configuration, threading.Event(), matching=MatchingTrace())
    except ProviderDeferred as exc:
        return 'wait', now + exc.delay_ms, 'JOB_WAITING', False
    except (ProviderUnavailable, ProviderInvalidResponse) as exc:
        if tries + 1 >= MAX_SERVICE_TRIES:
            return rejected(exc.reason_code)
        return 'wait', now + min(MAX_RETRY_MS, RETRY_MS * 2 ** tries), exc.reason_code, True
    except (ProviderAuthenticationFailed, ProviderConfigurationError):
        # Settings, not the release: never refuse releases for it. Keep them until it is fixed.
        logger.error('AniDB/TVDB are not set up (missing or refused credentials): check Settings. '
                     'Releases wait until it works.')
        return 'wait', now + SETUP_WAIT_MS, 'SERVICE_SETUP_INVALID', False
    except (IdentityNotReady, IdentityConflict) as exc:
        if _movie_request(request) or exc.reason_code == 'RELEASE_MOVIE_SKIPPED':
            return rejected('RELEASE_MOVIE_SKIPPED')
        return rejected(exc.reason_code)
    try:
        series = AnimeSeries.from_dict(candidate).to_dict()
    except (ValueError, TypeError, RecursionError, OverflowError):
        return rejected('IDENTITY_INVALID')
    return 'report', {'request_id': request_id, 'result': 'identified', 'series': series}


async def run(config_directory, out=sys.stdout):
    from src.anime.configuration import ConfigurationStore
    from src.anime.configuration_schema import for_operation
    from src.anime.diagnostics import configure_logging
    from src.anime.maintenance import MaintenanceGate, MaintenancePending
    from src.anime.providers.configuration import options_from_configuration, series_credentials
    from src.anime.providers.identity import ClientPool, make_resolver
    from src.anime.secrets import SecretStore
    store, secrets = ConfigurationStore(config_directory), SecretStore(config_directory)
    startup = store.read()
    logger = configure_logging(startup.logging_settings(), 'identity', logger=logging.Logger('anime.identity'))
    database = startup.value('storage.database_path')
    resolve = make_resolver(options_from_configuration, partial(series_credentials, secrets),
                            client_pool=ClientPool())
    gate = MaintenanceGate(database)
    while True:
        row, wait = await asyncio.to_thread(_next, database, now_ms())
        if row is None or wait > IDLE_EXIT_MS:
            return 0
        if wait > 0:
            await asyncio.sleep(wait / 1000)
            continue
        configuration = for_operation(store.read(), startup, component='identity')
        try:
            with gate.operation():
                outcome = await identify(resolve, configuration, row, logger)
        except MaintenancePending:
            return 0          # Maintenance asked for no new work; the API starts this again later.
        except Exception as exc:  # Boundary: a program bug; full trace, the release is refused and kept.
            logger.error('Identifying release request %s failed with a program error.\n%s', row[0], trace(exc))
            outcome = 'report', {'request_id': row[0], 'result': 'rejected', 'reason_code': 'JOB_HANDLER_FAILED'}
        if outcome[0] == 'wait':
            _, at_ms, reason, spend = outcome
            await asyncio.to_thread(_schedule, database, row[0], at_ms, reason, spend=spend)
            continue
        # Not tried again while the API applies it; tried again only if the API never did.
        await asyncio.to_thread(_schedule, database, row[0], now_ms() + REPORT_GRACE_MS, 'REPORTED')
        out.write(json.dumps(outcome[1], sort_keys=True) + '\n')
        out.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='identity_process')
    parser.add_argument('--config-dir', required=True)
    arguments = parser.parse_args(argv)
    return asyncio.run(run(arguments.config_dir))


if __name__ == '__main__':
    sys.exit(main())


@dataclass(frozen=True)
class IdentityRequest:
    """What the resolver reads of a queued release (it was the IDENTIFY_ANIME job's input)."""
    event_id: int
    source: str
    raw_body: bytes = field(repr=False)


def _movie_request(request):
    """A release whose identity was not found, and whose title says it is a movie."""
    import json
    from src.anime.batch.recognition import movie_title
    try:
        title = json.loads(getattr(request, "raw_body", b"") or b"{}").get("release_name")
    except (ValueError, TypeError, AttributeError):
        return False
    return movie_title(title)
