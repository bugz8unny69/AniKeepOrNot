"""Entry point of adm-agent.pyz (copied into the archive as __main__.py).

The archive carries only the transcode agent, so only `agent ...` (or the older `server agent ...`) is
available; it runs the same handlers as `adm agent` (cli.main).
"""
import os
import sys

MINIMUM_PYTHON = (3, 11)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    name = os.path.basename(sys.argv[0]) or 'adm-agent.pyz'
    if sys.version_info < MINIMUM_PYTHON:
        print(f'{name} needs Python {".".join(map(str, MINIMUM_PYTHON))} or newer; this is {sys.version.split()[0]}.',
              file=sys.stderr)
        return 2
    # `agent ...` is the spelling to use; `server agent ...` stays because installed
    # units run it, and rollback restores an older archive under the same unit.
    if argv[:1] == ['agent']:
        words, rest = 'agent', argv[1:]
    elif argv[:2] == ['server', 'agent']:
        words, rest = 'server agent', argv[2:]
    else:
        print(f'{name} contains only the AniKeepOrNot transcode agent.\n'
              f'usage: {name} agent [command] ...   (see: {name} agent --help)\n'
              'Other adm commands run on the AniKeepOrNot server itself.', file=sys.stderr)
        return 2
    from src.transcode_agent.cli import main as agent
    return agent(rest, prog=f'{name} {words}')


if __name__ == '__main__':
    raise SystemExit(main())
