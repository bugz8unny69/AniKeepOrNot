"""adm-agent.pyz: an allowlisted zipapp that runs the same agent commands outside the repository."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile

import pytest

from src.transcode_agent import AGENT_VERSION, version_text
from src.transcode_agent.pack import ARCHIVE_FILES, PackError, pack

ROOT = Path(__file__).resolve().parents[3]
CLEAN_ENV = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}


@pytest.fixture(scope='module')
def archive(tmp_path_factory):
    folder = tmp_path_factory.mktemp('built')
    built = pack(folder / 'adm-agent.pyz')
    return Path(built['path']), built


def outside(archive, *args, env=None, cwd=None, **kwargs):
    """Run the archive like an agent machine would: isolated Python, no repository on any path."""
    return subprocess.run([sys.executable, '-I', str(archive), *args], cwd=cwd or archive.parent,
                          env={**CLEAN_ENV, **(env or {})}, capture_output=True, text=True, timeout=30, **kwargs)


def test_inventory_is_exactly_the_allowlist(archive):
    path, built = archive
    names = {name for name in zipfile.ZipFile(path).namelist() if not name.endswith('/')}
    assert names == set(ARCHIVE_FILES) | {'src/transcode_agent/build.json'}
    for forbidden in ('anime', 'tests', '.git', 'secrets', 'settings', '.sqlite', '.env\n'):
        assert not any(forbidden in name for name in names - {'src/transcode_agent/templates/transcode-agent.env.example'})


def test_decoy_secrets_and_data_in_the_source_tree_are_never_packed(tmp_path):
    root = tmp_path / 'checkout'
    for source in filter(None, ARCHIVE_FILES.values()):
        (root / source).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, root / source)
    decoys = ['src/transcode_agent/secrets.json', 'src/transcode_agent/agent.env', 'deploy/transcode-agent/real.env',
              'src/transcode_agent/data.sqlite', 'src/transcode_agent/__pycache__/x.pyc', '.git/config']
    for decoy in decoys:
        (root / decoy).parent.mkdir(parents=True, exist_ok=True)
        (root / decoy).write_text('TRANSCODE_TOKEN=do-not-pack')
    built = pack(tmp_path / 'out.pyz', root=root)
    with zipfile.ZipFile(built['path']) as zipped:
        assert all(b'do-not-pack' not in zipped.read(name) for name in zipped.namelist() if not name.endswith('/'))
        assert not any(name.endswith(('secrets.json', 'agent.env', 'real.env', '.sqlite', '.pyc', 'config'))
                       for name in zipped.namelist())


def test_build_record_matches_contents_and_digest(archive):
    path, built = archive
    with zipfile.ZipFile(path) as zipped:
        record = json.loads(zipped.read('src/transcode_agent/build.json'))
        for name, digest in record['files'].items():
            assert hashlib.sha256(zipped.read(name)).hexdigest() == digest
    assert record['agent_version'] == AGENT_VERSION and record['revision']
    assert Path(str(path) + '.sha256').read_text() == f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
    assert path.read_bytes().startswith(b'#!/usr/bin/env python3\n')
    assert os.access(path, os.X_OK)


def test_archive_and_adm_agree_on_version_and_commands(archive, tmp_path):
    path, built = archive
    version = outside(path, 'agent', '--version', cwd=tmp_path)
    assert version.returncode == 0
    assert f'version {AGENT_VERSION} ' in version.stdout
    adm = subprocess.run([sys.executable, 'bin/adm', 'agent', '--help'], cwd=ROOT, capture_output=True, text=True)
    packed = outside(path, 'agent', '--help')
    assert 'TRANSCODE_TOKEN' in packed.stdout and 'run' in packed.stdout
    commands = lambda text: text.split('options:')[0]
    assert 'pack' in commands(adm.stdout)                 # Building needs an installation...
    assert 'pack' not in commands(packed.stdout)          # ...so the archive does not offer it.


def test_unsupported_commands_fail_clearly(archive):
    path, _ = archive
    for args in (('set-password',), ('report',), ()):
        result = outside(path, *args)
        assert result.returncode == 2 and 'contains only the AniKeepOrNot transcode agent' in result.stderr
    assert 'invalid choice' in outside(path, 'agent', 'pack').stderr


def test_packing_from_inside_an_archive_is_refused(tmp_path):
    with pytest.raises(PackError, match='cannot repack itself'):
        pack(tmp_path / 'x.pyz', root=tmp_path)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_archive_runs_the_http_agent_with_its_build_revision(archive, tmp_path):
    path, built = archive
    port = free_port()
    agent = subprocess.Popen([sys.executable, '-I', str(path), 'server', 'agent'], cwd=tmp_path,
                             env={**CLEAN_ENV, 'TRANSCODE_LISTEN': f'127.0.0.1:{port}', 'TRANSCODE_TOKEN': 'pack-test',
                                  'TRANSCODE_ENCODERS': 'cpu', 'TRANSCODE_TMP': str(tmp_path)},
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{port}/health', headers={'Authorization': 'Bearer pack-test'})
        for _ in range(50):
            try:
                health = json.loads(urllib.request.urlopen(request, timeout=2).read())
                break
            except OSError:
                time.sleep(.1)
        else:
            pytest.fail('archive agent did not answer')
        assert health['agent_version'] == AGENT_VERSION
        assert health['build_revision'] == built['revision']
    finally:
        agent.terminate()
        assert agent.wait(10) == 0


def test_the_archive_installer_reads_its_templates_from_the_archive(archive, tmp_path):
    path, _ = archive
    code = ('import sys; sys.path.insert(0, sys.argv[1]); from src.transcode_agent import install;'
            'print(install.running_archive()); print(install.template("transcode-agent.service"))')
    result = subprocess.run([sys.executable, '-I', '-c', code, str(path)], cwd=tmp_path, env=CLEAN_ENV,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == str(path)
    assert 'ExecStart=' in result.stdout and 'server agent' in result.stdout


def test_the_archive_accepts_agent_and_the_older_server_agent(archive, tmp_path):
    path, _ = archive
    short, long = outside(path, 'agent', '--version', cwd=tmp_path), outside(path, 'server', 'agent', '--version', cwd=tmp_path)
    assert short.returncode == long.returncode == 0 and short.stdout == long.stdout
    assert 'agent run' in outside(path, 'agent', 'run', '--help').stdout
    assert 'server agent install' in outside(path, 'server', 'agent', 'install', '--help').stdout
