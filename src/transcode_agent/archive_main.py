"""Entry point of adm-agent.pyz (copied into the archive as __main__.py).

The archive carries only the transcode agent, so only `server agent ...` is
available; it runs the same handlers as `adm server agent` (cli.main).
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
    if argv[:2] != ['server', 'agent']:
        print(f'{name} contains only the AniKeepOrNot transcode agent.\n'
              f'usage: {name} server agent [command] ...   (see: {name} server agent --help)\n'
              'Other adm commands run on the AniKeepOrNot server itself.', file=sys.stderr)
        return 2
    from src.transcode_agent.cli import main as agent
    return agent(argv[2:], prog=f'{name} server agent')


if __name__ == '__main__':
    raise SystemExit(main())
