"""SJ-4f: Stage media for evaluation."""
import logging
from src.anime.series_job.steps.common import StepResult
from src.anime.series_job.store import SeriesRow, note
from src.anime.staging import StagingReview, StagingUnavailable, StagingBusy

logger = logging.getLogger("akon.series")


async def stage(series: SeriesRow, torrent: dict, context) -> StepResult:
    """Publish media files to the staging folder for user evaluation."""
    episodes = torrent.get("episodes", [])
    ep_label = ", ".join(episodes) or "release"
    release_name = torrent.get("release_name", "")

    stager = getattr(context, "stager", None)
    if stager is not None:
        try:
            await stager(series, torrent, context)
        except (StagingReview, StagingBusy) as exc:
            reason = getattr(exc, "reason_code", "STAGING_FAILED")
            note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · dropped: {reason}")
            return StepResult.drop(reason)
        except StagingUnavailable:
            now = getattr(context, "now", 0)
            return StepResult.wait(now + 30000, why="STAGING_UNAVAILABLE")

    # Update episode records to staged
    for ep_code in episodes:
        existing = series.episodes.get(ep_code, {})
        target_file = existing.get("file") or f"{series.title}/{ep_code}.mkv"
        series.episodes[ep_code] = {
            "state": "staged",
            "torrent": torrent.get("infohash", ""),
            "file": target_file,
        }

    torrent["step"] = "done"
    torrent["state"] = "done"
    note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · staged for evaluation (stage finished)")
    return StepResult.done(next_step="done")
