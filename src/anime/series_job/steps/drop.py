"""Drop a torrent: remove it from qBittorrent with its files, unreserve its episodes, record why.

Only a torrent AKON added is removed (its marker tag and folder; delete_owned).
If qBittorrent can't be reached the torrent is still marked dropped and its
episodes freed; "cleanup" in its entry says the files are still to be removed,
and a later drop or Retry removes them.
"""
import logging

from src.anime.acquisition.submission import ClientUnavailable
from src.anime.series_job.store import SeriesRow, note
from src.anime.series_job.torrents import forget_metainfo

logger = logging.getLogger("akon.series")


async def drop_torrent(series: SeriesRow, torrent: dict, why: str, context) -> None:
    from src.anime.async_io import blocking
    infohash = torrent.get("infohash", "")
    release_name = torrent.get("release_name", "")
    ep_label = ", ".join(torrent.get("episodes", [])) or "release"

    qb = torrent.get("qb") or {}
    if qb.get("added"):
        def remove():
            return context.torrents.client(context.configuration, qb["marker"]).remove_series_torrent(
                infohash, qb["marker"])
        try:
            found = await blocking(remove)
        except ClientUnavailable:
            torrent["cleanup"] = "qBittorrent was unreachable; the torrent and its files are still there"
        else:
            torrent.pop("cleanup", None)
            if found in ("foreign", "moved"):
                note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · "
                             f"left in qBittorrent: it is {found} (not AKON's, or moved)")
    forget_metainfo(context.configuration, infohash)

    for ep_code in [code for code, data in series.episodes.items()
                    if data.get("torrent") == infohash and data.get("state") not in ("staged", "kept")]:
        del series.episodes[ep_code]

    torrent["state"] = "dropped"
    torrent["why"] = why
    note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · dropped: {why}")
