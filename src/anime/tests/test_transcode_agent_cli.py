"""`adm agent` (and the older `adm server agent`): one command for ADM, python -m and the archive."""
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from src.transcode_agent import AGENT_VERSION, cli, version_text
from src.transcode_agent.server import Agent

ROOT = Path(__file__).resolve().parents[3]


def run(*args, env=None):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=30,
                          env={**os.environ, **(env or {})})


def test_adm_and_python_m_print_the_same_version():
    adm = run('bin/adm', 'agent', '--version')
    module = run('-m', 'src.transcode_agent', '--version')
    assert adm.returncode == module.returncode == 0
    assert adm.stdout == module.stdout == version_text() + '\n'
    assert f'version {AGENT_VERSION} ' in adm.stdout


def test_help_documents_configuration_dependencies_and_the_service():
    text = run('bin/adm', 'agent', '--help').stdout
    for expected in ('TRANSCODE_TOKEN', 'TRANSCODE_LISTEN', 'ffprobe', 'systemctl status transcode-agent'):
        assert expected in text


def test_unknown_server_command_fails_clearly_and_other_adm_commands_still_parse():
    other = run('bin/adm', 'server', 'web')
    assert other.returncode == 1 and 'adm agent' in other.stderr
    assert run('bin/adm', '--help').returncode == 0


def test_missing_token_refuses_to_start_without_touching_files(tmp_path):
    result = run('bin/adm', 'agent', env={'TRANSCODE_TOKEN': '', 'TRANSCODE_TMP': str(tmp_path)})
    assert result.returncode == 2 and 'TRANSCODE_TOKEN is required' in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_importing_the_agent_modules_starts_nothing(tmp_path):
    code = ('import threading; import src.transcode_agent.cli, src.transcode_agent.encode, src.transcode_agent.server;'
            'print(threading.active_count())')
    result = run('-c', code, env={'TRANSCODE_TMP': str(tmp_path)})
    assert result.stdout.strip() == '1'                         # No listener or reaper thread.
    assert list(tmp_path.iterdir()) == []                       # No workspace created or wiped.


def test_health_reports_version_and_build(tmp_path):
    health = Agent(tmp=tmp_path, token='t', encoders='cpu').health()
    assert health['agent_version'] == AGENT_VERSION
    assert isinstance(health['build_revision'], str) and health['build_revision']


def test_shutdown_cancels_a_running_encode(tmp_path):
    started, stopped = threading.Event(), threading.Event()

    def transcode(source, output, *, cancelled, **_):
        started.set()
        cancelled.wait(10)                                        # Stands in for ffmpeg, killed on cancel.
        stopped.set()
        raise RuntimeError('cancelled')
    agent = Agent(tmp=tmp_path, token='t', encoders='cpu', transcode=transcode)
    job = agent.reserve()
    agent.jobs[job]['source'].write_bytes(b'x' * 16)
    agent.start(job, 100)
    assert started.wait(5)
    agent.shutdown(timeout_s=5)
    assert stopped.is_set()
    assert not agent.jobs[job]['thread'].is_alive()


def test_settings_validation_is_shared():
    with pytest.raises(ValueError, match='host:port'):
        cli.settings({'TRANSCODE_LISTEN': 'nope', 'TRANSCODE_TOKEN': 't'})
    assert cli.settings({'TRANSCODE_TOKEN': 't'})['port'] == 8787


def test_adm_agent_and_adm_server_agent_are_the_same_command(tmp_path):
    short, long = run('bin/adm', 'agent', '--version'), run('bin/adm', 'server', 'agent', '--version')
    assert short.returncode == long.returncode == 0 and short.stdout == long.stdout
    for command in ('run', 'install', 'pack', 'upgrade', 'rollback', 'status', 'token'):
        assert run('bin/adm', 'agent', command, '--help').returncode == 0, command
    refused = run('bin/adm', 'agent', 'run', env={'TRANSCODE_TOKEN': '', 'TRANSCODE_TMP': str(tmp_path)})
    assert refused.returncode == 2 and 'TRANSCODE_TOKEN is required' in refused.stderr


def test_adm_agent_run_serves_health(tmp_path):
    import json, socket, time, urllib.request
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    agent = subprocess.Popen([sys.executable, 'bin/adm', 'agent', 'run'], cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env={**os.environ, 'TRANSCODE_TOKEN': 't', 'TRANSCODE_TMP': str(tmp_path),
                                                          'TRANSCODE_LISTEN': f'127.0.0.1:{port}', 'TRANSCODE_ENCODERS': 'cpu'})
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{port}/health', headers={'Authorization': 'Bearer t'})
        for _ in range(100):
            try:
                with urllib.request.urlopen(request, timeout=1) as response:
                    health = json.loads(response.read())
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail('adm agent run did not answer')
        assert health['agent_version'] == AGENT_VERSION
    finally:
        agent.terminate()
        assert agent.wait(10) == 0
