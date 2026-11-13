"""adm lists its commands from src/anime/api/adm_commands; each one has its own help."""
import os
from pathlib import Path
import subprocess
import sys

from src.anime.api.adm_commands import commands

ROOT = Path(__file__).resolve().parents[3]


def adm(*args):
    return subprocess.run([sys.executable, 'bin/adm', *args], cwd=ROOT, capture_output=True, text=True, timeout=30,
                          env={**os.environ, 'TRANSCODE_TOKEN': ''})


def test_every_command_module_follows_the_contract():
    found = commands()
    assert {'agent', 'transcode', 'report', 'recover', 'services', 'set-password'} <= set(found)
    for name, module in found.items():
        assert module.NAME == name and module.SUMMARY.strip() and callable(module.main), name


def test_help_lists_every_visible_command_and_hides_old_spellings():
    result = adm('--help')
    assert result.returncode == 0
    for name, module in commands().items():
        listed = any(line.split()[:1] == [name] for line in result.stdout.splitlines())
        assert listed != getattr(module, 'HIDDEN', False), name


def test_each_visible_command_has_its_own_help_under_its_name():
    for name, module in commands().items():
        if getattr(module, 'HIDDEN', False):
            continue
        result = adm(name, '--help')
        assert result.returncode == 0, (name, result.stderr)
        assert f'usage: adm {name}' in result.stdout, name


def test_no_command_and_unknown_commands_show_the_list():
    empty, unknown = adm(), adm('nope')
    assert empty.returncode == unknown.returncode == 2
    assert 'transcode' in empty.stdout and "unknown command 'nope'" in unknown.stderr


def test_listing_commands_imports_no_implementation():
    code = ('import sys; from src.anime.api.adm_commands import commands; commands();'
            'print(sorted(m for m in ("src.anime.transcode_cli","src.anime.job_report","src.anime.job_recovery",'
            '"src.transcode_agent.cli") if m in sys.modules))')
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == '[]', result.stderr
