"""Which settings folder the command-line tools use by default.

Each branch keeps its own settings folder (the installer's per-branch state):
/etc/anime-discovery for the first branch, /etc/anime-discovery/branches/<branch>
for the others. The tools (adm report, adm recover, adm services check, ...)
must default to the folder of the branch that is RUNNING, not the first one.

In order:
1. ANIME_DISCOVERY_CONFIG_DIR, if set.
2. The --config-dir of the installed worker unit: the folder the services
   actually use. The unit file is readable by every account.
3. /etc/anime-discovery.
An explicit --config-dir always wins over this default.
"""
import os
import re
import shlex
from pathlib import Path

FALLBACK = '/etc/anime-discovery'
UNITS = (Path('/etc/systemd/system/anime-discovery-worker.service'),
         Path('/etc/systemd/system/anime-discovery-api.service'))
ENVIRONMENT = 'ANIME_DISCOVERY_CONFIG_DIR'


def from_unit(path):
    """The --config-dir in a unit's ExecStart, or None."""
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        if not line.startswith('ExecStart='):
            continue
        try:
            words = shlex.split(line[len('ExecStart='):].replace('%%', '%'))
        except ValueError:
            return None
        for i, word in enumerate(words):
            if word == '--config-dir' and i + 1 < len(words):
                value = words[i + 1]
            elif word.startswith('--config-dir='):
                value = word.split('=', 1)[1]
            else:
                continue
            return value if value.startswith('/') else None
    return None


def default_config_dir(*, environ=None, units=UNITS):
    environ = os.environ if environ is None else environ
    chosen = environ.get(ENVIRONMENT)
    if chosen:
        return chosen
    for unit in units:
        found = from_unit(unit)
        if found:
            return found
    return FALLBACK
