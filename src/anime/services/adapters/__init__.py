"""Built-in service adapters. To add a service, write an adapter and list it here."""
from src.anime.services.adapters.transcode import TranscodeAdapter
from src.anime.services.adapters.arr import ProwlarrAdapter, SonarrAdapter
from src.anime.services.adapters.media_servers import JellyfinAdapter, ShokoAdapter
from src.anime.services.adapters.metadata import AnidbAdapter, AnimeListsAdapter, TvdbAdapter
from src.anime.services.adapters.qbittorrent import QbittorrentAdapter

BUILTIN = (
    QbittorrentAdapter, ProwlarrAdapter, SonarrAdapter, JellyfinAdapter, ShokoAdapter,
    AnidbAdapter, AnimeListsAdapter, TvdbAdapter, TranscodeAdapter,
)


def builtin_adapters():
    return tuple(adapter() for adapter in BUILTIN)
