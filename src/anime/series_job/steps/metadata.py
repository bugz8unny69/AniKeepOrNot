"""First step: get the torrent's metadata (its file list), for every qBittorrent version.

The client's version adapter decides how:

- qBittorrent 5.2+ (metadata_strategy 'fetch'): qBittorrent fetches the
  metadata without adding the torrent. The torrent is added later, by the
  download step, from the saved .torrent bytes.
- qBittorrent 4.5-5.1 ('guarded_export'): the magnet is added paused/stopped,
  tagged with this torrent's marker, in its own folder. It is resumed only
  until qBittorrent has the file list, stopped at once, and exported. The
  download step then selects files in that same torrent and starts it.

The add is recorded in the series row before it is sent (context.checkpoint),
so an add whose outcome is unknown is never sent again. The .torrent bytes go
in a file beside the database (series_job/torrents.py), never in the row.
"""
from src.anime.acquisition.metadata_service import METADATA_TIMEOUT_MS, STOPPED
from src.anime.series_job.steps.common import StepResult, with_client
from src.anime.series_job.store import SeriesRow, note
from src.anime.series_job.torrents import magnet, marker, read_metainfo, save_metainfo

POLL_MS = 3000          # While qBittorrent looks for the file list.
FETCH_POLL_MS = 5000


async def get_metadata(series: SeriesRow, torrent: dict, context) -> StepResult:
    configuration, now = context.configuration, context.now
    info_hash = torrent["infohash"]
    label = f"series {series.anidb_id} {series.title} · {torrent.get('release_name', '')}"
    if read_metainfo(configuration, info_hash) is not None:
        return StepResult.done(next_step="check_release")
    qb = torrent.setdefault("qb", {})
    marker(torrent)
    since = qb.setdefault("metadata_since_ms", now)
    if now - since >= METADATA_TIMEOUT_MS:
        note(series, f"{label} · dropped: qBittorrent found no file list in {METADATA_TIMEOUT_MS // 60000} minutes")
        return StepResult.drop("TORRENT_METADATA_TIMEOUT")

    strategy = await with_client(context, torrent, lambda client: client.metadata_strategy)
    if isinstance(strategy, StepResult):
        return strategy
    if strategy == "fetch":
        def fetch(client):
            found = client.fetch_metadata(magnet(torrent), info_hash, batch=True)
            if found is None:
                return StepResult.wait(now + FETCH_POLL_MS, why="TORRENT_METADATA_WAITING")
            save_metainfo(configuration, info_hash, found[1])
            return StepResult.done(next_step="check_release")
        return await with_client(context, torrent, fetch)

    if qb.get("added") is None:
        qb["added"] = "sent"                      # Recorded before the send: never sent twice.
        context.checkpoint()
        result = await with_client(context, torrent, lambda client: client.add_series_torrent(
            info_hash, qb["marker"], magnet=magnet(torrent)))
        if isinstance(result, StepResult):
            return result
        note(series, f"{label} · added to qBittorrent stopped, to read its file list")
        return StepResult.wait(now + POLL_MS, why="TORRENT_METADATA_WAITING")

    def look(client):
        observed = client.series_torrent(info_hash, qb["marker"])
        if observed is None:
            # The add was sent but qBittorrent doesn't have it (yet). Never sent again;
            # the deadline above ends the wait.
            return StepResult.wait(now + POLL_MS, why="TORRENT_METADATA_WAITING")
        if observed.files:
            if observed.state not in STOPPED:
                client.stop_series_torrent(info_hash, qb["marker"])   # Before any video data comes in.
                return StepResult.wait(now + 1000, why="TORRENT_METADATA_WAITING")
            _, raw = client.export_metainfo(info_hash, batch=True)
            save_metainfo(configuration, info_hash, raw)
            return StepResult.done(next_step="check_release")
        if observed.state in STOPPED:
            client.start_series_torrent(info_hash, qb["marker"])      # Only to find the file list.
        return StepResult.wait(now + POLL_MS, why="TORRENT_METADATA_WAITING")
    return await with_client(context, torrent, look)
