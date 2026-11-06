"""Built-in service adapters. To add a service, write an adapter and list it here."""
from src.nextgen.services.adapters.arr import ProwlarrAdapter, SonarrAdapter
from src.nextgen.services.adapters.media_servers import JellyfinAdapter, ShokoAdapter
from src.nextgen.services.adapters.metadata import AnidbAdapter, AnimeListsAdapter, TvdbAdapter
from src.nextgen.services.adapters.qbittorrent import QbittorrentAdapter

BUILTIN = (
    QbittorrentAdapter, ProwlarrAdapter, SonarrAdapter, JellyfinAdapter, ShokoAdapter,
    AnidbAdapter, AnimeListsAdapter, TvdbAdapter,
)


def builtin_adapters():
    return tuple(adapter() for adapter in BUILTIN)
