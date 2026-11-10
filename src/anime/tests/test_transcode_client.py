"""Shared-client budgets, failover, waiting, slots, settings and private tokens."""
import json
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from src.anime.configuration_schema import ConfigurationSnapshot, ConfigurationError, convert_document
from src.anime.secrets import SecretStore
from src.anime.services import transcode as client
from src.anime.services.adapters.transcode import TranscodeAdapter
from src.transcode_agent.server import Agent, make_server


class Secrets:
    def transcode_token(self, address): return 'test-token'


def config(**values):
    return ConfigurationSnapshot(1, {'transcode': {'mode': 'remote', 'target_mb': 1, 'retries': 0,
                                'agents': ['http://first:8787', 'http://second:8787'], **values}})


def test_bitrate_audio_and_version_defaults():
    info = {'duration': 1440, 'streams': [{'codec_type': 'audio', 'codec_name': 'aac', 'bit_rate': '128000'},
                                          {'codec_type': 'audio', 'codec_name': 'flac', 'channels': 2}]}
    assert client.video_kbps(info, 500) == 2494
    assert client.target_bytes(720, 500) == 250_000_000
    current = convert_document({'schema_version': 16, 'revision': 7, 'settings': {}})
    assert current.schema_version == 17 and current.revision == 8
    assert current.value('transcode.mode') == 'off'
    assert current.value('transcode.encoders') == 'gpu,cpu'
    with pytest.raises(ConfigurationError): config(agents=['http://user:secret@host:8787'])
    with pytest.raises(ConfigurationError): config(agents=['http://host:8787/path'])
    with pytest.raises(ConfigurationError): config(mode='sometimes')


def test_tokens_remain_private(tmp_path):
    store = SecretStore(tmp_path)
    store.initialize()
    store.update_transcode_token('agent:8787', 'very-private-token', expected_revision=1)
    assert store.transcode_token('http://agent:8787') == 'very-private-token'
    assert store.status().transcode_agents == ('http://agent:8787',)
    assert 'very-private-token' not in repr(store.status())
    assert 'very-private-token' not in json.dumps(store.status().__dict__)
    assert (tmp_path/'secrets.json').stat().st_mode & 0o077 == 0


def test_failover_wait_and_three_attempts(tmp_path):
    source, output = tmp_path/'source', tmp_path/'result'
    source.write_bytes(b'x'*4096)
    info = {'duration': 1, 'streams': []}
    calls, attempts = [], []
    class Remote:
        token = 'test'
        fail = False
        all_down = False
        def __init__(self, address, token): self.address = address
        def health(self):
            if self.all_down or 'first' in self.address: raise ConnectionError()
            return {'busy': False, 'free_bytes': 100000}
        def run(self, source, output, rate, audio, flag, report, started):
            calls.append(self.address)
            started()
            if self.fail: raise client.TranscodeFailed('broken encode')
            output.write_bytes(b'encoded')
    with patch.object(client.encode, 'check'):
        assert client.transcode(source, output, config(), Secrets(), threading.Event(), info=info,
                                remote_factory=Remote, started=lambda: attempts.append(1))
        assert calls == ['http://second:8787'] and len(attempts) == 1
        Remote.all_down = True
        with pytest.raises(client.TranscodeWaiting):
            client.transcode(source, output, config(), Secrets(), threading.Event(), info=info, remote_factory=Remote, attempts=1)
        Remote.all_down = False
        Remote.fail = True
        attempts.clear()
        with pytest.raises(client.TranscodeFailed, match='broken encode'):
            client.transcode(source, output, config(), Secrets(), threading.Event(), info=info,
                             remote_factory=Remote, started=lambda: attempts.append(1))
        assert len(attempts) == 3
        assert not output.with_name('result.tmp').exists()


def test_off_under_target_and_priority(tmp_path):
    source, output = tmp_path/'source', tmp_path/'output'
    source.write_bytes(b'x')
    def forbidden(*args): raise AssertionError('must not contact an agent')
    assert not client.transcode(source, output, config(mode='off'), Secrets(), threading.Event(), remote_factory=forbidden)
    assert not client.transcode(source, output, config(), Secrets(), threading.Event(), info={'duration': 10, 'streams': []}, remote_factory=forbidden)
    with client.pipeline_priority():
        with client.slot('agent', batch=True) as acquired: assert not acquired
        with client.slot('agent') as acquired:
            assert acquired
            with client.slot('agent') as duplicate: assert not duplicate
    with client.slot('agent', batch=True) as acquired: assert acquired


def test_remote_wire_hash_and_download(tmp_path):
    def encode(source, output, **kwargs):
        Path(output).write_bytes(b'result')
        return {'encoder': 'cpu'}
    agent = Agent(tmp=tmp_path/'agent', token='test-token', encoders='cpu', transcode=encode)
    server = make_server(agent, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source, output = tmp_path/'source', tmp_path/'result'
    source.write_bytes(b'x'*4096)
    remote = client.Remote(f'127.0.0.1:{server.server_port}', 'test-token')
    try:
        remote.run(source, output, 100, 128, threading.Event(), lambda _: None, lambda: None)
        assert output.read_bytes() == b'result'
        assert not agent.jobs
    finally:
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize('alive,status', [(2,'UP'),(1,'UP'),(0,'DOWN')])
def test_agent_health(alive, status):
    seen = []
    def health(remote):
        seen.append(remote.address)
        if len(seen) > alive: raise ConnectionError()
        return {'busy': True, 'encoders': ['gpu']}
    with patch.object(client.Remote, 'health', health):
        result = TranscodeAdapter().probe(config(), Secrets())
    assert result.status == status
    assert f'Agents alive: {alive}/2' in result.detail
    for mode in ('off','local'):
        assert TranscodeAdapter().probe(config(mode=mode), Secrets()).status == 'DISABLED'


def test_local_disk_waits_without_spending_attempt(tmp_path):
    source, output = tmp_path/'source', tmp_path/'result'
    source.write_bytes(b'x'*4096)
    attempts = []
    with patch.object(client.encode, 'free_bytes', return_value=1):
        with pytest.raises(client.TranscodeWaiting):
            client.transcode(source, output, config(mode='local', tmp=str(tmp_path/'local')),
                             Secrets(), threading.Event(), info={'duration':1,'streams':[]},
                             started=lambda: attempts.append(1))
    assert attempts == [] and not output.exists()


def test_agent_dies_after_upload_then_next_agent_used(tmp_path):
    source, output = tmp_path/'source', tmp_path/'result'
    source.write_bytes(b'x'*4096)
    attempts = []
    class Remote:
        token = 'test'
        def __init__(self, address, token): self.address = address
        def health(self): return {'busy':False,'free_bytes':100000}
        def run(self, source, output, rate, audio, flag, report, started):
            started(); attempts.append(self.address)
            if 'first' in self.address: raise client.TranscodeFailed('HTTP 404: agent restarted')
            output.write_bytes(b'result')
    with patch.object(client.encode, 'check'):
        client.transcode(source, output, config(), Secrets(), threading.Event(),
                         info={'duration':1,'streams':[]}, remote_factory=Remote)
    assert attempts == ['http://first:8787','http://second:8787']


def test_batch_uses_another_slot_while_pipeline_is_encoding():
    with client.pipeline_priority() as acquired:
        with client.slot('first') as first:
            assert first
            acquired()
            with client.slot('second', batch=True) as second: assert second


@pytest.mark.parametrize('failure', ['hash','frozen'])
def test_remote_rejects_bad_hash_and_frozen_progress(tmp_path, failure):
    def encode(source, output, **kwargs):
        Path(output).write_bytes(b'result')
        return {'encoder':'cpu'}
    agent = Agent(tmp=tmp_path/'agent',token='test-token',encoders='cpu',transcode=encode)
    server = make_server(agent,'127.0.0.1',0)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    source, output = tmp_path/'source',tmp_path/'output.tmp'
    source.write_bytes(b'video')
    remote = client.Remote(f'127.0.0.1:{server.server_port}','test-token')
    class Flag:
        def is_set(self): return False
        def wait(self, seconds): return False
    original_request = remote.request
    original_result = agent.result
    def request(method, path, **kwargs):
        if method=='GET' and failure=='frozen': return {'state':'running','progress':0}
        return original_request(method,path,**kwargs)
    def result(job):
        value = original_result(job)
        return (value[0],'0'*64) if value and failure=='hash' else value
    try:
        with patch.object(remote,'request',side_effect=request), patch.object(agent,'result',side_effect=result):
            if failure=='frozen':
                with patch.object(client.time,'monotonic',side_effect=[0,1,62]):
                    with pytest.raises(client.TranscodeFailed,match='60 seconds'):
                        remote.run(source,output,100,128,Flag(),lambda _:None,lambda:None)
            else:
                with pytest.raises(client.TranscodeFailed,match='SHA-256 mismatch'):
                    remote.run(source,output,100,128,threading.Event(),lambda _:None,lambda:None)
        assert not agent.jobs
    finally:
        server.shutdown(); server.server_close(); thread.join()
