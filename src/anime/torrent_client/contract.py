"""What every torrent client adapter provides, whatever the client version."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ClientFile:
    """One file of a torrent, the same shape for every client version."""
    index: int          # the client's file index
    path: str           # absolute: the torrent's save path + the file's path in the torrent
    size: int           # bytes
    progress: float     # 0.0 to 1.0
    selected: bool      # the client downloads it (priority above 0)


@dataclass(frozen=True)
class TorrentChange:
    """What the client reports about one torrent in an incremental update.

    Fields the client did not send in this update are None (unchanged).
    """
    state: str | None = None
    progress: float | None = None
    downloaded: int | None = None
    size: int | None = None
    save_path: str | None = None
    category: str | None = None
    tags: str | None = None


@dataclass(frozen=True)
class Changes:
    """One incremental update for all torrents: what changed since `rid`."""
    rid: int                                  # pass it back to get only later changes
    full: bool                                # the client sent everything (not a delta)
    torrents: dict                            # info hash -> TorrentChange
    removed: tuple                            # info hashes no longer in the client


class VersionAdapter:
    """The version-specific part of a client's API. No transport, no policy.

    Subclasses state which client versions they cover and how those versions
    spell each operation.
    """
    name = ''
    covers = ''                               # human-readable version range
    metadata_strategy = 'guarded_export'      # 'fetch' when metadata-only fetch exists
    stopped_parameter = 'stopped'             # torrents/add field for "add stopped"
    start_endpoint = 'torrents/start'
    stop_endpoint = 'torrents/stop'

    def __init__(self, major, api_version):
        self.major, self.api_version = major, api_version

    def add_fields(self, stopped):
        return {self.stopped_parameter: 'true' if stopped else 'false',
                'autoTMM': 'false', 'contentLayout': 'Original', 'useDownloadPath': 'false'}

    def label_fields(self, marker):
        return {'tags': 'ad-' + marker}

    def __repr__(self):
        return f'{type(self).__name__}({self.major}, {self.api_version})'
