"""SJ-4c: Submit to qBittorrent and track download progress."""
import logging
import time
from src.anime.series_job.steps.common import StepResult
from src.anime.series_job.store import SeriesRow, note

logger = logging.getLogger("akon.series")


async def download(series: SeriesRow, torrent: dict, context) -> StepResult:
    """Submit torrent to download client, monitor progress, and complete when done."""
    now = getattr(context, "now", int(time.time() * 1000))
    infohash = torrent.get("infohash", "")
    release_name = torrent.get("release_name", "")
    ep_label = ", ".join(torrent.get("episodes", [])) or "release"
    client = getattr(context, "qb_client", None)

    # 1. Submit if not yet submitted
    if torrent.get("submitted_at_ms") is None:
        if client is not None and hasattr(client, "add"):
            try:
                await client.add(torrent)
            except Exception as exc:
                if exc.__class__.__name__ in ("ClientUnavailable", "ConnectionError", "TimeoutError"):
                    return StepResult.wait(now + 10000, why="CLIENT_UNAVAILABLE")
                raise

        torrent["submitted_at_ms"] = now
        torrent["progress"] = 0.0
        torrent["state"] = "working"
        for ep in torrent.get("episodes", []):
            if ep in series.episodes:
                series.episodes[ep]["state"] = "downloading"

        note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · download started")
        return StepResult.wait(now + 5000)

    # 2. Check download status
    status = None
    if client is not None and hasattr(client, "get_status"):
        try:
            status = await client.get_status(infohash)
        except Exception as exc:
            if exc.__class__.__name__ in ("ClientUnavailable", "ConnectionError", "TimeoutError"):
                return StepResult.wait(now + 10000, why="CLIENT_UNAVAILABLE")
            raise

    if status is None:
        progress = torrent.get("progress", 0.0)
        files = torrent.get("files", [])
    elif isinstance(status, dict):
        progress = status.get("progress", 0.0)
        files = status.get("files", [])
    else:
        progress = getattr(status, "progress", 0.0)
        files = getattr(status, "files", [])

    if progress < 1.0:
        old_pct = int(torrent.get("progress", 0.0) * 100)
        new_pct = int(progress * 100)
        torrent["progress"] = progress
        # Log progress only when it changes
        if new_pct != old_pct:
            note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · downloading, {new_pct}%")
        return StepResult.wait(now + 5000)

    # Download finished (100%)
    torrent["progress"] = 1.0
    if files:
        torrent["files"] = files
    torrent["step"] = "check_media"
    note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · download finished")
    return StepResult.done(next_step="check_media")
