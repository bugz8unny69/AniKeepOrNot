"""Install, upgrade and roll back the agent archive as a systemd service.

Scope comes from the effective UID only: root installs a system service (runs as
the `transcode` account), anyone else installs a user service for themselves.
There is no switch for it, and nothing is ever elevated. The scope, owner and
paths are recorded in install.json and every later command checks them.

Everything that touches the host goes through a `run(args)` callable and the
health check through `health(...)`, so tests use fakes. Paths are written under
`root` (normally /); file contents always use the real runtime paths.

The token is written only to the private environment file. It is shown only by
the explicit `server agent token` command, never in logs or other output.
"""
from dataclasses import dataclass, field
import getpass
import grp
import hashlib
import json
import os
from pathlib import Path
import pkgutil
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from src.transcode_agent import AGENT_VERSION, BUILD_RESOURCE, in_archive

UNIT = 'transcode-agent.service'
SYSTEM_ACCOUNT = 'transcode'
ARCHIVE = 'adm-agent.pyz'
DEFAULTS = {'TRANSCODE_LISTEN': '0.0.0.0:8787', 'TRANSCODE_ENCODERS': 'gpu,cpu',
            'TRANSCODE_VAAPI_DEVICE': '/dev/dri/renderD128'}
KEYS = ('TRANSCODE_LISTEN', 'TRANSCODE_TOKEN', 'TRANSCODE_ENCODERS', 'TRANSCODE_VAAPI_DEVICE', 'TRANSCODE_TMP')
HEALTH_WAIT_S = 20
RETRY_DELAYS = (5, 20)
_SAFE_PATH = re.compile(r'[A-Za-z0-9_./@%+=:,-]+')


class InstallError(RuntimeError):
    pass


# --- Where things go -----------------------------------------------------------------

@dataclass
class Layout:
    scope: str                 # 'system' or 'user'
    account: str               # who runs the installer
    uid: int
    runtime_account: str       # who runs the service
    app_dir: Path
    config_dir: Path
    env_file: Path
    unit_file: Path
    state_file: Path
    default_tmp: Path
    root: Path = field(default=Path('/'))

    @property
    def archive(self): return self.app_dir / ARCHIVE

    @property
    def previous(self): return self.app_dir / (ARCHIVE + '.previous')

    @property
    def user(self): return self.scope == 'user'

    def host(self, path):
        """Where a runtime path is written (under root in tests)."""
        return self.root / Path(path).relative_to('/')

    def systemctl(self, *args):
        return ['systemctl', *(['--user'] if self.user else []), *args]

    def announcement(self):
        if self.user:
            return f'Installing as {self.account}: user service (systemctl --user)'
        return f'Installing as {self.account}: system service (runs as {self.runtime_account})'


def _xdg(environ, name, home, default):
    value = environ.get(name, '')
    return Path(value) if value and os.path.isabs(value) else home / default


def detect(environ=os.environ, *, euid=None, lookup=pwd.getpwuid, root=Path('/')):
    """The layout for whoever is really running this: the effective UID, never $USER or $SUDO_USER."""
    euid = os.geteuid() if euid is None else euid
    try:
        entry = lookup(euid)
    except KeyError:
        raise InstallError(f'no account exists for user id {euid}') from None
    if euid == 0:
        return Layout('system', entry.pw_name, 0, SYSTEM_ACCOUNT, Path('/opt/transcode-agent'),
                      Path('/etc'), Path('/etc/transcode-agent.env'), Path('/etc/systemd/system') / UNIT,
                      Path('/var/lib/transcode-agent/install.json'), Path('/var/tmp'), Path(root))
    home = Path(entry.pw_dir)
    data = _xdg(environ, 'XDG_DATA_HOME', home, '.local/share') / 'anikeepornot/transcode-agent'
    config = _xdg(environ, 'XDG_CONFIG_HOME', home, '.config')
    cache = _xdg(environ, 'XDG_CACHE_HOME', home, '.cache') / 'anikeepornot'
    return Layout('user', entry.pw_name, euid, entry.pw_name, data, config / 'anikeepornot/transcode-agent',
                  config / 'anikeepornot/transcode-agent/transcode-agent.env', config / 'systemd/user' / UNIT,
                  data / 'install.json', cache, Path(root))


SYSTEM_STATE = Path('/var/lib/transcode-agent/install.json')


# --- Small file helpers ----------------------------------------------------------------

def atomic_write(path, data, mode):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data if isinstance(data, bytes) else data.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def template(name):
    """A maintained deploy/transcode-agent template: from the archive, else the checkout."""
    if in_archive():
        data = pkgutil.get_data('src.transcode_agent', 'templates/' + name)
        if data is None:
            raise InstallError(f'the archive is missing its {name} template')
        return data.decode()
    return (Path(__file__).resolve().parents[2] / 'deploy/transcode-agent' / name).read_text()


def read_env(path):
    values = {}
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        values[key.strip()] = value
    return values


def _env_value(value):
    if _SAFE_PATH.fullmatch(value):
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def render_env(values, text):
    """The maintained example with our values filled in; comments kept."""
    lines, seen = [], set()
    for line in text.splitlines():
        key = line.split('=', 1)[0].strip()
        if not line.lstrip().startswith('#') and '=' in line and key in values:
            lines.append(f'{key}={_env_value(values[key])}')
            seen.add(key)
        else:
            lines.append(line)
    lines += [f'{key}={_env_value(values[key])}' for key in KEYS if key in values and key not in seen]
    return '\n'.join(lines) + '\n'


def render_unit(text, layout, python):
    """The maintained unit with real paths; user scope drops the system-only account lines."""
    for path in (python, layout.archive, layout.env_file, layout.app_dir):
        if not _SAFE_PATH.fullmatch(str(path)):
            raise InstallError(f'paths with spaces or special characters are not supported: {path}')
    replace = {'ExecStart': f'{python} {layout.archive} server agent', 'EnvironmentFile': str(layout.env_file),
               'WorkingDirectory': str(layout.app_dir)}
    if layout.user:
        replace['WantedBy'] = 'default.target'
    out = []
    for line in text.splitlines():
        key = line.split('=', 1)[0].strip()
        if layout.user and key in ('User', 'Group', 'SupplementaryGroups'):
            continue
        out.append(f'{key}={replace[key]}' if '=' in line and not line.startswith('#') and key in replace else line)
    return '\n'.join(out) + '\n'


# --- Host interaction ------------------------------------------------------------------

def run_command(args, *, check=True, input=None):
    result = subprocess.run([str(a) for a in args], cwd='/', input=input, text=True, capture_output=True)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise InstallError(f"{Path(str(args[0])).name} {' '.join(map(str, args[1:3]))} failed: "
                           f"{detail[-1] if detail else 'status ' + str(result.returncode)}")
    return result


def with_retries(run, args, *, sleep=time.sleep):
    """A package download: retried twice before giving up."""
    for attempt, delay in enumerate((*RETRY_DELAYS, None), 1):
        try:
            return run(args)
        except InstallError as exc:
            if delay is None:
                raise
            print(f'{exc} (attempt {attempt}); retrying in {delay}s...', flush=True)
            sleep(delay)


def connect_host(listen):
    host = listen.rpartition(':')[0].strip('[]')
    return '127.0.0.1' if host in ('0.0.0.0', '::', '') else host


def http_health(listen, token, *, timeout=2):
    """GET /health on the local agent. Returns the answer, or raises OSError/ValueError."""
    host, port = connect_host(listen), int(listen.rpartition(':')[2])
    target = f'[{host}]' if ':' in host else host
    request = urllib.request.Request(f'http://{target}:{port}/health', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(65536))


def wait_healthy(health, listen, token, *, wait_s=HEALTH_WAIT_S, sleep=time.sleep):
    deadline, last = time.monotonic() + wait_s, 'no answer'
    while True:
        try:
            return health(listen, token)
        except urllib.error.HTTPError as exc:
            last = 'token rejected (HTTP 401)' if exc.code == 401 else f'HTTP {exc.code}'
        except (OSError, ValueError) as exc:
            last = str(getattr(exc, 'reason', exc))
        if time.monotonic() >= deadline:
            raise InstallError(f'the agent did not answer its health check: {last}')
        sleep(.5)


def primary_address():
    """This machine's LAN address (no packet is sent), for the pairing hint."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('192.0.2.1', 9))
            return probe.getsockname()[0]
    except OSError:
        return socket.gethostname()


# --- Archives --------------------------------------------------------------------------

def running_archive():
    """The .pyz this code runs from, or None outside an archive."""
    loader = getattr(sys.modules['src.transcode_agent'], '__loader__', None)
    archive = getattr(loader, 'archive', None)
    return Path(archive) if archive else None


def inspect_archive(path, python, run):
    """{'agent_version', 'revision', 'sha256'} of a candidate archive, after proving it runs here."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as zipped:
            if zipped.testzip() is not None or '__main__.py' not in zipped.namelist():
                raise InstallError(f'{path} is damaged or not an agent archive')
            record = json.loads(zipped.read('src/transcode_agent/' + BUILD_RESOURCE))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        raise InstallError(f'{path} is not a readable agent archive (adm server agent pack builds one)') from None
    version = run([python, '-I', path, 'server', 'agent', '--version'], check=False)
    if version.returncode or f"version {record.get('agent_version')} " not in version.stdout:
        raise InstallError(f'{path} does not run on this machine: {(version.stderr or version.stdout).strip()[:200]}')
    return {'agent_version': record['agent_version'], 'revision': record.get('revision', 'unknown'), 'sha256': sha256(path)}


def install_archive(layout, source, *, keep_previous=True):
    """Put source in place atomically; the old archive becomes the previous one."""
    current, previous = layout.host(layout.archive), layout.host(layout.previous)
    current.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(current.parent, 0o755)
    fd, staged = tempfile.mkstemp(prefix='.' + ARCHIVE + '-', dir=current.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, staged)
        os.chmod(staged, 0o755)
        if keep_previous and current.exists():
            atomic_write(previous, current.read_bytes(), 0o755)
        os.replace(staged, current)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


# --- State -----------------------------------------------------------------------------

def load_state(layout):
    try:
        state = json.loads(layout.host(layout.state_file).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise InstallError(f'{layout.state_file} is unreadable; fix or remove it before continuing') from None
    if state.get('scope') != layout.scope or state.get('owner_uid') != layout.uid:
        raise InstallError(f"{layout.state_file} belongs to {state.get('owner', '?')} ({state.get('scope', '?')} scope); "
                           f'run this command as that account instead')
    return state


def save_state(layout, state):
    atomic_write(layout.host(layout.state_file), json.dumps(state, indent=1, sort_keys=True) + '\n', 0o644)


def other_scope_port(layout):
    """The system agent's port, when a user install could collide with it."""
    if not layout.user:
        return None
    try:
        return json.loads(layout.host(SYSTEM_STATE).read_text()).get('listen', '').rpartition(':')[2] or None
    except (OSError, ValueError):
        return None


# --- Checks ----------------------------------------------------------------------------

def ffmpeg_encoders(run):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        return None
    return run(['ffmpeg', '-hide_banner', '-encoders'], check=False).stdout


def check_dependencies(layout, run, *, ask, install_deps):
    """Python, systemd and ffmpeg. Root may install missing packages with apt-get when allowed."""
    missing = []
    if not shutil.which('systemctl'):
        missing.append('systemd (systemctl)')
    encoders = ffmpeg_encoders(run)
    if encoders is None or 'libopus' not in encoders or 'libx265' not in encoders:
        missing.append('ffmpeg and ffprobe with libx265 and libopus')
    if not missing:
        return encoders
    packages = 'python3 ffmpeg mesa-va-drivers'
    if not layout.user and shutil.which('apt-get') and 'systemd (systemctl)' not in missing:
        if install_deps or ask(f'Missing: {", ".join(missing)}. Install {packages} with apt-get? [y/N] ', 'n').lower() in ('y', 'yes'):
            with_retries(run, ['apt-get', 'update'])
            with_retries(run, ['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', *packages.split()])
            return check_dependencies(layout, run, ask=ask, install_deps=False)
    advice = (f'an administrator can install them, for example: sudo apt-get install {packages}' if layout.user
              else f'install them (Debian/Ubuntu: apt-get install {packages}) and run this again')
    raise InstallError(f'missing {", ".join(missing)}; {advice}')


def check_gpu(layout, values, encoders, run):
    """GPU access for the account that runs the service. Never changes groups for a user install."""
    wanted = values['TRANSCODE_ENCODERS'].split(',')
    if 'gpu' not in wanted:
        return []
    device = values['TRANSCODE_VAAPI_DEVICE']
    notes = []
    problem = None
    if 'hevc_vaapi' not in (encoders or ''):
        problem = 'ffmpeg here has no hevc_vaapi encoder'
    elif not os.path.exists(device):
        problem = f'{device} does not exist'
    elif layout.user and not os.access(device, os.R_OK | os.W_OK):
        try:
            group = grp.getgrgid(os.stat(device).st_gid).gr_name
        except KeyError:
            group = 'render'
        problem = (f'{layout.account} cannot open {device}; an administrator can run: sudo usermod -aG {group} '
                   f'{layout.account} (then log in again)')
    elif not layout.user:
        group = grp.getgrgid(os.stat(device).st_gid).gr_name
        run(['usermod', '-aG', group, SYSTEM_ACCOUNT])
        notes.append(f'{SYSTEM_ACCOUNT} was added to group {group} for {device}')
    if problem and wanted == ['gpu']:
        raise InstallError(problem + '; or choose CPU only')
    if problem:
        notes.append(problem + '; encoding will use the CPU')
    return notes


def check_user_manager(layout, run):
    if not layout.user:
        return
    if run(['systemctl', '--user', 'show-environment'], check=False).returncode:
        raise InstallError('no systemd user manager is reachable from this session. Log in as '
                           f'{layout.account} directly (a desktop or ssh session, not sudo or su) and run this again')


def linger_note(layout, run):
    if not layout.user:
        return None
    state = run(['loginctl', 'show-user', layout.account, '--property=Linger', '--value'], check=False).stdout.strip()
    if state == 'yes':
        return 'User lingering is on: the agent starts at boot and keeps running after you log out.'
    return ('User lingering is off: the agent runs only while you are logged in. To start it at boot and keep it '
            f'after logout, an administrator can run: sudo loginctl enable-linger {layout.account}')


# --- Service control -------------------------------------------------------------------

def restart_and_verify(layout, run, health, values, expected_version):
    run(layout.systemctl('daemon-reload'))
    run(layout.systemctl('enable', UNIT))
    run(layout.systemctl('restart', UNIT))
    if layout.user:
        fragment = run(layout.systemctl('show', UNIT, '--property=FragmentPath', '--value'), check=False).stdout.strip()
        if fragment and fragment != str(layout.unit_file):
            raise InstallError(f'the user manager loads {fragment}, not {layout.unit_file}; '
                               'its XDG_CONFIG_HOME differs from this shell\'s')
    answer = wait_healthy(health, values['TRANSCODE_LISTEN'], values['TRANSCODE_TOKEN'])
    if answer.get('agent_version') != expected_version:
        raise InstallError(f"the running agent reports version {answer.get('agent_version')}, expected {expected_version}")
    return answer


def write_unit(layout, python):
    atomic_write(layout.host(layout.unit_file), render_unit(template('transcode-agent.service'), layout, python), 0o644)


# --- Commands --------------------------------------------------------------------------

@dataclass
class Host:
    """Everything that reaches outside the process, replaceable in tests."""
    run: object = run_command
    health: object = http_health
    ask: object = None
    secret: object = None
    python: str = None

    def __post_init__(self):
        self.ask = self.ask or (lambda prompt, default: input(prompt).strip() or default)
        self.secret = self.secret or (lambda prompt: getpass.getpass(prompt).strip())
        self.python = self.python or shutil.which('python3') or sys.executable


def _source(source):
    source = Path(source) if source else running_archive()
    if source is None:
        raise InstallError('give the archive to install: --from adm-agent.pyz (build it with adm server agent pack)')
    if not source.is_file():
        raise InstallError(f'{source} does not exist')
    return source.resolve()


def _ask_values(layout, existing, options, host):
    """Settings: flags win, then the existing file (reruns keep everything), then questions or defaults."""
    values = {key: existing[key] for key in KEYS if existing.get(key)}
    flags = {'TRANSCODE_LISTEN': options.get('listen'), 'TRANSCODE_ENCODERS': options.get('encoders'),
             'TRANSCODE_VAAPI_DEVICE': options.get('vaapi_device'), 'TRANSCODE_TMP': options.get('tmp')}
    defaults = {**DEFAULTS, 'TRANSCODE_TMP': str(layout.default_tmp)}
    questions = {'TRANSCODE_LISTEN': 'Listen address and port', 'TRANSCODE_ENCODERS': 'Encoders: gpu,cpu (GPU, then CPU), gpu or cpu',
                 'TRANSCODE_VAAPI_DEVICE': 'GPU render device', 'TRANSCODE_TMP': 'Temporary folder (needs 1.5x a video free)'}
    for key, question in questions.items():
        if flags[key]:
            values[key] = flags[key]
        elif key not in values:
            if key == 'TRANSCODE_VAAPI_DEVICE' and 'gpu' not in values.get('TRANSCODE_ENCODERS', ''):
                values[key] = defaults[key]
                continue
            values[key] = defaults[key] if options.get('yes') else host.ask(f'{question} [{defaults[key]}]: ', defaults[key])
    if options.get('token') is not None:
        values['TRANSCODE_TOKEN'] = options['token']
    elif not values.get('TRANSCODE_TOKEN'):
        typed = '' if options.get('yes') else host.secret(
            'Token from AniKeepOrNot (paste it, or press Enter to generate one): ')
        values['TRANSCODE_TOKEN'] = typed or secrets.token_hex(32)
    from src.transcode_agent.cli import settings
    try:
        settings(values)
    except ValueError as exc:
        raise InstallError(str(exc)) from None
    if len(values['TRANSCODE_TOKEN']) < 16:
        raise InstallError('the token must be at least 16 characters')
    return values


def install(options, layout, host=None, out=print):
    host = host or Host()
    out(layout.announcement())
    out(f'  archive:  {layout.archive}\n  settings: {layout.env_file}\n  service:  {layout.unit_file}')
    state = load_state(layout)
    source = _source(options.get('source'))
    check_user_manager(layout, host.run)
    encoders = check_dependencies(layout, host.run, ask=host.ask, install_deps=options.get('install_deps'))
    candidate = inspect_archive(source, host.python, host.run)
    existing = read_env(layout.host(layout.env_file))
    values = _ask_values(layout, existing, options, host)
    port = values['TRANSCODE_LISTEN'].rpartition(':')[2]
    if other_scope_port(layout) == port:
        raise InstallError(f'the system transcode agent already uses port {port}; choose another with --listen')
    if not layout.user:
        try:
            pwd.getpwnam(SYSTEM_ACCOUNT)
        except KeyError:
            host.run(['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', '/nonexistent',
                      '--shell', '/usr/sbin/nologin', SYSTEM_ACCOUNT])
            out(f'Created the service account {SYSTEM_ACCOUNT}.')
    for note in check_gpu(layout, values, encoders, host.run):
        out('Note: ' + note)
    previous = state.get('installed') if state else None
    install_archive(layout, source, keep_previous=bool(previous))
    atomic_write(layout.host(layout.env_file), render_env(values, template('transcode-agent.env.example')), 0o600)
    write_unit(layout, host.python)
    try:
        answer = restart_and_verify(layout, host.run, host.health, values, candidate['agent_version'])
    except InstallError:
        if previous and layout.host(layout.previous).exists():
            _restore(layout, host, values, previous, out)
        raise
    save_state(layout, {'scope': layout.scope, 'owner': layout.account, 'owner_uid': layout.uid,
                        'runtime_account': layout.runtime_account, 'archive': str(layout.archive),
                        'env_file': str(layout.env_file), 'unit_file': str(layout.unit_file),
                        'listen': values['TRANSCODE_LISTEN'], 'installed': candidate,
                        'previous': previous, 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    out(f"Installed: transcode agent version {candidate['agent_version']} (build {candidate['revision'][:12]}), "
        f"encoders {','.join(answer.get('encoders', []))}.")
    note = linger_note(layout, host.run)
    if note:
        out(note)
    out(pairing(layout, values))
    return 0


def pairing(layout, values):
    control = 'systemctl --user' if layout.user else 'sudo systemctl'
    logs = 'journalctl --user -u transcode-agent' if layout.user else 'sudo journalctl -u transcode-agent'
    command = f"{'sudo ' if not layout.user else ''}{layout.archive} server agent token"
    return (f"\nPair it in AniKeepOrNot: Settings > Transcode > Remote\n"
            f"  Address: {primary_address()}:{values['TRANSCODE_LISTEN'].rpartition(':')[2]}\n"
            f"  Token:   shown by: {command}\n"
            f"           (the dashboard token and TRANSCODE_TOKEN here must be the same)\n"
            f"Service:  {control} status transcode-agent   Logs: {logs}\n"
            f"The port must be reachable from AniKeepOrNot; keep it on a trusted network (tokens travel over plain HTTP).")


def _restore(layout, host, values, previous, out):
    """Put the previous archive back after a failed start; report what happened."""
    os.replace(layout.host(layout.previous), layout.host(layout.archive))
    try:
        restart_and_verify(layout, host.run, host.health, values, previous['agent_version'])
        out(f"The new version did not start; version {previous['agent_version']} was restored and is running.")
    except InstallError as exc:
        out(f"The new version did not start, and restoring version {previous['agent_version']} also failed: {exc}")


def _installed(layout):
    state = load_state(layout)
    if not state:
        other = 'sudo ' if layout.user else 'as the user who installed it, '
        raise InstallError(f'no {layout.scope} agent is installed for {layout.account} '
                           f'(a {"system" if layout.user else "user"} install is managed {other.strip()})')
    return state


def upgrade(options, layout, host=None, out=print):
    host = host or Host()
    out(layout.announcement().replace('Installing', 'Upgrading'))
    state = _installed(layout)
    source = _source(options.get('source'))
    candidate = inspect_archive(source, host.python, host.run)       # Before anything stops.
    values = read_env(layout.host(layout.env_file))
    try:
        busy = host.health(values['TRANSCODE_LISTEN'], values['TRANSCODE_TOKEN']).get('busy')
    except (OSError, ValueError, KeyError):
        busy = False                                                  # Not running: nothing to interrupt.
    if busy and not options.get('interrupt'):
        raise InstallError('the agent is encoding right now; run again when it is idle, or add --interrupt '
                           '(AniKeepOrNot retries the video within its attempt budget)')
    install_archive(layout, source)
    write_unit(layout, host.python)
    try:
        restart_and_verify(layout, host.run, host.health, values, candidate['agent_version'])
    except InstallError as exc:
        out(f'Upgrade failed: {exc}')
        _restore(layout, host, values, state['installed'], out)
        raise InstallError('upgrade failed; see above') from None
    save_state(layout, {**state, 'installed': candidate, 'previous': state['installed'],
                        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    out(f"Upgraded to version {candidate['agent_version']} (build {candidate['revision'][:12]}); "
        f"version {state['installed']['agent_version']} kept for rollback.")
    return 0


def rollback(options, layout, host=None, out=print):
    host = host or Host()
    out(layout.announcement().replace('Installing', 'Rolling back'))
    state = _installed(layout)
    if not state.get('previous') or not layout.host(layout.previous).exists():
        raise InstallError('there is no previous version to roll back to')
    values = read_env(layout.host(layout.env_file))
    current, previous = layout.host(layout.archive), layout.host(layout.previous)
    swap = current.with_name('.' + ARCHIVE + '.swap')
    os.replace(current, swap)
    os.replace(previous, current)
    os.replace(swap, previous)
    try:
        restart_and_verify(layout, host.run, host.health, values, state['previous']['agent_version'])
    except InstallError:
        os.replace(current, swap); os.replace(previous, current); os.replace(swap, previous)
        restart_and_verify(layout, host.run, host.health, values, state['installed']['agent_version'])
        raise InstallError('the previous version did not start; the current version was restored') from None
    save_state(layout, {**state, 'installed': state['previous'], 'previous': state['installed'],
                        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    out(f"Rolled back to version {state['previous']['agent_version']}; "
        f"version {state['installed']['agent_version']} kept as the previous one.")
    return 0


def status(options, layout, host=None, out=print):
    host = host or Host()
    state = _installed(layout)
    values = read_env(layout.host(layout.env_file))
    active = host.run(layout.systemctl('is-active', UNIT), check=False).stdout.strip() or 'unknown'
    out(f"{layout.scope} agent of {state['owner']}: service {active}, installed version "
        f"{state['installed']['agent_version']} (build {state['installed']['revision'][:12]})")
    try:
        answer = host.health(values['TRANSCODE_LISTEN'], values['TRANSCODE_TOKEN'])
        out(f"health: {'busy' if answer.get('busy') else 'idle'}, encoders {','.join(answer.get('encoders', []))}, "
            f"version {answer.get('agent_version', 'none')}")
    except (OSError, ValueError, KeyError) as exc:
        out(f'health: no answer ({getattr(exc, "reason", exc)})')
        return 1
    return 0


def token(options, layout, host=None, out=print):
    """The explicit pairing step: show the token, or replace it (--set reads it hidden)."""
    host = host or Host()
    state = _installed(layout)
    values = read_env(layout.host(layout.env_file))
    if options.get('set'):
        new = host.secret('New token (paste the one from AniKeepOrNot): ')
        if len(new) < 16:
            raise InstallError('the token must be at least 16 characters')
        values['TRANSCODE_TOKEN'] = new
        atomic_write(layout.host(layout.env_file), render_env(values, template('transcode-agent.env.example')), 0o600)
        restart_and_verify(layout, host.run, host.health, values, state['installed']['agent_version'])
        out('Token replaced and the agent restarted.')
        return 0
    out(f"TRANSCODE_TOKEN={values.get('TRANSCODE_TOKEN', '')}")
    out('Save the same token for this agent in AniKeepOrNot: Settings > Transcode > Remote.')
    return 0


COMMANDS = {'install': install, 'upgrade': upgrade, 'rollback': rollback, 'status': status, 'token': token}


def main(command, options):
    """Entry from cli.py: returns an exit status; errors are one readable line."""
    try:
        layout = detect()
        return COMMANDS[command](options, layout)
    except InstallError as exc:
        print(f'{command} failed: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f'{command} interrupted; the installed agent was not replaced unless reported above.', file=sys.stderr)
        return 130
