"""Download the chosen files of the torrent, and follow their progress.

- qBittorrent 5.2+: the torrent is not in qBittorrent yet (the metadata step
  only fetched its metadata). It is added stopped from the saved .torrent
  bytes; the add is recorded first, so it is never sent twice.
- qBittorrent 4.5-5.1: the metadata step added it stopped already.

Then: select only the chosen files (every other file is skipped), start it,
and check every POLL_MS until the chosen files are complete. A full download
disk waits and restarts the torrent once there is room (owner, 2026-11-26);
any other qBittorrent error drops the torrent with why.

Staging full (owner, 2026-11-27): a torrent starts only when the staging folder has room
for its files, after everything already on its way there (started, not staged yet). Until
then it waits (STAGING_FULL) and nothing is downloaded, rather than finding at the end that
it cannot be staged. A started torrent goes on: its room was counted when it started.
"""
import json
import os

from src.anime.acquisition.metadata_service import STOPPED
from src.anime.series_job.steps.common import StepResult, with_client
from src.anime.series_job.store import SeriesRow, note
from src.anime.series_job.torrents import read_metainfo
from src.anime.storage_mounts import STAGING, uses_storage

POLL_MS = 5000
DISK_FULL_WAIT_MS = 10 * 60_000                  # A full download disk: check again in 10 minutes.
DISK_MARGIN_BYTES = 256 * 1024 * 1024            # Room needed beyond what the download still needs.
STAGING_FULL_WAIT_MS = 10 * 60_000               # Staging full: look again in 10 minutes.
STAGING_MARGIN_BYTES = 1024 * 1024 * 1024        # Kept free on staging beyond what is on its way.
ON_ITS_WAY = ("download", "check_media", "transcode", "stage")


def _unstaged_bytes(torrent, episodes):
    return sum(f.get("size", 0) for f in torrent.get("files", [])
               if (episodes.get(f.get("episode")) or {}).get("state") != "staged")


def staging_free(staging_root):
    """Free bytes on the staging share (in a worker thread: a hard NFS mount can hang)."""
    info = os.statvfs(staging_root)
    return info.f_bavail * info.f_frsize


def staging_room(db, torrent):
    """(needed, on its way): this torrent's files, and the files of every other started torrent
    not staged yet (transcoding can only make them smaller). A local query: the worker's thread."""
    on_its_way = 0
    for torrents_json, episodes_json in db.execute(
            "SELECT torrents_json, episodes_json FROM series WHERE state IN ('WORKING','ASLEEP')"):
        episodes = json.loads(episodes_json)
        for other in json.loads(torrents_json):
            if (other.get("infohash") != torrent["infohash"] and other.get("state") in ("working", "waiting")
                    and other.get("step") in ON_ITS_WAY and (other.get("qb") or {}).get("started")):
                on_its_way += _unstaged_bytes(other, episodes)
    return sum(f.get("size", 0) for f in torrent.get("files", [])), on_its_way


@uses_storage(STAGING)     # What can't be staged is not downloaded; a missing mount can't be measured.
async def download(series: SeriesRow, torrent: dict, context) -> StepResult:
    now, info_hash = context.now, torrent["infohash"]
    qb = torrent["qb"]
    wanted = {f["index"] for f in torrent["files"]}
    label = (f"series {series.anidb_id} {series.title} · {', '.join(torrent['episodes'])} "
             f"{torrent.get('release_name', '')}")
    staging_root = context.configuration.value("storage.user_evaluation_root")
    if not qb.get("started") and staging_root:
        from src.anime.async_io import blocking
        free = await blocking(staging_free, staging_root)
        needed, on_its_way = staging_room(context.db, torrent)
        if free - on_its_way - STAGING_MARGIN_BYTES < needed:
            if not str(torrent.get("why") or "").startswith("STAGING_FULL"):
                note(series, f"{label} · waiting: staging is full ({free // 2**20} MB free, "
                             f"{on_its_way // 2**20} MB already on its way, this needs {needed // 2**20} MB); "
                             "it starts once Keep or Reject frees room")
            torrent["why"] = "STAGING_FULL"
            return StepResult.wait(now + STAGING_FULL_WAIT_MS, why="STAGING_FULL")
        if torrent.get("why") == "STAGING_FULL":
            torrent["why"] = None

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
