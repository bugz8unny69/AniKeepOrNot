"""The `adm` commands. One module here is one command; `adm` lists this package.

Each module defines:
  NAME     the word typed after `adm` (for example 'transcode')
  SUMMARY  one line for `adm --help`
  main(argv, prog) -> int   runs the command; argv excludes NAME, prog is 'adm NAME'
and optionally:
  HIDDEN = True   works, but is left out of `adm --help` (old spellings)

Importing these modules must stay cheap: `adm --help` imports all of them.
Import the implementation inside main(), not at the top of the module.
"""
import importlib
import pkgutil


def commands():
    """{name: module} for every command module in this package, in name order."""
    found = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith('_'):
            continue
        module = importlib.import_module(f'{__name__}.{info.name}')
        found[module.NAME] = module
    return dict(sorted(found.items()))
