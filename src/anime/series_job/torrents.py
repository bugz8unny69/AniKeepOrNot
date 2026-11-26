"""qBittorrent for the series job: one client per torrent, its own folder and marker.

Every AKON torrent lives in <download root>/ad-<marker> and carries the tag
ad-<marker>, as before. The marker is kept in the torrent's entry in the series
row (torrent["qb"]["marker"]), so ownership survives restarts. The client's
version adapter picks how each call is spelled (qBittorrent 4: pause/resume,
"paused" on add; 5: stop/start; 5.2+: metadata-only fetch).

Metadata (the .torrent bytes) is kept in a file beside the database, not in
the row: <database>-files/metainfo/<info hash>.torrent.
"""
from pathlib import Path, PurePosixPath
import json
import os
import uuid

from src.anime.acquisition.metadata import hash_magnet
from src.anime.acquisition.settings import DownloadOptions, download_options
from src.anime.torrent_client.qbittorrent import Client


def marker(torrent):
    """The torrent's marker, created once."""
    qb = torrent.setdefault("qb", {})
    if "marker" not in qb:
        qb["marker"] = uuid.uuid4().hex
    return qb["marker"]


def options_for(configuration, torrent_marker):
    base = download_options(configuration)
    return DownloadOptions(base.base_url, str(PurePosixPath(base.download_root) / ("ad-" + torrent_marker)),
                           base.category, base.timeout_seconds)


def magnet(torrent):
    """The release's sanitized magnet (trackers kept), or the bare info hash as a magnet."""
    raw = torrent.get("raw_body") or ""
    declared = None
    if raw:
        try:
            declared = json.loads(raw).get("magnet_uri")
        except ValueError:
            declared = None
    return hash_magnet(torrent["infohash"], declared) or hash_magnet(torrent["infohash"])


def metainfo_path(configuration, info_hash):
    database = Path(configuration.value("storage.database_path"))
    return database.with_name(database.stem + "-files") / "metainfo" / f"{info_hash}.torrent"


def save_metainfo(configuration, info_hash, raw):
    path = metainfo_path(configuration, info_hash)
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(raw)
    os.replace(partial, path)


def read_metainfo(configuration, info_hash):
    """The saved .torrent bytes, or None."""
    try:
        return metainfo_path(configuration, info_hash).read_bytes()
    except FileNotFoundError:
        return None


def forget_metainfo(configuration, info_hash):
    metainfo_path(configuration, info_hash).unlink(missing_ok=True)


class Torrents:
    """Builds logged-in clients for the series steps (the worker gives it the secrets)."""

    def __init__(self, secrets, *, client_factory=Client):
        self.secrets, self.client_factory = secrets, client_factory

    def client(self, configuration, torrent_marker):
        client = self.client_factory(options_for(configuration, torrent_marker),
                                     self.secrets.qbittorrent_credentials())
        client.login()
        return client
