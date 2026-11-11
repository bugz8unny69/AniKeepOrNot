"""The visible product name changes without renaming runtime identities."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

from src.anime import APP_NAME
from src.anime.providers.http import Clients, Options
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[3]


def test_visible_product_name_is_consistent():
    files = [ROOT/'README.md',ROOT/'PROJECT_STATUS.md',ROOT/'development.md',
             ROOT/'src/anime/reasons.py',ROOT/'src/anime/api/adm.py',
             *list((ROOT/'src/anime/dashboard/static').glob('*')),
             *list((ROOT/'src/anime').rglob('README.md')),
             ROOT/'src/anime/KEEP_SONARR.md',ROOT/'src/anime/SERVICE_SETUP.md',ROOT/'docs/TRANSCODE_AGENT.md']
    assert APP_NAME == 'AniKeepOrNot'
    old = re.compile(r'Anime [Dd]iscovery')
    assert not [str(path.relative_to(ROOT)) for path in files if path.is_file() and old.search(path.read_text())]


def test_anidb_client_identity_remains_configured(monkeypatch):
    options = Options('registered-client',42,30)
    client = Clients('unused.sqlite',options)
    calls = []
    def public(provider, key, url, **kwargs):
        calls.append(url)
        return b'<anime id="123"><titles><title type="main">Test</title></titles></anime>'
    monkeypatch.setattr(client, '_public', public)
    client.anime(123)
    query = parse_qs(urlsplit(calls[0]).query)
    assert query['client'] == ['registered-client']
    assert query['clientver'] == ['42']


def test_installer_stays_standalone_and_uses_product_name(tmp_path):
    installer=tmp_path/'anime-discovery-install'
    shutil.copyfile(ROOT/'bin/anime-discovery-install',installer)
    result=subprocess.run([sys.executable,str(installer),'--help'],cwd=tmp_path,
                          capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
    assert APP_NAME in result.stdout
    assert 'anime-discovery' in result.stdout  # Internal paths/command names stay stable.
