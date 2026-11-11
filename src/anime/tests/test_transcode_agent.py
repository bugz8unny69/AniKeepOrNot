"""Offline agent contracts and real CPU/VAAPI stream-preservation encodes."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from unittest.mock import patch

import pytest

from src.transcode_agent import encode
from src.transcode_agent.server import Agent, make_server


def test_order_fallback_and_validation(tmp_path):
    info = {'duration': 2, 'streams': [{'codec_type': 'video'}]}
    calls = []
    def run(command, duration, progress):
        calls.append(command)
        return (1, 'VRAM allocation failed') if 'hevc_vaapi' in command else (0, '')
    with patch.object(encode, 'probe', return_value=info):
        assert encode.transcode('in', tmp_path/'out', video_kbps=100,
                                encoders=encode.parse_encoders('cpu,gpu'), run=run)['encoder'] == 'cpu'
        assert 'hevc_vaapi' in calls[0]
        calls.clear()
        with pytest.raises(encode.EncodeError, match='VRAM'):
            encode.transcode('in', tmp_path/'out', video_kbps=100, encoders=('gpu',), run=run)
        assert len(calls) == 1
    with patch.object(encode, 'probe', return_value={'duration': 2, 'streams': []}):
        with pytest.raises(encode.EncodeError, match='audio streams'):
            encode.check(source_info={'duration': 2, 'streams': [{'codec_type': 'audio'}]}, output=tmp_path/'out')


def test_http_contract_and_scoped_wipe(tmp_path):
    sibling = tmp_path/'keep.txt'
    sibling.write_text('keep')
    owned = tmp_path/'transcode-agent'
    owned.mkdir()
    (owned/'stale').write_text('old')
    release = threading.Event()
    def transcode(source, output, **kwargs):
        release.wait(5)
        Path(output).write_bytes(b'result')
        return {'encoder': 'cpu'}
    agent = Agent(tmp=tmp_path, token='test', encoders='cpu', transcode=transcode)
    assert sibling.read_text() == 'keep' and not (owned/'stale').exists()
    server = make_server(agent, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, data=None, headers=None, auth=True):
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        values = {'Authorization': 'Bearer test'} if auth else {}
        values.update(headers or {})
        conn.request(method, path, data, values)
        response = conn.getresponse()
        code, body = response.status, response.read()
        conn.close()
        return code, json.loads(body) if body else None
    headers = {'X-Sha256': hashlib.sha256(b'video').hexdigest(), 'X-Video-Kbps': '100'}
    try:
        assert request('GET', '/health', auth=False)[0] == 401
        assert request('GET', '/jobs/'+'0'*32)[0] == 404
        assert request('POST', '/jobs', b'wrong', headers)[0] == 422
        assert not agent.jobs
        # No request body is sent: rejection must happen before trying to read it.
        with patch.object(agent, 'free_bytes', return_value=1):
            assert request('POST', '/jobs', None, {**headers, 'Content-Length': '100'})[0] == 507
        code, job = request('POST', '/jobs', b'video', headers)
        assert code == 201
        assert request('POST', '/jobs', b'video', headers)[0] == 409
        release.set()
        for _ in range(100):
            if agent.status(job['job_id'])['state'] != 'running': break
            time.sleep(.01)
        assert agent.status(job['job_id'])['state'] == 'done'
        assert request('DELETE', '/jobs/'+job['job_id'])[0] == 200
        assert not list(owned.iterdir())
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_expiration(tmp_path):
    agent = Agent(tmp=tmp_path, token='x', encoders='cpu', clock=lambda: 4000)
    job = agent.reserve()
    agent.jobs[job].update(state='failed', finished_at=0)
    assert agent.reap() == [job]
    assert agent.status(job) is None


def _usable_vaapi_device():
    """Skip optional hardware coverage when no accessible Main10 encoder works."""
    configured = os.environ.get('TRANSCODE_VAAPI_DEVICE')
    devices = [Path(configured)] if configured else sorted(Path('/dev/dri').glob('renderD*'))
    reasons = []
    for device in devices:
        if not os.access(device, os.R_OK | os.W_OK):
            reasons.append(f'{device}: not accessible')
            continue
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                   '-vaapi_device', str(device), '-f', 'lavfi',
                   '-i', 'color=size=320x192:rate=1:duration=1',
                   '-vf', 'format=p010,hwupload', '-c:v', 'hevc_vaapi',
                   '-profile:v', 'main10', '-b:v', '100k', '-frames:v', '1', '-f', 'null', '-']
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            reasons.append(f'{device}: {exc}')
            continue
        if result.returncode == 0:
            return str(device)
        reasons.append(f'{device}: {result.stderr.strip()[-300:]}')
    pytest.skip('no usable HEVC Main10 VAAPI GPU: ' + ('; '.join(reasons) or 'no render devices'))


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg/ffprobe unavailable')
@pytest.mark.parametrize('encoder', ['cpu', 'gpu'], ids=['cpu', 'vaapi'])
def test_real_encode_keeps_streams(tmp_path, encoder):
    available = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True).stdout
    required = 'libx265' if encoder == 'cpu' else 'hevc_vaapi'
    if required not in available or 'libopus' not in available: pytest.skip('required encoders unavailable')
    device = _usable_vaapi_device() if encoder == 'gpu' else encode.DEFAULT_VAAPI_DEVICE
    font = next(iter(Path('/usr/share/fonts').rglob('*.ttf')), None)
    if font is None: pytest.skip('no test font installed')
    sub = tmp_path/'test.ass'
    sub.write_text('[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello\n')
    source, output = tmp_path/'source.mkv', tmp_path/'result.mkv'
    command = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x192:rate=10:duration=2',
               '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=2',
               '-i', str(sub), '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:s',
               '-c:v', 'ffv1', '-c:a:0', 'flac', '-c:a:1', 'aac', '-c:s', 'ass',
               '-metadata:s:a:0', 'language=jpn', '-metadata:s:a:1', 'language=eng',
               '-disposition:a:0', 'default', '-disposition:s:0', 'forced',
               '-attach', str(font), '-metadata:s:t', 'mimetype=application/x-truetype-font', str(source)]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    encoded = encode.transcode(source, output, video_kbps=100, encoders=(encoder,),
                               vaapi_device=device, workdir=tmp_path)
    assert encoded['encoder'] == encoder
    result = encode.probe(output)
    video = next(s for s in result['streams'] if s['codec_type'] == 'video')
    assert video['codec_name'] == 'hevc'
    assert video['profile'] == 'Main 10'
    assert encode.counts(result['streams']) == {'video': 1, 'audio': 2, 'subtitle': 1, 'attachment': 1}
    assert [s['codec_name'] for s in result['streams'] if s['codec_type'] == 'audio'] == ['opus', 'aac']
    assert [s.get('tags', {}).get('language') for s in result['streams'] if s['codec_type'] == 'audio'] == ['jpn', 'eng']

    assert next(s for s in result['streams'] if s['codec_type']=='subtitle')['disposition']['forced'] == 1
    assert next(s for s in result['streams'] if s['codec_type']=='audio')['disposition']['default'] == 1
