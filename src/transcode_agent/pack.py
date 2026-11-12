"""`adm agent pack`: build adm-agent.pyz with the standard library's zipapp.

Only the files listed in ARCHIVE_FILES go in: the agent modules unchanged, the
maintained entry point and the service/environment templates, plus build.json
recording the agent version, source revision and each file's SHA-256. Nothing
else from the checkout or installation is read, so credentials, settings,
databases, media, .git and tests can never be packed.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import zipapp

from src.transcode_agent import AGENT_VERSION, BUILD_RESOURCE, build_info

ROOT = Path(__file__).resolve().parents[2]
INTERPRETER = '/usr/bin/env python3'
# archive path: path in the source tree (relative to ROOT)
ARCHIVE_FILES = {
    '__main__.py': 'src/transcode_agent/archive_main.py',
    'src/__init__.py': None,                                   # Empty: makes src a package inside the zip.
    'src/transcode_agent/__init__.py': 'src/transcode_agent/__init__.py',
    'src/transcode_agent/cli.py': 'src/transcode_agent/cli.py',
    'src/transcode_agent/encode.py': 'src/transcode_agent/encode.py',
    'src/transcode_agent/server.py': 'src/transcode_agent/server.py',
    'src/transcode_agent/install.py': 'src/transcode_agent/install.py',
    'src/transcode_agent/templates/transcode-agent.service': 'deploy/transcode-agent/transcode-agent.service',
    'src/transcode_agent/templates/transcode-agent.env.example': 'deploy/transcode-agent/transcode-agent.env.example',
}


class PackError(RuntimeError):
    pass


def source_revision(root=ROOT):
    """The revision being packed; '+modified' when packed agent files differ from it in Git."""
    revision = build_info()['revision']
    if (root / '.git').exists():
        paths = sorted({source for source in ARCHIVE_FILES.values() if source})
        try:
            changed = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--', *paths],
                                     capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
            if changed.returncode == 0 and changed.stdout.strip():
                revision += '+modified'
        except (OSError, subprocess.TimeoutExpired):
            pass
    return revision


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def pack(output, *, root=ROOT, now=None):
    """Write the archive atomically to output (and output.sha256). Returns the build record."""
    if not (root / 'src/transcode_agent/cli.py').is_file():
        raise PackError('pack runs from an AniKeepOrNot installation or checkout; '
                        'an agent archive cannot repack itself')
    output = Path(output).resolve()
    if output.is_dir():
        output = output / 'adm-agent.pyz'
    if not output.parent.is_dir():
        raise PackError(f'folder does not exist: {output.parent}')
    record = {'agent_version': AGENT_VERSION, 'revision': source_revision(root),
              'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'files': {}}
    with tempfile.TemporaryDirectory(prefix='adm-agent-') as temp:
        staging = Path(temp) / 'archive'
        for target, source in ARCHIVE_FILES.items():
            destination = staging / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source is None:
                destination.write_bytes(b'')
            else:
                if not (root / source).is_file():
                    raise PackError(f'missing source file: {source}')
                shutil.copyfile(root / source, destination)
            record['files'][target] = _sha256(destination)
        (staging / 'src/transcode_agent' / BUILD_RESOURCE).write_text(json.dumps(record, indent=1, sort_keys=True) + '\n')
        partial = Path(temp) / 'adm-agent.pyz'
        zipapp.create_archive(staging, partial, interpreter=INTERPRETER, compressed=True)
        digest = _sha256(partial)
        fd, name = tempfile.mkstemp(prefix='.' + output.name + '-', dir=output.parent)
        os.close(fd)
        try:
            shutil.copyfile(partial, name)
            os.chmod(name, 0o755)
            os.replace(name, output)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    Path(str(output) + '.sha256').write_text(f'{digest}  {output.name}\n')
    return {**record, 'path': str(output), 'sha256': digest}
