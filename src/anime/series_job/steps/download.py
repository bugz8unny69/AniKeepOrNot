"""Download the chosen files of the torrent, and follow their progress.

- qBittorrent 5.2+: the torrent is not in qBittorrent yet (the metadata step
  only fetched its metadata). It is added stopped from the saved .torrent
  bytes; the add is recorded first, so it is never sent twice.
- qBittorrent 4.5-5.1: the metadata step added it stopped already.

Then: select only the chosen files (every other file is skipped), start it,
and check every POLL_MS until the chosen files are complete. A full download
disk waits and restarts the torrent once there is room (owner, 2026-11-26);
any other qBittorrent error drops the torrent with why.
"""
from src.anime.acquisition.metadata_service import STOPPED
from src.anime.series_job.steps.common import StepResult, with_client
from src.anime.series_job.store import SeriesRow, note
from src.anime.series_job.torrents import read_metainfo

POLL_MS = 5000
DISK_FULL_WAIT_MS = 10 * 60_000                  # A full download disk: check again in 10 minutes.
DISK_MARGIN_BYTES = 256 * 1024 * 1024            # Room needed beyond what the download still needs.


async def download(series: SeriesRow, torrent: dict, context) -> StepResult:
    now, info_hash = context.now, torrent["infohash"]
    qb = torrent["qb"]
    wanted = {f["index"] for f in torrent["files"]}
    label = (f"series {series.anidb_id} {series.title} · {', '.join(torrent['episodes'])} "
             f"{torrent.get('release_name', '')}")

    if qb.get("added") is None:
        raw = read_metainfo(context.configuration, info_hash)
        if raw is None:
            return StepResult.done(next_step="get_metadata")
        qb["added"] = "sent"                      # Recorded before the send: never sent twice.
        context.checkpoint()
        result = await with_client(context, torrent, lambda client: client.add_series_torrent(
            info_hash, qb["marker"], raw=raw))
        if isinstance(result, StepResult):
            return result
        return StepResult.wait(now + 1000, why="TORRENT_DOWNLOAD_WAITING")

    def work(client):
        observed = client.series_torrent(info_hash, qb["marker"])
        if observed is None:
            if qb.get("started"):
                note(series, f"{label} · dropped: the torrent is no longer in qBittorrent")
                return StepResult.drop("TORRENT_DOWNLOAD_WAITING")
            return StepResult.wait(now + POLL_MS, why="TORRENT_DOWNLOAD_WAITING")
        if not observed.files:
            return StepResult.wait(now + POLL_MS, why="TORRENT_METADATA_WAITING")
        if not qb.get("selected"):
            if observed.state not in STOPPED:
                client.stop_series_torrent(info_hash, qb["marker"])
                return StepResult.wait(now + 1000, why="TORRENT_SELECTION_UNCERTAIN")
            client.select_series_files(info_hash, qb["marker"], wanted)
            qb["selected"] = True
            return StepResult.wait(now + 1000, why="TORRENT_SELECTION_UNCERTAIN")
        files = {f.index: f for f in client.files(info_hash)}
        if {i for i, f in files.items() if f.selected} != wanted:
            note(series, f"{label} · dropped: its file selection was changed in qBittorrent")
            return StepResult.drop("TORRENT_TRANSFER_CONFLICT")
        size = sum(files[i].size for i in wanted)
        done = sum(files[i].size * files[i].progress for i in wanted)
        if all(files[i].progress == 1 for i in wanted):
            for entry in torrent["files"]:
                entry["local_path"] = files[entry["index"]].path
            torrent["progress"] = 1.0
            note(series, f"{label} · download finished")
            return StepResult.done(next_step="check_media")
        if observed.state == "error":
            free = client.free_space()
            if free < size - done + DISK_MARGIN_BYTES:
                if not qb.get("disk_full"):
                    note(series, f"{label} · waiting: the download disk has {free // 2**20} MB free; "
                                 f"{int(size - done) // 2**20} MB more are needed")
                qb["disk_full"] = True
                return StepResult.wait(now + DISK_FULL_WAIT_MS, why="TORRENT_DISK_FULL")
            if not qb.pop("disk_full", False):
                note(series, f"{label} · dropped: qBittorrent stopped it with an error")
                return StepResult.drop("TORRENT_TRANSFER_CONFLICT")
            client.start_series_torrent(info_hash, qb["marker"])           # Room again.
            note(series, f"{label} · the download disk has room again; restarted")
            return StepResult.wait(now + POLL_MS, why="TORRENT_DOWNLOAD_IN_PROGRESS")
        if observed.state in STOPPED and not qb.get("started"):
            client.start_series_torrent(info_hash, qb["marker"])
            qb["started"] = True
            for episode in torrent["episodes"]:
                series.episodes[episode]["state"] = "downloading"       # Intake shows it per episode.
            note(series, f"{label} · download started")
            return StepResult.wait(now + POLL_MS, why="TORRENT_DOWNLOAD_IN_PROGRESS")
        progress = done / size if size else 0.0
        if int(progress * 10) != int(torrent.get("progress", 0.0) * 10):
            note(series, f"{label} · downloading, {int(progress * 100)}%")      # Every 10%, not every check.
        torrent["progress"] = progress
        return StepResult.wait(now + POLL_MS, why="TORRENT_DOWNLOAD_IN_PROGRESS")
    return await with_client(context, torrent, work)
