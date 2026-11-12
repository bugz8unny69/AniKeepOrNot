"""adm-agent.pyz server agent install/upgrade/rollback/status/token, with fake system commands.

Real archives are built and run (their --version), so archive validation is real;
systemctl, useradd, loginctl and the agent's health answer are fakes. These are
mocked service-command tests, not systemd verification.
"""
import io
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import pytest

from src.transcode_agent import AGENT_VERSION
from src.transcode_agent import install as agent_install
from src.transcode_agent.pack import pack

ENCODERS = ' V..... libx265 \n V..... hevc_vaapi \n A..... libopus \n'


def account(uid, name, home):
    return SimpleNamespace(pw_uid=uid, pw_name=name, pw_dir=str(home))


@pytest.fixture(scope='module')
def archives(tmp_path_factory):
    """The current archive and a copy claiming the next agent version."""
    folder = tmp_path_factory.mktemp('archives')
    current = Path(pack(folder / 'current.pyz')['path'])
    newer = folder / 'newer.pyz'
    with zipfile.ZipFile(current) as source, zipfile.ZipFile(newer, 'w', zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == 'src/transcode_agent/__init__.py':
                data = data.replace(f'AGENT_VERSION = {AGENT_VERSION}'.encode(), f'AGENT_VERSION = {AGENT_VERSION + 1}'.encode())
            if item.filename == 'src/transcode_agent/build.json':
                record = json.loads(data); record['agent_version'] = AGENT_VERSION + 1; record['revision'] = 'next'
                data = json.dumps(record).encode()
            target.writestr(item, data)
    with open(newer, 'r+b') as stream:                         # zipfile drops the shebang; restore it.
        body = stream.read(); stream.seek(0); stream.write(b'#!/usr/bin/env python3\n' + body)
    return {'current': current, 'newer': newer}


class FakeHost:
    """Records every command; the agent 'runs' whatever archive is installed."""
    def __init__(self, layout, *, manager=True, healthy=True, busy=False, linger='no'):
        self.layout, self.manager, self.healthy, self.busy, self.linger = layout, manager, healthy, busy, linger
        self.commands, self.output = [], []
        self.answers = {}
        self.host = agent_install.Host(run=self.run, health=self.health, ask=self.ask, secret=self.secret,
                                       python='/usr/bin/python3')

    def run(self, args, *, check=True, input=None):
        args = [str(a) for a in args]
        if args[0] == '/usr/bin/python3':                     # Archive checks really run.
            return agent_install.run_command(['python3', *args[1:]], check=check)
        self.commands.append(args)
        out, code = '', 0
        if args[:3] == ['systemctl', '--user', 'show-environment']:
            code = 0 if self.manager else 1
        elif args[:2] == ['loginctl', 'show-user']:
            out = self.linger + '\n'
        elif args[:2] == ['ffmpeg', '-hide_banner']:
            out = ENCODERS
        elif 'is-active' in args:
            out = 'active\n'
        if code and check:
            raise agent_install.InstallError(f'{args[0]} failed')
        return subprocess.CompletedProcess(args, code, out, '')

    def health(self, listen, token):
        if not self.healthy:
            raise ConnectionRefusedError(111, 'Connection refused')
        with zipfile.ZipFile(self.layout.host(self.layout.archive)) as zipped:
            version = json.loads(zipped.read('src/transcode_agent/build.json'))['agent_version']
        return {'agent_version': version, 'busy': self.busy, 'encoders': ['gpu', 'cpu'], 'free_bytes': 1}

    def ask(self, prompt, default):
        self.output.append(prompt)
        return self.answers.get(prompt.split(' [')[0], default)

    def secret(self, prompt):
        self.output.append(prompt)
        return self.answers.get('secret', '')

    def __call__(self, text):
        self.output.append(text)


def user_layout(tmp_path, environ=None):
    home = tmp_path / 'home/alice'
    return agent_install.detect(environ or {}, euid=1000, lookup=lambda uid: account(uid, 'alice', home), root=tmp_path / 'root')


def system_layout(tmp_path):
    return agent_install.detect({'USER': 'alice', 'SUDO_USER': 'alice', 'HOME': '/home/alice'}, euid=0,
                                lookup=lambda uid: account(0, 'root', '/root'), root=tmp_path / 'root')


@pytest.fixture(autouse=True)
def tools():
    with patch.object(agent_install.shutil, 'which', side_effect=lambda name: f'/usr/bin/{name}'):
        yield


def do(command, layout, fake, **options):
    return agent_install.COMMANDS[command](options, layout, fake.host, out=fake)


# --- Scope -------------------------------------------------------------------------------

def test_root_is_a_system_install_whatever_user_and_sudo_user_say(tmp_path):
    layout = system_layout(tmp_path)
    assert layout.scope == 'system' and layout.account == 'root' and layout.runtime_account == 'transcode'
    assert str(layout.archive) == '/opt/transcode-agent/adm-agent.pyz'
    assert str(layout.unit_file) == '/etc/systemd/system/transcode-agent.service'
    assert layout.announcement() == 'Installing as root: system service (runs as transcode)'


def test_non_root_is_a_user_install_in_xdg_or_home_folders(tmp_path):
    layout = user_layout(tmp_path, {'USER': 'mallory', 'XDG_CONFIG_HOME': str(tmp_path / 'cfg'), 'XDG_DATA_HOME': 'relative/ignored'})
    home = tmp_path / 'home/alice'
    assert layout.scope == 'user' and layout.account == 'alice'
    assert layout.archive == home / '.local/share/anikeepornot/transcode-agent/adm-agent.pyz'
    assert layout.env_file == tmp_path / 'cfg/anikeepornot/transcode-agent/transcode-agent.env'
    assert layout.unit_file == tmp_path / 'cfg/systemd/user/transcode-agent.service'
    assert layout.announcement() == 'Installing as alice: user service (systemctl --user)'


def test_there_is_no_scope_option(archives):
    from src.transcode_agent.cli import parser
    commands = parser()._subparsers._group_actions[0].choices
    options = {option for command in commands.values() for action in command._actions for option in action.option_strings}
    assert not options & {'--user', '--system', '--scope'}


# --- Install -----------------------------------------------------------------------------

def test_user_install_writes_a_user_unit_and_touches_nothing_system_wide(tmp_path, archives):
    layout = user_layout(tmp_path)
    fake = FakeHost(layout)
    assert do('install', layout, fake, source=archives['current'], yes=True) == 0
    assert fake.output[0] == 'Installing as alice: user service (systemctl --user)'
    unit = layout.host(layout.unit_file).read_text()
    settings = [line.split('=')[0] for line in unit.splitlines() if '=' in line and not line.startswith('#')]
    assert not {'User', 'Group', 'SupplementaryGroups'} & set(settings)
    assert 'WantedBy=default.target' in unit
    assert f'ExecStart=/usr/bin/python3 {layout.archive} server agent run' in unit
    assert f'EnvironmentFile={layout.env_file}' in unit
    env = agent_install.read_env(layout.host(layout.env_file))
    assert re.fullmatch('[0-9a-f]{64}', env['TRANSCODE_TOKEN'])
    assert env['TRANSCODE_TMP'] == str(tmp_path / 'home/alice/.cache/anikeepornot')
    assert layout.host(layout.env_file).stat().st_mode & 0o777 == 0o600
    for command in fake.commands:
        assert command[0] in ('systemctl', 'loginctl', 'ffmpeg') and command[0] != 'sudo'
        assert command[0] != 'systemctl' or command[1] == '--user'
    assert not (tmp_path / 'root/etc').exists() and not (tmp_path / 'root/opt').exists()
    text = '\n'.join(fake.output)
    assert env['TRANSCODE_TOKEN'] not in text                  # The token is shown only by `token`.
    assert 'lingering is off' in text and 'enable-linger alice' in text
    assert 'Settings > Transcode > Remote' in text


def test_system_install_creates_the_account_and_a_system_unit(tmp_path, archives):
    layout = system_layout(tmp_path)
    fake = FakeHost(layout)
    with patch.object(agent_install.pwd, 'getpwnam', side_effect=KeyError('transcode')):
        assert do('install', layout, fake, source=archives['current'], yes=True, encoders='cpu') == 0
    assert ['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', '/nonexistent',
            '--shell', '/usr/sbin/nologin', 'transcode'] in fake.commands
    unit = layout.host(layout.unit_file).read_text()
    assert 'User=transcode' in unit and 'WantedBy=multi-user.target' in unit
    assert ['systemctl', 'restart', 'transcode-agent.service'] in fake.commands
    assert all(c[1] != '--user' for c in fake.commands if c[0] == 'systemctl')
    state = json.loads(layout.host(layout.state_file).read_text())
    assert state['scope'] == 'system' and state['owner_uid'] == 0 and state['installed']['agent_version'] == AGENT_VERSION


def test_rerun_keeps_token_and_settings_and_a_given_token_is_used(tmp_path, archives):
    layout = user_layout(tmp_path)
    fake = FakeHost(layout)
    do('install', layout, fake, source=archives['current'], yes=True, listen='0.0.0.0:9000', token='a' * 32)
    before = agent_install.read_env(layout.host(layout.env_file))
    assert before['TRANSCODE_TOKEN'] == 'a' * 32 and before['TRANSCODE_LISTEN'] == '0.0.0.0:9000'
    do('install', layout, FakeHost(layout), source=archives['current'], yes=True)
    assert agent_install.read_env(layout.host(layout.env_file)) == before


def test_interactive_install_asks_and_generates_a_token_on_enter(tmp_path, archives):
    layout = user_layout(tmp_path)
    fake = FakeHost(layout)
    fake.answers = {'Listen address and port': '127.0.0.1:8800', 'Encoders: gpu,cpu (GPU, then CPU), gpu or cpu': 'cpu'}
    do('install', layout, fake, source=archives['current'])
    env = agent_install.read_env(layout.host(layout.env_file))
    assert env['TRANSCODE_LISTEN'] == '127.0.0.1:8800' and env['TRANSCODE_ENCODERS'] == 'cpu'
    assert len(env['TRANSCODE_TOKEN']) == 64
    assert not any(p.startswith('GPU render device') for p in fake.output)   # Not asked for CPU only.


def test_missing_user_manager_stops_before_any_change(tmp_path, archives):
    layout = user_layout(tmp_path)
    with pytest.raises(agent_install.InstallError, match='Log in as alice directly'):
        do('install', layout, FakeHost(layout, manager=False), source=archives['current'], yes=True)
    assert not layout.host(layout.archive).exists() and not layout.host(layout.env_file).exists()


def test_missing_ffmpeg_is_reported_with_advice(tmp_path, archives):
    layout = user_layout(tmp_path)
    with patch.object(agent_install.shutil, 'which', side_effect=lambda name: None if name == 'ffmpeg' else '/usr/bin/' + name), \
            pytest.raises(agent_install.InstallError, match='an administrator can install them'):
        do('install', layout, FakeHost(layout), source=archives['current'], yes=True)


def test_system_install_offers_apt_for_missing_packages(tmp_path, archives):
    layout = system_layout(tmp_path)
    fake = FakeHost(layout)
    present = {'value': False}
    real_run = fake.run
    def run(args, **kw):
        if args[:2] == ['ffmpeg', '-hide_banner'] and not present['value']:
            return subprocess.CompletedProcess(args, 0, '', '')
        if 'install' in args and 'apt-get' in args:
            present['value'] = True
        return real_run(args, **kw)
    fake.host.run = run
    with patch.object(agent_install.pwd, 'getpwnam'):
        do('install', layout, fake, source=archives['current'], yes=True, encoders='cpu', install_deps=True)
    assert ['apt-get', 'update'] in fake.commands


def test_unhealthy_first_start_fails_and_records_nothing(tmp_path, archives):
    layout = user_layout(tmp_path)
    with patch.object(agent_install, 'HEALTH_WAIT_S', 0), pytest.raises(agent_install.InstallError, match='health check'):
        do('install', layout, FakeHost(layout, healthy=False), source=archives['current'], yes=True)
    assert not layout.host(layout.state_file).exists()


def test_a_damaged_archive_is_refused_before_anything_changes(tmp_path):
    layout = user_layout(tmp_path)
    bad = tmp_path / 'bad.pyz'; bad.write_bytes(b'not a zip')
    with pytest.raises(agent_install.InstallError, match='not a readable agent archive'):
        do('install', layout, FakeHost(layout), source=bad, yes=True)
    assert not layout.host(layout.archive).exists()


def test_user_install_refuses_the_system_agents_port(tmp_path, archives):
    layout = user_layout(tmp_path)
    system_state = layout.host(agent_install.SYSTEM_STATE)
    system_state.parent.mkdir(parents=True); system_state.write_text(json.dumps({'listen': '0.0.0.0:8787'}))
    with pytest.raises(agent_install.InstallError, match='already uses port 8787'):
        do('install', layout, FakeHost(layout), source=archives['current'], yes=True)


def test_another_accounts_install_is_not_touched(tmp_path, archives):
    layout = user_layout(tmp_path)
    do('install', layout, FakeHost(layout), source=archives['current'], yes=True)
    state = json.loads(layout.host(layout.state_file).read_text()); state['owner_uid'] = 1001; state['owner'] = 'bob'
    layout.host(layout.state_file).write_text(json.dumps(state))
    with pytest.raises(agent_install.InstallError, match='belongs to bob'):
        do('upgrade', layout, FakeHost(layout), source=archives['current'])


# --- Upgrade, rollback, token, status ------------------------------------------------------

def installed(tmp_path, archives):
    layout = user_layout(tmp_path)
    do('install', layout, FakeHost(layout), source=archives['current'], yes=True)
    return layout


def test_upgrade_keeps_the_old_archive_and_settings(tmp_path, archives):
    layout = installed(tmp_path, archives)
    env = layout.host(layout.env_file).read_bytes()
    fake = FakeHost(layout)
    assert do('upgrade', layout, fake, source=archives['newer']) == 0
    state = json.loads(layout.host(layout.state_file).read_text())
    assert state['installed']['agent_version'] == AGENT_VERSION + 1 and state['previous']['agent_version'] == AGENT_VERSION
    assert layout.host(layout.previous).read_bytes() == archives['current'].read_bytes()
    assert layout.host(layout.env_file).read_bytes() == env


def test_upgrade_waits_for_a_running_encode_unless_told_to_interrupt(tmp_path, archives):
    layout = installed(tmp_path, archives)
    with pytest.raises(agent_install.InstallError, match='encoding right now'):
        do('upgrade', layout, FakeHost(layout, busy=True), source=archives['newer'])
    assert layout.host(layout.archive).read_bytes() == archives['current'].read_bytes()
    assert do('upgrade', layout, FakeHost(layout, busy=True), source=archives['newer'], interrupt=True) == 0


def test_a_failed_upgrade_restores_the_previous_archive(tmp_path, archives):
    layout = installed(tmp_path, archives)
    fake = FakeHost(layout)
    real_health = fake.health
    def health(listen, token):                                  # The new version never answers.
        answer = real_health(listen, token)
        if answer['agent_version'] != AGENT_VERSION: raise ConnectionRefusedError(111, 'refused')
        return answer
    fake.host.health = health
    with patch.object(agent_install, 'HEALTH_WAIT_S', 0), pytest.raises(agent_install.InstallError, match='upgrade failed'):
        do('upgrade', layout, fake, source=archives['newer'])
    assert layout.host(layout.archive).read_bytes() == archives['current'].read_bytes()
    assert any('was restored and is running' in line for line in fake.output)
    assert json.loads(layout.host(layout.state_file).read_text())['installed']['agent_version'] == AGENT_VERSION


def test_rollback_swaps_versions(tmp_path, archives):
    layout = installed(tmp_path, archives)
    do('upgrade', layout, FakeHost(layout), source=archives['newer'])
    assert do('rollback', layout, FakeHost(layout)) == 0
    assert layout.host(layout.archive).read_bytes() == archives['current'].read_bytes()
    assert layout.host(layout.previous).read_bytes() == archives['newer'].read_bytes()
    state = json.loads(layout.host(layout.state_file).read_text())
    assert state['installed']['agent_version'] == AGENT_VERSION and state['previous']['agent_version'] == AGENT_VERSION + 1


def test_token_is_shown_only_on_request_and_can_be_replaced(tmp_path, archives):
    layout = installed(tmp_path, archives)
    fake = FakeHost(layout)
    do('token', layout, fake)
    token = agent_install.read_env(layout.host(layout.env_file))['TRANSCODE_TOKEN']
    assert fake.output[0] == f'TRANSCODE_TOKEN={token}'
    fake = FakeHost(layout); fake.answers['secret'] = 'b' * 40
    do('token', layout, fake, set=True)
    assert agent_install.read_env(layout.host(layout.env_file))['TRANSCODE_TOKEN'] == 'b' * 40
    assert ['systemctl', '--user', 'restart', 'transcode-agent.service'] in fake.commands


def test_status_reports_version_service_and_health(tmp_path, archives):
    layout = installed(tmp_path, archives)
    fake = FakeHost(layout)
    assert do('status', layout, fake) == 0
    assert f'service active, installed version {AGENT_VERSION}' in fake.output[0]
    assert fake.output[1].startswith('health: idle')


def test_commands_before_install_explain_what_to_do(tmp_path):
    layout = user_layout(tmp_path)
    with pytest.raises(agent_install.InstallError, match='no user agent is installed for alice'):
        do('status', layout, FakeHost(layout))
