"""HTTP side of the transcode agent: one job at a time, files in its own folder.

Standard library only; nothing here imports src.anime. See TRANSCODE_AGENT.md.

Routes (every request needs `Authorization: Bearer <TRANSCODE_TOKEN>`):
  GET    /health             {encoders, busy, free_bytes, agent_version, build_revision}
  POST   /jobs               body: the video. Headers X-Sha256, X-Video-Kbps.
                             507 when free space < 1.5 x Content-Length (before
                             reading the body), 409 while busy, 422 on a hash
                             mismatch (the upload is deleted). Returns {job_id}.
  GET    /jobs/{id}          {state: running|done|failed, progress, encoder, error}
  GET    /jobs/{id}/result   the output MKV, with X-Sha256 of it
  DELETE /jobs/{id}          deletes the job's files

The agent works only inside <TRANSCODE_TMP>/transcode-agent/, wipes only that
folder at startup, and deletes a finished job nobody collected after an hour.
"""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from src.transcode_agent import encode, build_info

SUBFOLDER = 'transcode-agent'
SPACE_FACTOR = 1.5
UNCOLLECTED_TTL_S = 3600
CHUNK = 1024 * 1024
_JOB = re.compile(r'/jobs/([a-f0-9]{32})(/result)?')
_HEX64 = re.compile(r'[a-f0-9]{64}')


def say(text):
    """One line in the agent's journal (journalctl -u transcode-agent)."""
    print(f'transcode-agent: {text}', flush=True)


def workspace(tmp):
    """The only folder the agent touches: <tmp>/transcode-agent."""
    return Path(tmp) / SUBFOLDER


def wipe(tmp):
    """Delete the agent's subfolder (and nothing else) and create it again."""
    folder = workspace(tmp)
    if folder.is_symlink():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, mode=0o700)
    return folder


class Agent:
    """Job state and the encode thread. The HTTP handler only talks to this."""

    def __init__(self, *, tmp, token, encoders, vaapi_device=encode.DEFAULT_VAAPI_DEVICE,
                 transcode=encode.transcode, clock=time.monotonic, ttl_s=UNCOLLECTED_TTL_S):
        if not token:
            raise ValueError('TRANSCODE_TOKEN is required')
        self.token = token
        self.encoders = encode.parse_encoders(','.join(encoders)) if not isinstance(encoders, str) else encode.parse_encoders(encoders)
        self.vaapi_device = vaapi_device
        self.transcode = transcode
        self.clock = clock
        self.ttl_s = ttl_s
        self.folder = wipe(tmp)
        self.lock = threading.Lock()
        self.jobs = {}

    def authorized(self, header):
        expected = 'Bearer ' + self.token
        return type(header) is str and hmac.compare_digest(header.encode(), expected.encode())

    def busy(self):
        with self.lock:
            return any(job['state'] == 'running' for job in self.jobs.values())

    def free_bytes(self):
        return encode.free_bytes(self.folder)

    def health(self):
        info = build_info()
        return {'encoders': list(self.encoders), 'busy': self.busy(), 'free_bytes': self.free_bytes(),
                'agent_version': info['agent_version'], 'build_revision': info['revision']}

    def shutdown(self, timeout_s=30):
        """Stop: cancel every encode (its ffmpeg is killed) and wait for the threads. No child outlives the agent."""
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            job['cancelled'].set()
        for job in jobs:
            if job['thread'] is not None:
                job['thread'].join(timeout_s)

    def enough_space(self, content_length):
        return self.free_bytes() >= content_length * SPACE_FACTOR

    def reserve(self):
        """Take the single slot for a new job, or return None while busy."""
        with self.lock:
            if any(job['state'] == 'running' for job in self.jobs.values()):
                return None
            job_id = uuid.uuid4().hex
            folder = self.folder / job_id
            folder.mkdir(mode=0o700)
            self.jobs[job_id] = {'state': 'running', 'progress': 0, 'encoder': None, 'error': None,
                                 'folder': folder, 'source': folder / 'source', 'output': folder / 'output.mkv',
                                 'sha256': None, 'finished_at': None, 'uploaded': False, 'cancelled': threading.Event(), 'thread': None}
            return job_id

    def abandon(self, job_id):
        """An upload that did not complete: free the slot and delete its files."""
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if job is not None:
            shutil.rmtree(job['folder'], ignore_errors=True)

    def start(self, job_id, video_kbps):
        with self.lock:
            job = self.jobs[job_id]
            job['uploaded'] = True
        thread = threading.Thread(target=self._run, args=(job_id, video_kbps), daemon=True,
                                  name='transcode-' + job_id[:8])
        job['thread'] = thread
        thread.start()
        return thread

    def _run(self, job_id, video_kbps):
        job = self.jobs[job_id]
        name, began = job_id[:8], time.monotonic()
        size = job['source'].stat().st_size if job['source'].exists() else 0
        say(f'job {name}: received {size / 1e6:.0f} MB, target video {video_kbps} kb/s')
        logged = [-10.0]

        def progress(fraction):
            # Hundredths of a percent: a slow CPU encode still moves within the worker's stall window.
            job['progress'] = round(fraction * 100, 2)
            if job['progress'] >= logged[0] + 10:
                logged[0] = job['progress'] // 10 * 10
                say(f'job {name}: {logged[0]:.0f}% ({job["encoder"]}, {time.monotonic() - began:.0f}s)')

        def encoder(value):
            job.update(encoder=value)
            logged[0] = -10.0
            say(f'job {name}: encoding with {value}')

        def failed(value, error):
            say(f'job {name}: {value} encode failed: {error}')
        try:
            options = dict(video_kbps=video_kbps, encoders=self.encoders, vaapi_device=self.vaapi_device,
                           workdir=job['folder'], on_progress=progress, cancelled=job['cancelled'], on_encoder=encoder)
            if self.transcode is encode.transcode:
                options['on_failed'] = failed          # Test doubles need not accept it.
            result = self.transcode(job['source'], job['output'], **options)
            digest = hashlib.sha256()
            with open(job['output'], 'rb') as stream:
                for chunk in iter(lambda: stream.read(CHUNK), b''):
                    digest.update(chunk)
            with self.lock:
                job.update(state='done', progress=100, encoder=result['encoder'],
                           sha256=digest.hexdigest(), finished_at=self.clock())
            say(f'job {name}: done with {result["encoder"]} in {time.monotonic() - began:.0f}s')
        except Exception as exc:  # The agent must answer; the error goes to the worker.
            with self.lock:
                job.update(state='failed', error=str(exc)[:500] or type(exc).__name__, finished_at=self.clock())
            reason = 'cancelled by the worker' if job['cancelled'].is_set() else job['error']
            say(f'job {name}: stopped after {time.monotonic() - began:.0f}s: {reason}')
        finally:
            try:
                os.unlink(job['source'])
            except OSError:
                pass

    def status(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {'state': job['state'], 'progress': job['progress'], 'encoder': job['encoder'],
                    'error': job['error']}

    def result(self, job_id):
        """(path, sha256) of a finished job, None when unknown, False when not done."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job['state'] != 'done':
                return False
            return job['output'], job['sha256']

    def delete(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job['cancelled'].set()
        if job is None:
            return False
        if job['state'] == 'running':
            say(f'job {job_id[:8]}: the worker cancelled it at {job["progress"]}% ({job["encoder"] or "not started"})')
        if job['thread'] is not None:
            job['thread'].join()
        with self.lock:
            self.jobs.pop(job_id, None)
        shutil.rmtree(job['folder'], ignore_errors=True)
        return True

    def reap(self):
        """Delete finished jobs nobody collected within ttl_s. Returns their ids."""
        now = self.clock()
        with self.lock:
            stale = [job_id for job_id, job in self.jobs.items()
                     if job['state'] != 'running' and job['finished_at'] is not None
                     and now - job['finished_at'] >= self.ttl_s]
        for job_id in stale:
            self.delete(job_id)
        return stale


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    agent = None   # Set by make_server().

    def setup(self):
        super().setup()
        self.connection.settimeout(120)

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        pass

    def _json(self, code, body, headers=()):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        for name, value in headers:
            self.send_header(name, value)
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)

    def _refuse(self, code, error):
        # Sent before the body was read: the connection cannot be reused.
        self.close_connection = True
        self._json(code, {'error': error})

    def _authorized(self):
        if self.agent.authorized(self.headers.get('Authorization')):
            return True
        self._refuse(401, 'unauthorized')
        return False

    def do_GET(self):
        if not self._authorized():
            return
        if self.path == '/health':
            return self._json(200, self.agent.health())
        match = _JOB.fullmatch(self.path)
        if match is None:
            return self._json(404, {'error': 'not found'})
        job_id, result = match.group(1), bool(match.group(2))
        if not result:
            status = self.agent.status(job_id)
            return self._json(404, {'error': 'unknown job'}) if status is None else self._json(200, status)
        found = self.agent.result(job_id)
        if found is None:
            return self._json(404, {'error': 'unknown job'})
        if found is False:
            return self._json(409, {'error': 'job is not done'})
        path, digest = found
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header('Content-Type', 'video/x-matroska')
        self.send_header('Content-Length', str(size))
        self.send_header('X-Sha256', digest)
        self.end_headers()
        with open(path, 'rb') as stream:
            shutil.copyfileobj(stream, self.wfile, CHUNK)

    def do_DELETE(self):
        if not self._authorized():
            return
        match = _JOB.fullmatch(self.path)
        if match is None or match.group(2):
            return self._json(404, {'error': 'not found'})
        if self.agent.delete(match.group(1)):
            return self._json(200, {'deleted': True})
        return self._json(404, {'error': 'unknown job'})

    def handle_expect_100(self):
        if not self._authorized(): return False
        try:
            length = int(self.headers.get('Content-Length', ''))
            if length <= 0: raise ValueError()
        except ValueError:
            self._refuse(400, 'Content-Length is required')
            return False
        if not self.agent.enough_space(length):
            self._refuse(507, 'not enough free space for this file')
            return False
        if self.agent.busy():
            self._refuse(409, 'busy')
            return False
        self.send_response_only(100)
        self.end_headers()
        return True

    def do_POST(self):
        if not self._authorized():
            return
        if self.path != '/jobs':
            return self._refuse(404, 'not found')
        try:
            length = int(self.headers.get('Content-Length', ''))
            video_kbps = int(self.headers.get('X-Video-Kbps', ''))
            expected = self.headers.get('X-Sha256', '').lower()
            if length <= 0 or video_kbps <= 0 or not _HEX64.fullmatch(expected):
                raise ValueError()
        except ValueError:
            return self._refuse(400, 'Content-Length, X-Sha256 and X-Video-Kbps are required')
        if not self.agent.enough_space(length):
            return self._refuse(507, 'not enough free space for this file')
        job_id = self.agent.reserve()
        if job_id is None:
            return self._refuse(409, 'busy')
        digest = hashlib.sha256()
        try:
            with open(self.agent.jobs[job_id]['source'], 'wb') as stream:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ConnectionError('upload ended early')
                    digest.update(chunk)
                    stream.write(chunk)
                    remaining -= len(chunk)
        except (OSError, ConnectionError):
            self.agent.abandon(job_id)
            self.close_connection = True
            return
        if digest.hexdigest() != expected:
            self.agent.abandon(job_id)
            return self._json(422, {'error': 'sha256 mismatch'})
        self.agent.start(job_id, video_kbps)
        return self._json(201, {'job_id': job_id})


def make_server(agent, host='0.0.0.0', port=8787):
    handler = type('AgentHandler', (Handler,), {'agent': agent})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.agent = agent
    return server


def reaper(agent, stop, interval_s=60):
    """Thread body: delete uncollected finished jobs until stop is set."""
    while not stop.wait(interval_s):
        agent.reap()
