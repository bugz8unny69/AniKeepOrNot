"""Work item 5: the packed agent, run outside the repository, against the real worker client.

Real ffmpeg encodes (skipped without libx265/libopus). The systemd checks at the
end use a transient `systemd-run --user` unit that is removed afterwards; they run
only with AGENT_SYSTEMD_TESTS=1 and a reachable user manager.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile

import pytest

from src.anime.configuration_schema import ConfigurationSnapshot
from src.anime.services import transcode as client
from src.anime.services.adapters.transcode import TranscodeAdapter
from src.transcode_agent import AGENT_VERSION, encode
from src.transcode_agent.pack import pack

TOKEN = 'acceptance-token-0123456789'
CLEAN_ENV = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}


def encoders_available():
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        return False
    listed = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True).stdout
    return 'libx265' in listed and 'libopus' in listed


needs_ffmpeg = pytest.mark.skipif(not encoders_available(), reason='ffmpeg with libx265 and libopus unavailable')


@pytest.fixture(scope='module')
def archives(tmp_path_factory):
    folder = tmp_path_factory.mktemp('archives')
    current = Path(pack(folder / 'adm-agent.pyz')['path'])
    newer = folder / 'newer.pyz'
    with zipfile.ZipFile(current) as source, zipfile.ZipFile(newer, 'w', zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == 'src/transcode_agent/__init__.py':
                data = data.replace(f'AGENT_VERSION = {AGENT_VERSION}'.encode(), f'AGENT_VERSION = {AGENT_VERSION + 1}'.encode())
            target.writestr(item, data)
    return {'current': current, 'newer': newer}


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def health(port):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/health', headers={'Authorization': 'Bearer ' + TOKEN})
    return json.loads(urllib.request.urlopen(request, timeout=2).read())


def wait_for(port, seconds=10):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return health(port)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(.1)


class ArchiveAgent:
    """The archive started like the service does, from an unrelated folder with an isolated Python."""
    def __init__(self, archive, folder):
        self.port = free_port()
        self.folder = folder
        self.process = subprocess.Popen(
            [sys.executable, '-I', str(archive), 'server', 'agent'], cwd=folder, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            env={**CLEAN_ENV, 'TRANSCODE_LISTEN': f'127.0.0.1:{self.port}', 'TRANSCODE_TOKEN': TOKEN,
                 'TRANSCODE_ENCODERS': 'cpu', 'TRANSCODE_TMP': str(folder)})
        self.health = wait_for(self.port)

    @property
    def address(self):
        return f'http://127.0.0.1:{self.port}'

    def stop(self):
        self.process.terminate()
        return self.process.wait(30)


@pytest.fixture(autouse=True)
def fixed_size():
    """Size targets are not under test here: always transcode, at a fixed small bitrate."""
    from unittest.mock import patch
    with patch.object(client, 'target_bytes', return_value=1), patch.object(client, 'video_kbps', return_value=300):
        yield


@pytest.fixture
def agent(archives, tmp_path):
    running = ArchiveAgent(archives['current'], tmp_path)
    yield running
    running.stop()


class Tokens:
    def transcode_token(self, address):
        return TOKEN


def settings(*addresses):
    return ConfigurationSnapshot(1, {'transcode': {'mode': 'remote', 'target_mb': 1, 'retries': 0,
                                                   'agents': list(addresses)}})


def synthetic(folder, seconds=3, size='320x192'):
    source = folder / 'episode.mkv'
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=24:duration={seconds}',
                    '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-c:v', 'ffv1', '-c:a', 'flac',
                    str(source)], check=True, capture_output=True, timeout=60)
    return source


@needs_ffmpeg
def test_worker_client_encodes_through_the_archive_agent(agent, tmp_path):
    assert agent.health['agent_version'] == AGENT_VERSION
    source, output = synthetic(tmp_path), tmp_path / 'staged/episode.mkv'
    spent, steps = [], []
    done = client.transcode(source, output, settings(agent.address), Tokens(), threading.Event(),
                            started=lambda: spent.append(1), report=steps.append)
    assert done is True and spent == [1]
    result = encode.probe(output)                               # The shared validator already passed it.
    video = next(stream for stream in result['streams'] if stream['codec_type'] == 'video')
    assert (video['codec_name'], video['profile']) == ('hevc', 'Main 10')
    assert any(step.startswith('Transcoding on') for step in steps)
    assert not list((tmp_path / 'transcode-agent').iterdir())   # The agent deleted the job afterwards.


@needs_ffmpeg
def test_a_newer_agent_is_refused_without_uploading_and_the_job_waits(archives, tmp_path):
    newer = ArchiveAgent(archives['newer'], tmp_path)
    try:
        assert newer.health['agent_version'] == AGENT_VERSION + 1
        source = synthetic(tmp_path)
        spent = []
        with pytest.raises(client.TranscodeWaiting, match=f'version {AGENT_VERSION + 1}; this worker supports version'):
            client.transcode(source, tmp_path / 'out.mkv', settings(newer.address), Tokens(), threading.Event(),
                             started=lambda: spent.append(1))
        assert spent == []
        assert not list((tmp_path / 'transcode-agent').iterdir())   # Nothing was uploaded.
        probe = TranscodeAdapter().probe(settings(newer.address), Tokens())
        assert probe.status == 'UNSUPPORTED' and 'Update AniKeepOrNot' in probe.detail
    finally:
        newer.stop()


@needs_ffmpeg
def test_a_health_check_during_an_encode_does_not_disturb_it(agent, tmp_path):
    source, output = synthetic(tmp_path, seconds=60, size='1280x720'), tmp_path / 'long.mkv'
    outcome = {}

    def work():
        try:
            outcome['done'] = client.transcode(source, output, settings(agent.address), Tokens(), threading.Event())
        except Exception as exc:   # Reported by the assertion below.
            outcome['error'] = exc
    thread = threading.Thread(target=work)
    thread.start()
    deadline = time.monotonic() + 30
    while not health(agent.port)['busy'] and time.monotonic() < deadline:
        time.sleep(.1)
    probe = TranscodeAdapter().probe(settings(agent.address), Tokens())
    assert probe.status == 'UP' and 'up, busy' in probe.detail
    thread.join(300)
    assert outcome == {'done': True}


@needs_ffmpeg
def test_stopping_the_agent_mid_encode_leaves_no_ffmpeg(archives, tmp_path):
    running = ArchiveAgent(archives['current'], tmp_path)
    source = synthetic(tmp_path, seconds=60, size='1280x720')
    remote = client.Remote(running.address, TOKEN)
    thread = threading.Thread(target=lambda: _swallow(lambda: remote.run(
        source, tmp_path / 'never.mkv', 200, 128, threading.Event(), lambda _: None, lambda: None)))
    thread.start()
    assert _wait_for_ffmpeg(tmp_path)                               # An encode is running (after the upload)...
    assert running.stop() == 0
    assert not _ffmpeg_under(tmp_path)                              # ...and stopped with the agent.
    thread.join(30)


def _swallow(call):
    try:
        call()
    except Exception:
        pass


def _wait_for_ffmpeg(folder, seconds=30):
    deadline = time.monotonic() + seconds
    while not (found := _ffmpeg_under(folder)) and time.monotonic() < deadline:
        time.sleep(.1)
    return found


def _ffmpeg_under(folder):
    """ffmpeg processes working on files in folder."""
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if command and command[0].endswith(b'ffmpeg') and any(str(folder).encode() in part for part in command):
            found.append(int(entry.name))
    return found


# --- Real systemd (opt-in): a transient user unit, removed afterwards ------------------------

def _user_manager():
    return shutil.which('systemd-run') and subprocess.run(['systemctl', '--user', 'show-environment'],
                                                          capture_output=True).returncode == 0


systemd = pytest.mark.skipif(os.environ.get('AGENT_SYSTEMD_TESTS') != '1' or not _user_manager(),
                             reason='set AGENT_SYSTEMD_TESTS=1 with a systemd user manager to run')


def _unit_pid(unit):
    return int(subprocess.run(['systemctl', '--user', 'show', unit, '--property=MainPID', '--value'],
                              capture_output=True, text=True).stdout.strip() or 0)


@systemd
@needs_ffmpeg
def test_systemd_restarts_a_crashed_agent_and_stopping_it_kills_ffmpeg(archives, tmp_path):
    unit = f'transcode-agent-acceptance-{uuid.uuid4().hex[:8]}'
    port = free_port()
    subprocess.run(['systemd-run', '--user', '--collect', f'--unit={unit}', '-p', 'Restart=on-failure',
                    '-p', 'RestartSec=1', '-p', 'TimeoutStopSec=30',
                    f'--setenv=TRANSCODE_LISTEN=127.0.0.1:{port}', f'--setenv=TRANSCODE_TOKEN={TOKEN}',
                    '--setenv=TRANSCODE_ENCODERS=cpu', f'--setenv=TRANSCODE_TMP={tmp_path}',
                    shutil.which('python3'), '-I', str(archives['current']), 'server', 'agent'],
                   check=True, capture_output=True)
    try:
        assert wait_for(port)['agent_version'] == AGENT_VERSION
        first = _unit_pid(unit)
        os.kill(first, signal.SIGKILL)                              # A crash.
        time.sleep(1.5)
        assert wait_for(port, 15)['busy'] is False
        assert _unit_pid(unit) not in (0, first)                    # systemd started a new agent.
        source = synthetic(tmp_path, seconds=60, size='1280x720')
        remote = client.Remote(f'http://127.0.0.1:{port}', TOKEN)
        threading.Thread(target=lambda: _swallow(lambda: remote.run(
            source, tmp_path / 'never.mkv', 200, 128, threading.Event(), lambda _: None, lambda: None)), daemon=True).start()
        assert _wait_for_ffmpeg(tmp_path)
        subprocess.run(['systemctl', '--user', 'stop', unit], check=True)
        assert not _ffmpeg_under(tmp_path)
    finally:
        subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True)
        subprocess.run(['systemctl', '--user', 'reset-failed', unit], capture_output=True)
