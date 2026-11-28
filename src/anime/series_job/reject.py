"""Reject, as series work: remove every AKON torrent with its files, and the files staging wrote.

Only what AKON made is removed: torrents with its marker (remove_series_torrent
checks), and the files of each staging plan AKON recorded before writing them
(remove_staged).
qBittorrent down is a wait; the series is REJECTED when nothing is left.
"""
import os

from src.anime.acquisition.cleanup import directory, parent
from src.anime.acquisition.submission import ClientUnavailable
from src.anime.async_io import blocking
from src.anime.storage_mounts import DOWNLOADS, STAGING, uses_storage
from src.anime.series_job.steps.common import SERVICE_WAIT_MS, StepResult
from src.anime.series_job.store import note
from src.anime.series_job.torrents import forget_metainfo


def remove_staged(configuration, torrent, codes=None, *, series_nfo=True):
    """Remove the files AKON staged for this torrent; returns how many were left because they differ.

    Owner, 2026-11-27: a file is AKON's when its path is in a plan AKON recorded before
    writing it (planned or published: a copy interrupted after its rename is still ours)
    and it is the same file: a video by its size and content (content_id, never mtime),
    an NFO by its size (AKON writes them). Then the empty season and series folders go.
    codes: only those episodes (Keep removes what Sonarr imported); series_nfo: also
    tvshow.nfo (Reject; Keep removes it once no episode of the series is left).
    """
    from src.anime.acquisition.media import same_content
    root = configuration.value("storage.user_evaluation_root")
    kept, folders = 0, set()
    for code, plan in (torrent.get("staging") or {}).get("plans", {}).items():
        if codes is not None and code not in codes:
            continue
        for entry in plan["entries"]:
            if entry["role"] == "series" and not series_nfo:
                continue
            if entry["role"] == "media":
                ours = same_content(root, entry["path"], entry["size"], entry.get("content"))
            else:
                try:
                    with directory(root) as base, parent(base, entry["path"]) as (folder, name):
                        ours = os.stat(name, dir_fd=folder, follow_symlinks=False).st_size == entry["size"]
                except FileNotFoundError:
                    ours = None
            if ours is None:
                continue                                  # Already gone.
            if not ours:
                kept += 1                                 # Not the file AKON wrote: left alone.
                continue
            with directory(root) as base, parent(base, entry["path"]) as (folder, name):
                try:
                    os.unlink(name, dir_fd=folder)
                except FileNotFoundError:
                    pass
            parts = entry["path"].split("/")
            folders.update("/".join(parts[:n]) for n in range(1, len(parts)))
    for relative in sorted(folders, key=lambda f: -f.count("/")):      # Deepest first; only if empty.
        with directory(root) as base, parent(base, relative) as (folder, name):
            try:
                os.rmdir(name, dir_fd=folder)
            except OSError:
                pass                                      # Not empty (other episodes, your files) or gone.
    return kept


@uses_storage(STAGING, DOWNLOADS)     # A missing mount would look like files already gone.
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
        differ = await blocking(remove_staged, context.configuration, torrent)
        if differ:
            note(series, f"series {series.anidb_id} {series.title} · {differ} staged file(s) differ from what AKON "
                         "wrote, so they were left in the staging folder")
        forget_metainfo(context.configuration, torrent["infohash"])
        if torrent.get("state") != "dropped":
            torrent["state"], torrent["why"] = "dropped", "series rejected"
    for data in series.episodes.values():
        data["state"] = "dropped"
    note(series, f"series {series.anidb_id} {series.title} · rejected; its torrents and staged files are removed")
    return StepResult.done(next_step="done")
