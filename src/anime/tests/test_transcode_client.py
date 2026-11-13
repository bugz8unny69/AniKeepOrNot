"""Shared-client budgets, failover, waiting, slots, settings and private tokens."""
import itertools
import json
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from src.anime.configuration_schema import ConfigurationSnapshot, ConfigurationError, convert_document
from src.anime.secrets import SecretStore
from src.anime.services import transcode as client
from src.anime.services.adapters.transcode import TranscodeAdapter
from src.transcode_agent import AGENT_VERSION
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
            return {'busy': False, 'free_bytes': 100000, 'agent_version': AGENT_VERSION}
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
        return {'busy': True, 'encoders': ['gpu'], 'agent_version': AGENT_VERSION}
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
        def health(self): return {'busy':False,'free_bytes':100000,'agent_version':AGENT_VERSION}
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
                with patch.object(client.time,'monotonic',side_effect=itertools.count(0, 31)):
                    with pytest.raises(client.TranscodeFailed,match='60 seconds'):
                        remote.run(source,output,100,128,Flag(),lambda _:None,lambda:None)
            else:
                with pytest.raises(client.TranscodeFailed,match='SHA-256 mismatch'):
                    remote.run(source,output,100,128,threading.Event(),lambda _:None,lambda:None)
        assert not agent.jobs
    finally:
        server.shutdown(); server.server_close(); thread.join()


# --- Agent versions (work item 1a) -------------------------------------------------------

@pytest.mark.parametrize('health,expected', [
    ({'agent_version': AGENT_VERSION}, None),
    ({}, 'reports no version (an older agent); this worker requires version'),
    ({'agent_version': AGENT_VERSION - 1}, f'is version {AGENT_VERSION - 1}; this worker requires version {AGENT_VERSION}. Update this agent.'),
    ({'agent_version': AGENT_VERSION + 1}, f'is version {AGENT_VERSION + 1}; this worker supports version {AGENT_VERSION}. Update AniKeepOrNot'),
    ({'agent_version': '1'}, 'reports an unreadable version'),
    ({'agent_version': True}, 'reports an unreadable version'),
    ({'agent_version': -3}, 'reports an unreadable version'),
], ids=['match', 'missing', 'older', 'newer', 'string', 'bool', 'negative'])
def test_version_rule_is_an_exact_match(health, expected):
    problem = client.compatibility('http://pc:8787', health)
    if expected is None:
        assert problem is None
    else:
        assert problem.startswith('Agent pc:8787 ') and expected in problem


class VersionedRemote:
    """A fake agent reporting a chosen version; records uploads."""
    versions = {}
    uploads = []

    def __init__(self, address, token):
        self.address, self.token = address, token

    def health(self):
        version = self.versions[self.address]
        answer = {'busy': False, 'free_bytes': 10**9, 'encoders': ['cpu']}
        if version is not None:
            answer['agent_version'] = version
        return answer

    def run(self, source, output, rate, audio, flag, report, started):
        self.uploads.append(self.address)
        started()
        output.write_bytes(b'encoded')


def versioned(tmp_path, versions):
    VersionedRemote.versions, VersionedRemote.uploads = versions, []
    source, output = tmp_path/'source', tmp_path/'result'
    source.write_bytes(b'x' * 4096)
    spent = []
    run = lambda **kw: client.transcode(source, output, config(agents=list(versions)), Secrets(), threading.Event(),
                                        info={'duration': 1, 'streams': []}, remote_factory=VersionedRemote,
                                        started=lambda: spent.append(1), **kw)
    return run, output, spent


def test_an_agent_needing_an_update_is_skipped_for_a_compatible_one(tmp_path):
    run, output, spent = versioned(tmp_path, {'http://first:8787': AGENT_VERSION - 1, 'http://second:8787': AGENT_VERSION})
    with patch.object(client.encode, 'check'), patch.object(client, 'video_kbps', return_value=100):
        assert run() is True
    assert VersionedRemote.uploads == ['http://second:8787']      # The older agent never got the file.
    assert len(spent) == 1


@pytest.mark.parametrize('version', [None, AGENT_VERSION - 1, AGENT_VERSION + 1, 'bad'])
def test_when_every_agent_needs_an_update_the_job_waits_and_spends_nothing(tmp_path, version):
    run, output, spent = versioned(tmp_path, {'http://first:8787': version, 'http://second:8787': version})
    with patch.object(client, 'video_kbps', return_value=100), pytest.raises(client.TranscodeWaiting) as waiting:
        run()
    assert VersionedRemote.uploads == [] and spent == []
    assert 'Agent first:8787' in str(waiting.value) and 'Agent second:8787' in str(waiting.value)
    assert ('Update this agent' in str(waiting.value)) != (version == AGENT_VERSION + 1)


def test_an_updated_agent_takes_work_again_without_a_worker_restart(tmp_path):
    run, output, spent = versioned(tmp_path, {'http://first:8787': AGENT_VERSION - 1})
    with patch.object(client, 'video_kbps', return_value=100), pytest.raises(client.TranscodeWaiting):
        run()
    VersionedRemote.versions['http://first:8787'] = AGENT_VERSION       # The operator upgraded the agent.
    with patch.object(client.encode, 'check'), patch.object(client, 'video_kbps', return_value=100):
        assert run() is True
    assert VersionedRemote.uploads == ['http://first:8787']


def test_health_names_each_agents_problem():
    answers = {
        'http://ok:8787': {'busy': False, 'encoders': ['gpu', 'cpu'], 'agent_version': AGENT_VERSION},
        'http://old:8787': {'busy': False, 'encoders': ['cpu']},
        'http://wrong:8787': client.TranscodeFailed('Agent returned HTTP 401', status=401),
        'http://off:8787': ConnectionRefusedError(111, 'Connection refused'),
    }
    tokens = {'http://notoken:8787': ''}

    class Fake:
        def __init__(self, address, token): self.address = address
        def health(self):
            answer = answers[self.address]
            if isinstance(answer, Exception): raise answer
            return answer

    class Tokens:
        def transcode_token(self, address): return tokens.get(address, 't')
    with patch('src.anime.services.adapters.transcode.Remote', Fake):
        result = TranscodeAdapter().probe(config(agents=[*answers, 'http://notoken:8787']), Tokens())
    assert result.status == 'UP'
    assert result.detail.startswith('Agents alive: 1/5. ')
    for text in ('ok:8787: up, idle, gpu,cpu, version', 'Agent old:8787 reports no version',
                 'wrong:8787: token rejected', 'off:8787: unreachable (Connection refused)', 'notoken:8787: no token set'):
        assert text in result.detail


@pytest.mark.parametrize('answer,status', [
    ({'busy': False, 'encoders': ['cpu'], 'agent_version': AGENT_VERSION + 1}, 'UNSUPPORTED'),
    (client.TranscodeFailed('Agent returned HTTP 401', status=401), 'AUTH_FAILED'),
    (OSError(113, 'No route to host'), 'DOWN'),
])
def test_health_status_when_no_agent_can_work(answer, status):
    class Fake:
        def __init__(self, address, token): pass
        def health(self):
            if isinstance(answer, Exception): raise answer
            return answer
    with patch('src.anime.services.adapters.transcode.Remote', Fake):
        result = TranscodeAdapter().probe(config(agents=['http://only:8787']), Secrets())
    assert result.status == status and result.detail.startswith('Agents alive: 0/1. ')


def test_no_agents_or_no_tokens_is_a_setup_problem():
    assert TranscodeAdapter().probe(config(agents=[]), Secrets()).status == 'NOT_CONFIGURED'

    class NoTokens:
        def transcode_token(self, address): return ''
    assert TranscodeAdapter().probe(config(), NoTokens()).status == 'NOT_CONFIGURED'


def test_each_skipped_agent_is_logged_with_its_reason(tmp_path, caplog):
    source, output = tmp_path/'episode.mkv', tmp_path/'result'
    source.write_bytes(b'x'*4096)
    answers = {'http://first:8787': {'busy': True, 'free_bytes': 10**9},
               'http://second:8787': {'busy': False, 'free_bytes': 10}}
    class Remote:
        token = 'test'
        def __init__(self, address, token): self.address = address
        def health(self): return {**answers[self.address], 'agent_version': AGENT_VERSION}
    with caplog.at_level('INFO', logger='anime.worker.transcode'), pytest.raises(client.TranscodeWaiting):
        client.transcode(source, output, config(), Secrets(), threading.Event(), info={'duration': 1, 'streams': []},
                         remote_factory=Remote)
    text = caplog.text
    assert 'episode.mkv: skipped first:8787: the agent reports a job running' in text
    assert 'episode.mkv: skipped second:8787: 10 B free on the agent, needs 6.1 kB' in text
    assert 'episode.mkv: no agent took it; waiting' in text
