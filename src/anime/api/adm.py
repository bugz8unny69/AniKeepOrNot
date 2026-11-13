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
    try:
        code = command.main(argv[1:], prog=f'adm {argv[0]}')
    except Exception as exc:
        from src.anime.configuration_schema import ConfigurationMigrationRequired
        if not isinstance(exc, ConfigurationMigrationRequired):
            raise
        print(settings_mismatch(argv[1:]), file=sys.stderr)
        sys.exit(1)
    sys.exit(code)


def settings_mismatch(argv):
    """Why the settings could not be read, instead of a traceback."""
    import json
    from pathlib import Path
    from src.anime.configuration import cli_config_dir
    from src.anime.configuration_schema import CONFIGURATION_VERSION
    folder = next((argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == '--config-dir'),
                  next((arg.split('=', 1)[1] for arg in argv if arg.startswith('--config-dir=')), cli_config_dir()))
    try:
        found = json.loads((Path(folder) / 'settings.json').read_text()).get('schema_version', 'unknown')
    except (OSError, ValueError, AttributeError):
        found = 'unknown'
    release = Path(__file__).resolve().parents[3]
    return (f'adm: the settings in {folder} are version {found}; this release ({release}) uses version '
            f'{CONFIGURATION_VERSION}.\n'
            '  They probably belong to another branch or release: each branch keeps its own settings folder.\n'
            '  Use the installed launcher (adm, in /usr/local/bin), which selects the installed branch, or pass\n'
            '  --config-dir for the right folder. If you are inside /opt/anime-discovery/current, cd there again:\n'
            '  after an upgrade an open shell stays in the previous release.')


if __name__ == "__main__":
    main()
