"""Transcode each checked file when it is bigger than the target (Settings: transcode).

The same transcoder as before (services/transcode.py: local or remote agents,
three tries in all). Its try count is saved in the row before each try, so
agent outages and restarts cannot reset it. The output goes to
<staging folder>/.transcode/ and staging moves it into place.
"""
import json
import logging
import os
import sqlite3
from pathlib import Path

from src.anime.acquisition.cleanup import directory, parent
from src.anime.acquisition.media import content_id, media_fingerprint, signature
from src.anime.async_io import blocking
from src.anime.storage_mounts import DOWNLOADS, STAGING, uses_storage
from src.anime.series_job.staging import write_torrent_key
from src.anime.series_job.steps.common import SERVICE_WAIT_MS, SETUP_WAIT_MS, StepResult
from src.anime.series_job.store import SeriesRow, note
from src.anime.series_job.torrents import options_for
from src.anime.services.transcode import (TranscodeFailed, TranscodeStorageUnavailable, TranscodeWaiting,
                                         transcode as run_transcode)
from src.anime.staging import StagingReview
from src.transcode_agent.encode import EncodeError

WAIT_MS = 10_000
log = logging.getLogger(__name__)


def transcode_file(context, series, torrent, entry, client=None, share=(0, 1)):
    """Transcode one file; returns its evidence in the staging folder, or None (small enough).

    share=(i, n): this is the torrent's i-th of n files, for its transcode_progress."""
    configuration = context.configuration
    output_root = configuration.value("storage.user_evaluation_root")
    work = Path(output_root) / ".transcode"
    if work.is_symlink():
        raise StagingReview()
    work.mkdir(exist_ok=True, mode=0o700)
    output = work / f"{torrent['infohash']}-{entry['index']}.mkv"
    database = configuration.value("storage.database_path")

    def started():
        entry["transcode_attempts"] = entry.get("transcode_attempts", 0) + 1
        write_torrent_key(database, context.claim, series.torrents, torrent, "files")

    def progress(fraction):
        # Owner, 2026-11-28: the dashboard showed 100% (the download's) during the transcode.
        overall = round((share[0] + fraction) / share[1], 2)
        if overall == torrent.get("transcode_progress"):
            return
        torrent["transcode_progress"] = overall
        try:
            write_torrent_key(database, context.claim, series.torrents, torrent, "transcode_progress")
        except sqlite3.Error as exc:                     # Only the display: the encode goes on.
            log.info("series %s: transcode progress not saved (%s)", series.anidb_id, exc)

    qb = torrent["qb"]
    source_root = options_for(configuration, qb["marker"], prefix=qb.get("prefix", "akon")).download_root
    with directory(source_root) as root, parent(root, entry["path"]) as (folder, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder)
        try:
            before = signature(os.fstat(fd))
            recorded = entry["media"].get("content")
            if before[2] != entry["media"]["file"][2] or (recorded is not None and content_id(fd, before[2]) != recorded):
                raise StagingReview("The downloaded file changed after its media check")   # Size and content.
            source = Path(f"/proc/{os.getpid()}/fd/{fd}")     # The transcoder reads the pinned file.
            spent = entry.get("transcode_attempts", 0)
            try:
                if not (client or context.transcoder or run_transcode)(source, output, configuration, context.secrets,
                                                                       context.cancelled, started=started,
                                                                       attempts=spent, progress=progress):
                    return None
            except TranscodeStorageUnavailable:
                if entry.get("transcode_attempts", 0) != spent:   # Our share failed: the attempt is given back.
                    entry["transcode_attempts"] = spent
                    write_torrent_key(database, context.claim, series.torrents, torrent, "files")
                raise
            if signature(os.fstat(fd)) != before:
                raise StagingReview("Source changed during transcode")
        finally:
            os.close(fd)
    with output.open("rb") as result:
        stat = signature(os.fstat(result.fileno()))
        fingerprint = media_fingerprint(result.fileno(), stat[2], stat)
        content = content_id(result.fileno(), stat[2])
    with directory(output_root) as base:
        root_sig = [os.fstat(base).st_dev, os.fstat(base).st_ino]
    return {"path": ".transcode/" + output.name, "file": list(stat), "sha256": fingerprint, "content": content,
            "root": root_sig}


@uses_storage(STAGING, DOWNLOADS)       # Writes <staging>/.transcode from the download.
async def transcode(series: SeriesRow, torrent: dict, context) -> StepResult:
    if context.configuration.value("transcode.mode") == "off":
        return StepResult.done(next_step="stage")
    label = (f"series {series.anidb_id} {series.title} · {', '.join(torrent['episodes'])} "
             f"{torrent.get('release_name', '')}")
    if not context.configuration.value("storage.user_evaluation_root"):
        return StepResult.wait(context.now + SETUP_WAIT_MS, why="STAGING_CONFIGURATION_INVALID")
    torrent.setdefault("transcode_progress", 0.0)
    files = torrent["files"]
    for index, entry in enumerate(files):
        if "transcoded" in entry:
            continue
        try:
            entry["transcoded"] = await blocking(transcode_file, context, series, torrent, entry,
                                                 share=(index, len(files)))
        except TranscodeWaiting:
            return StepResult.wait(context.now + WAIT_MS, why="TRANSCODE_WAITING")
        except TranscodeStorageUnavailable as exc:
            # Our own share failed, not the transcoder: wait for it; no attempt is spent.
            return StepResult.wait(context.now + SERVICE_WAIT_MS, why=f"STORAGE_UNAVAILABLE: {exc}"[:200])
        except (TranscodeFailed, EncodeError) as exc:
            # Every transcoder failed: that may be the transcoders, not the video. Keep it; stop.
            return StepResult.stop(f"TRANSCODE_FAILED: {entry['episode']} could not be transcoded ({exc})"[:300])
        except StagingReview as exc:
            return StepResult.stop(f"STAGING_REVIEW: {exc}"[:300])
        note(series, f"{label} · {entry['episode']} "
                     + ("transcoded" if entry["transcoded"] else "kept as it is (already small enough)"))
        torrent["transcode_progress"] = round((index + 1) / len(files), 2)
    return StepResult.done(next_step="stage")
