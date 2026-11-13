"""adm run straight from a release uses that release's settings, and explains a mismatch."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]


def release_copy(tmp_path, config_dir=None):
    release = tmp_path / 'release'
    shutil.copytree(ROOT / 'src', release / 'src', ignore=shutil.ignore_patterns('tests', '__pycache__'))
    shutil.copytree(ROOT / 'bin', release / 'bin', ignore=shutil.ignore_patterns('__pycache__'))
    if config_dir is not None:
        (release / '.anime-discovery-release.json').write_text(json.dumps({'revision': 'a' * 40, 'config_dir': str(config_dir)}))
    return release


def run(release, *args, env=None):
    environ = {k: v for k, v in os.environ.items() if k != 'ANIME_DISCOVERY_CONFIG_DIR'}
    return subprocess.run([sys.executable, '-I', str(release / 'bin/adm'), *args], cwd=release, capture_output=True,
                          text=True, timeout=30, env={**environ, **(env or {})})


def folder_seen(release, env=None):
    code = 'from src.anime.configuration import cli_config_dir; print(cli_config_dir())'
    environ = {k: v for k, v in os.environ.items() if k != 'ANIME_DISCOVERY_CONFIG_DIR'}
    return subprocess.run([sys.executable, '-c', code], cwd=release, capture_output=True, text=True, timeout=30,
                          env={**environ, **(env or {})}).stdout.strip()


def test_a_release_uses_the_settings_folder_recorded_for_it(tmp_path):
    branch = tmp_path / 'etc/branches/develop'
    release = release_copy(tmp_path, branch)
    assert folder_seen(release) == str(branch)
    assert folder_seen(release, {'ANIME_DISCOVERY_CONFIG_DIR': '/elsewhere'}) == '/elsewhere'   # The launcher wins.


def test_without_a_record_the_native_default_is_kept(tmp_path):
    assert folder_seen(release_copy(tmp_path)) == '/etc/anime-discovery'


def test_settings_of_another_version_are_explained_without_a_traceback(tmp_path):
    other = tmp_path / 'etc'
    other.mkdir()
    (other / 'settings.json').write_text(json.dumps({'schema_version': 10, 'revision': 1, 'settings': {}}))
    result = run(release_copy(tmp_path, other), 'transcode', 'status')
    assert result.returncode == 1
    assert 'Traceback' not in result.stderr
    assert f'settings in {other} are version 10' in result.stderr and 'each branch keeps its own' in result.stderr
