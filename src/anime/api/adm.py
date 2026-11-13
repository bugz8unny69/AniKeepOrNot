"""AniKeepOrNot local administration: `adm COMMAND ...`.

The commands are the modules in src/anime/api/adm_commands; adding a module
there adds a command and its line in `adm --help`.
"""
import sys

from src.anime import APP_NAME


def usage(commands):
    shown = {name: module for name, module in commands.items() if not getattr(module, 'HIDDEN', False)}
    width = max(map(len, shown))
    lines = [f'usage: adm COMMAND ...', '', f'{APP_NAME} local administration.', '', 'commands:']
    lines += [f'  {name.ljust(width)}  {module.SUMMARY}' for name, module in shown.items()]
    lines += ['', 'Run adm COMMAND --help for the options and subcommands of a command.']
    return '\n'.join(lines)


def main(argv=None):
    from src.anime.api.adm_commands import commands
    argv = sys.argv[1:] if argv is None else argv
    found = commands()
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(usage(found))
        sys.exit(0 if argv else 2)
    command = found.get(argv[0])
    if command is None:
        print(usage(found), file=sys.stderr)
        print(f'\nadm: unknown command {argv[0]!r}', file=sys.stderr)
        sys.exit(2)
    sys.exit(command.main(argv[1:], prog=f'adm {argv[0]}'))


if __name__ == "__main__":
    main()
