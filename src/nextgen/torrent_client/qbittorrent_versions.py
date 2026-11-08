"""qBittorrent version adapters: 4.5 to 5.x, one per difference in the Web API.

| Adapter              | qBittorrent | Web API            | Differences                            |
|----------------------|-------------|--------------------|----------------------------------------|
| Qbittorrent4         | 4.5 - 4.6   | 2.8.14 - <2.11     | pause/resume; "paused" on add; export  |
| Qbittorrent5         | 5.0 - 5.1   | 2.11 - <2.11.9     | stop/start; "stopped" on add; export   |
| Qbittorrent5Fetch    | 5.2+        | 2.11.9+            | as 5, plus metadata-only fetch         |

The selection reproduces the earlier _Protocol rules exactly.
"""
from src.nextgen.acquisition.submission import ClientUnsupported
from src.nextgen.torrent_client.contract import VersionAdapter

STOP_API = (2, 11, 0)        # torrents/stop and torrents/start replace pause and resume
FETCH_API = (2, 11, 9)       # torrents/fetchMetadata (qBittorrent 5.2)


class Qbittorrent4(VersionAdapter):
    name, covers = 'qbittorrent-4', 'qBittorrent 4.5 - 4.6 (Web API 2.8.14 - 2.10)'
    stopped_parameter = 'paused'
    start_endpoint = 'torrents/resume'
    stop_endpoint = 'torrents/pause'


class Qbittorrent5(VersionAdapter):
    name, covers = 'qbittorrent-5', 'qBittorrent 5.0 - 5.1 (Web API 2.11 - 2.11.8)'


class Qbittorrent5Fetch(Qbittorrent5):
    name, covers = 'qbittorrent-5-fetch', 'qBittorrent 5.2+ (Web API 2.11.9+)'
    metadata_strategy = 'fetch'


def select_adapter(major, api_version):
    """The adapter for this client, or ClientUnsupported."""
    if (major not in (4, 5) or type(api_version) is not tuple
            or len(api_version) != 3 or not (2, 8, 14) <= api_version < (3, 0, 0)):
        raise ClientUnsupported()
    if api_version < STOP_API:
        return Qbittorrent4(major, api_version)
    if major == 5 and api_version >= FETCH_API:
        return Qbittorrent5Fetch(major, api_version)
    return Qbittorrent5(major, api_version)
