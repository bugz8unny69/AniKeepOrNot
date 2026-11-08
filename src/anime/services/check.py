"""`adm services check`: check every external service once and print a table.

AniDB, Anime-Lists and TVDB are read from the local cache and pacing records,
never contacted (the worker's own startup check is the one that contacts AniDB
and TVDB, see startup.py). Services marked "(required)" stop the worker at
startup when they are not set up or reject the login. Exit status: 0 when every service is usable, 1 when any
service is down, not set up, rejected the credentials or is unsupported,
2 when the settings themselves cannot be read.
"""
import argparse
import json
import sys

from src.anime.configuration import cli_config_dir
from src.anime.services.base import DISABLED, UNKNOWN, UP
from src.anime.services.health import ServiceHealth
from src.anime.services.registry import default_registry

_SHOW = {UP: 'OK', DISABLED: 'off', UNKNOWN: 'unknown'}


def check(configuration, secrets, *, registry=None, services=None, clock=None):
    """Probe the selected services. Returns a tuple of ServiceState."""
    registry = registry or default_registry()
    names = tuple(services) if services else registry.names()
    for name in names:
        registry.get(name)  # Unknown names fail before any probe runs.
    health = ServiceHealth(registry, clock=clock)
    for name in names:
        health.probe(name, configuration, secrets)
    return tuple(health.state(name) for name in names)


def _table(states, registry):
    rows = [('Service', 'Status', 'Details')]
    for state in states:
        source = '' if registry.get(state.service).network_probe else ' (from cache)'
        required = ' (required)' if registry.get(state.service).required else ''
        rows.append((state.label + required, _SHOW.get(state.status, state.status.replace('_', ' ').lower()),
                     state.message() + source))
    width = [max(len(row[i]) for row in rows) for i in range(2)]
    return '\n'.join(f'{a:<{width[0]}}  {b:<{width[1]}}  {c}' for a, b, c in rows)


def main(argv=None, *, out=print, registry=None):
    parser = argparse.ArgumentParser(prog='adm services',
                                     description='Check the external services Anime Discovery depends on.')
    parser.add_argument('--config-dir', default=cli_config_dir())
    sub = parser.add_subparsers(dest='action', required=True)
    run = sub.add_parser('check', help='Check each service once and print its status')
    run.add_argument('--service', action='append', metavar='NAME',
                     help='Check only this service (repeatable)')
    run.add_argument('--json', action='store_true', help='Print JSON instead of a table')
    args = parser.parse_args(argv)

    registry = registry or default_registry()
    try:
        from src.anime.configuration import ConfigurationStore
        from src.anime.secrets import SecretStore
        configuration = ConfigurationStore(args.config_dir).read()
        secrets = SecretStore(args.config_dir)
    except Exception as exc:
        print(f'Settings in {args.config_dir} could not be read: {exc}', file=sys.stderr)
        return 2
    try:
        states = check(configuration, secrets, registry=registry, services=args.service)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        out(json.dumps([{'service': s.service, 'label': s.label, 'status': s.status,
                         'message': s.message(), 'detail': s.detail,
                         'from_cache': not registry.get(s.service).network_probe,
                         'required': registry.get(s.service).required}
                        for s in states], indent=2))
    else:
        out(_table(states, registry))
    return 0 if all(s.usable for s in states) else 1


if __name__ == '__main__':
    sys.exit(main())
