"""Reject, as series work: remove every AKON torrent with its files, and the files staging wrote.

Only what AKON made is removed: torrents with its marker (remove_series_torrent
checks), and the staged files of each staging plan whose size is unchanged.
qBittorrent down is a wait; the series is REJECTED when nothing is left.
"""
import os

from src.anime.acquisition.cleanup import directory, parent
from src.anime.acquisition.submission import ClientUnavailable
from src.anime.async_io import blocking
from src.anime.series_job.steps.common import SERVICE_WAIT_MS, StepResult
from src.anime.series_job.store import note
from src.anime.series_job.torrents import forget_metainfo


def remove_staged(configuration, torrent):
    root = configuration.value("storage.user_evaluation_root")
    for plan in (torrent.get("staging") or {}).get("plans", {}).values():
        for entry in plan["entries"]:
            with directory(root) as base, parent(base, entry["path"]) as (folder, name):
                try:
                    if entry["role"] != "series" and os.stat(name, dir_fd=folder, follow_symlinks=False).st_size == entry["size"]:
                        os.unlink(name, dir_fd=folder)
                except FileNotFoundError:
                    pass


async def run_reject(series, context):
    for torrent in series.torrents:
        qb = torrent.get("qb") or {}
        if qb.get("added") and not torrent.get("removed"):
            def remove():
                prefix = qb.get("prefix", "akon")
                client = (context.torrents.client(context.configuration, qb["marker"], prefix=prefix)
                          if prefix != "akon" else context.torrents.client(context.configuration, qb["marker"]))
                return client.remove_series_torrent(torrent["infohash"], qb["marker"])
            try:
                await blocking(remove)
            except ClientUnavailable:
                return StepResult.wait(context.now + SERVICE_WAIT_MS, why="SERVICE_UNAVAILABLE")
            torrent["removed"] = True
        await blocking(remove_staged, context.configuration, torrent)
        forget_metainfo(context.configuration, torrent["infohash"])
        if torrent.get("state") != "dropped":
            torrent["state"], torrent["why"] = "dropped", "series rejected"
    for data in series.episodes.values():
        data["state"] = "dropped"
    note(series, f"series {series.anidb_id} {series.title} · rejected; its torrents and staged files are removed")
    return StepResult.done(next_step="done")
