"""Shared Local/Remote client. One encoder and one slot per destination."""
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import urlsplit
import uuid

from src.anime.acquisition.media import cancelled
from src.transcode_agent import encode

CHUNK = 1024 * 1024
_SLOTS = threading.Condition()
_BUSY = set()
_PIPELINE_WAITERS = 0
_LOCAL_INITIALIZED = set()


class TranscodeWaiting(RuntimeError):
    reason_code = 'TRANSCODE_WAITING'


class TranscodeFailed(RuntimeError):
    reason_code = 'TRANSCODE_FAILED'


def agent_url(value):
    if type(value) is not str or not value or any(c.isspace() for c in value):
        raise ValueError('agent must be host:port or an HTTP(S) origin')
    value = value if '://' in value else 'http://' + value
    parts = urlsplit(value)
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ('', '/') or not parts.port):
        raise ValueError('agent must have a host and port without credentials or a subpath')
    return value.rstrip('/')


def target_bytes(duration, mb=500):
    return int(mb * 1_000_000 * duration / (24 * 60))


def video_kbps(info, mb=500):
    # Reserve 1% of the total budget for the container, after accounting for every audio track.
    budget = target_bytes(info['duration'], mb) * 8 / info['duration'] / 1000
    rate = int(budget * .99 - encode.audio_kbps(info['streams']))
    if rate <= 0: raise TranscodeFailed('Target size is too small for the audio tracks')
    return rate


def digest(path, flag):
    result = hashlib.sha256()
    with open(path, 'rb') as stream:
        while chunk := stream.read(CHUNK):
            cancelled(flag)
            result.update(chunk)
    return result.hexdigest()


@contextmanager
def pipeline_priority(batch=False):
    global _PIPELINE_WAITERS
    pending = not batch
    with _SLOTS:
        if pending: _PIPELINE_WAITERS += 1
    def acquired():
        nonlocal pending
        global _PIPELINE_WAITERS
        with _SLOTS:
            if pending:
                _PIPELINE_WAITERS -= 1
                pending = False
    try: yield acquired
    finally: acquired()


@contextmanager
def slot(name, batch=False):
    with _SLOTS:
        acquired = name not in _BUSY and (not batch or _PIPELINE_WAITERS == 0)
        if acquired: _BUSY.add(name)
    try: yield acquired
    finally:
        if acquired:
            with _SLOTS:
                _BUSY.remove(name)
                _SLOTS.notify_all()


class Remote:
    def __init__(self, address, token):
        self.address = agent_url(address)
        self.token = token

    def connection(self, timeout):
        url = urlsplit(self.address)
        cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        return cls(url.hostname, url.port, timeout=timeout)

    def request(self, method, path, *, timeout=2):
        conn = self.connection(timeout)
        try:
            conn.request(method, path, headers={'Authorization': 'Bearer '+self.token})
            response = conn.getresponse()
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise TranscodeFailed(f'Agent returned HTTP {response.status}')
            return json.loads(body)
        finally: conn.close()

    def health(self):
        return self.request('GET', '/health')

    def run(self, source, output, rate, audio, flag, report, started):
        # SHA-256 must be known before the headers are sent. Verify it again during upload.
        expected = digest(source, flag)
        conn = self.connection(120)
        job = None
        try:
            report('Sending to ' + self.address)
            conn.putrequest('POST', '/jobs')
            for key, value in {'Authorization': 'Bearer '+self.token, 'Content-Length': str(source.stat().st_size),
                               'X-Sha256': expected, 'X-Video-Kbps': str(rate), 'X-Max-Audio': str(audio)}.items():
                conn.putheader(key, value)
            conn.putheader('Expect', '100-continue')
            conn.endheaders()
            interim = conn.response_class(conn.sock, method='POST')
            try:
                _, status, _ = interim._read_status()
                http.client.parse_headers(interim.fp)
                if status in (409, 507): raise TranscodeWaiting('Agent busy or out of space')
                if status != 100: raise TranscodeFailed(f'Upload returned HTTP {status}')
            finally: interim.close()
            started()
            check = hashlib.sha256()
            with source.open('rb') as stream:
                while chunk := stream.read(CHUNK):
                    cancelled(flag)
                    check.update(chunk)
                    conn.send(chunk)
            if check.hexdigest() != expected: raise TranscodeFailed('Source changed during upload')
            response = conn.getresponse()
            body = response.read(65537)
            if response.status in (409, 507): raise TranscodeWaiting('Agent busy or out of space')
            if response.status != 201: raise TranscodeFailed(f'Upload returned HTTP {response.status}')
            job = json.loads(body)['job_id']
            last_progress, changed = None, time.monotonic()
            while True:
                cancelled(flag)
                status = self.request('GET', '/jobs/'+job)
                if status['state'] == 'failed': raise TranscodeFailed(status.get('error') or 'Agent encode failed')
                if status['state'] == 'done': break
                progress = status.get('progress', 0)
                report(f'Transcoding on {self.address} ({progress}%)')
                if progress != last_progress:
                    last_progress, changed = progress, time.monotonic()
                elif time.monotonic() - changed >= 60:
                    raise TranscodeFailed('No encoding progress for 60 seconds')
                if flag.wait(5): cancelled(flag)
            report('Receiving result')
            conn.close()
            conn = self.connection(120)
            conn.request('GET', '/jobs/'+job+'/result', headers={'Authorization': 'Bearer '+self.token})
            response = conn.getresponse()
            if response.status != 200: raise TranscodeFailed(f'Result returned HTTP {response.status}')
            expected = response.getheader('X-Sha256')
            check = hashlib.sha256()
            with output.open('wb') as stream:
                while chunk := response.read(CHUNK):
                    cancelled(flag)
                    check.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if check.hexdigest() != expected: raise TranscodeFailed('Result SHA-256 mismatch')
        finally:
            conn.close()
            if job is not None:
                try: self.request('DELETE', '/jobs/'+job, timeout=10)
                except (OSError, ValueError, http.client.HTTPException, TranscodeFailed): pass


def transcode(source, output, configuration, secrets, flag, *, info=None, batch=False,
              report=lambda text: None, started=lambda: None, attempts=0, remote_factory=Remote):
    """Write output atomically. Return False when skipped; waits never become review.

    started() durably spends an attempt before external work. The caller supplies
    a persisted count so agent outages and worker restarts cannot reset the limit.
    """
    initial_attempts = attempts
    source, output = Path(source), Path(output)
    mode = configuration.value('transcode.mode')
    if mode == 'off': return False
    info = info or encode.probe(source)
    if source.stat().st_size <= target_bytes(info['duration'], configuration.value('transcode.target_mb')):
        return False
    if attempts >= 3: raise TranscodeFailed('Transcode failed on all agents: attempt limit reached')
    rate = video_kbps(info, configuration.value('transcode.target_mb'))
    temporary = output.with_name(output.name+'.tmp')
    temporary.unlink(missing_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    candidates = ('local',) if mode == 'local' else configuration.value('transcode.agents')
    last_error = 'No agent available'
    with pipeline_priority(batch) as acquired_slot:
        for address in candidates:
            cancelled(flag)
            with slot(address, batch) as acquired:
                if not acquired: continue
                acquired_slot()
                remote = None
                folder = None
                if mode == 'local':
                    tmp = configuration.value('transcode.tmp')
                    with _SLOTS:
                        if tmp not in _LOCAL_INITIALIZED:
                            from src.transcode_agent.server import wipe
                            wipe(tmp)
                            _LOCAL_INITIALIZED.add(tmp)
                    folder = Path(tmp)/'transcode-agent'
                    if encode.free_bytes(folder) < source.stat().st_size * 1.5: continue
                else:
                    remote = remote_factory(address, secrets.transcode_token(address))
                    if not remote.token: continue
                    alive = False
                    for retry in range(configuration.value('transcode.retries')+1):
                        try:
                            health = remote.health()
                            alive = not health['busy'] and health['free_bytes'] >= source.stat().st_size * 1.5
                            break
                        except (OSError, ValueError, KeyError, http.client.HTTPException, TranscodeFailed):
                            if retry < configuration.value('transcode.retries') and flag.wait(configuration.value('transcode.retry_seconds')):
                                cancelled(flag)
                    if not alive: continue
                while attempts < 3:
                    def spend():
                        nonlocal attempts
                        started()
                        attempts += 1
                    try:
                        if mode == 'local':
                            spend()
                            work = folder/uuid.uuid4().hex
                            work.mkdir(mode=0o700)
                            try:
                                encoded = work/'result.mkv'
                                encode.transcode(source, encoded, video_kbps=rate,
                                    encoders=encode.parse_encoders(configuration.value('transcode.encoders')),
                                    vaapi_device=configuration.value('transcode.vaapi_device'), workdir=work,
                                    cancelled=flag, source_info=info,
                                    on_progress=lambda p: report(f'Transcoding locally ({int(p*100)}%)'))
                                shutil.copyfile(encoded, temporary)
                            finally: shutil.rmtree(work, ignore_errors=True)
                        else:
                            remote.run(source, temporary, rate, encode.audio_kbps(info['streams']), flag, report, spend)
                        encode.check(source_info=info, output=temporary)
                        cancelled(flag)
                        os.replace(temporary, output)
                        return True
                    except TranscodeWaiting:
                        break
                    except (OSError, ValueError, KeyError, http.client.HTTPException, encode.EncodeError, TranscodeFailed) as exc:
                        last_error = str(exc)[-400:]
                        temporary.unlink(missing_ok=True)
                        if remote is not None: break  # Fail over before retrying this destination.
                    finally:
                        temporary.unlink(missing_ok=True)
        if attempts >= 3: raise TranscodeFailed('Transcode failed on all agents: '+last_error)
        if attempts > initial_attempts:
            # Retry another pass through the configured agents, retaining the same budget.
            return transcode(source, output, configuration, secrets, flag, info=info, batch=batch,
                             report=report, started=started, attempts=attempts, remote_factory=remote_factory)
    report('Waiting for transcoder')
    raise TranscodeWaiting('Waiting for transcoder')
