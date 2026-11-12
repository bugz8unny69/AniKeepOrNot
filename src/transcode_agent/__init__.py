"""Standalone transcode agent. Standard library + ffmpeg only; never imports src.anime.

AGENT_VERSION is the compatibility number between agents and workers. Bump it
only when a change requires deployed agents and workers to be updated
together (the HTTP contract or the encode contract). The worker runs only with
agents reporting exactly its own AGENT_VERSION. The build revision is separate
and only for diagnostics.
"""
import json
from pathlib import Path
import pkgutil
import subprocess

AGENT_VERSION = 1

# Written into the .pyz by `adm agent pack`; absent in a source checkout.
BUILD_RESOURCE = 'build.json'
_build = None


def build_info():
    """{'agent_version', 'revision'}: from the archive, else the installed release, else Git."""
    global _build
    if _build is not None:
        return _build
    revision = 'unknown'
    try:
        data = pkgutil.get_data(__name__, BUILD_RESOURCE)
    except OSError:
        data = None
    if data:
        try:
            revision = str(json.loads(data)['revision'])
        except (ValueError, KeyError, TypeError):
            revision = 'unknown'
    else:
        root = Path(__file__).resolve().parents[2]
        release = root / '.anime-discovery-release.json'
        try:
            revision = str(json.loads(release.read_text())['revision'])
        except (OSError, ValueError, KeyError, TypeError):
            if (root / '.git').exists():
                try:
                    found = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True,
                                           text=True, timeout=5, stdin=subprocess.DEVNULL)
                    if found.returncode == 0 and found.stdout.strip():
                        revision = found.stdout.strip()
                except (OSError, subprocess.TimeoutExpired):
                    pass
    _build = {'agent_version': AGENT_VERSION, 'revision': revision}
    return _build


def in_archive():
    """True when running from adm-agent.pyz (it carries build.json)."""
    try:
        return pkgutil.get_data(__name__, BUILD_RESOURCE) is not None
    except OSError:
        return False


def version_text():
    info = build_info()
    revision = info['revision']
    short = revision[:12] + ('+modified' if revision.endswith('+modified') else '')
    return f"transcode agent version {info['agent_version']} (build {short})"
