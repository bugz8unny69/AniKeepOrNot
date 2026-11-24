"""SJ-4g: Drop a torrent (remove from qB, delete files, unreserve episodes, record why)."""
import logging
from src.anime.series_job.store import SeriesRow, note

logger = logging.getLogger("akon.series")


async def drop_torrent(series: SeriesRow, torrent: dict, why: str, context) -> None:
    """Drop torrent, remove from client with files deleted, unreserve non-staged episodes."""
    infohash = torrent.get("infohash", "")
    release_name = torrent.get("release_name", "")
    ep_label = ", ".join(torrent.get("episodes", [])) or "release"

    # 1. Remove from download client and delete files if submitted
    client = getattr(context, "qb_client", None)
    if client is not None and torrent.get("submitted_at_ms") is not None:
        try:
            if hasattr(client, "delete"):
                await client.delete(infohash, delete_files=True)
            elif hasattr(client, "delete_owned"):
                await client.delete_owned(infohash)
        except Exception as exc:
            logger.warning("Could not delete dropped torrent %s from client: %s", infohash, exc)

    # 2. Unreserve non-staged episodes reserved by this torrent
    unreserved = []
    to_delete = []
    for ep_code, ep_data in series.episodes.items():
        if ep_data.get("torrent") == infohash and ep_data.get("state") not in ("staged", "kept"):
            to_delete.append(ep_code)
            unreserved.append(ep_code)

    for ep_code in to_delete:
        del series.episodes[ep_code]

    # 3. Mark torrent dropped
    torrent["state"] = "dropped"
    torrent["why"] = why

    note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · dropped: {why}")
