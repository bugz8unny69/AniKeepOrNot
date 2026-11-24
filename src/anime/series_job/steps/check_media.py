"""SJ-4d: Check downloaded media files (container, streams, audio)."""
import logging
from src.anime.acquisition.media import MediaInvalid, AudioMissing, ProbeUnavailable, MediaUnavailable
from src.anime.series_job.steps.common import StepResult
from src.anime.series_job.store import SeriesRow, note

logger = logging.getLogger("akon.series")

VALID_EXTENSIONS = (".mkv", ".mp4", ".avi", ".webm")


async def check_media(series: SeriesRow, torrent: dict, context) -> StepResult:
    """Verify that downloaded media files are valid and meet language/container criteria."""
    files = torrent.get("files", [])
    ep_label = ", ".join(torrent.get("episodes", [])) or "release"
    release_name = torrent.get("release_name", "")

    media_files = []
    for f in files:
        name = f.get("name", "") if isinstance(f, dict) else str(f)
        if any(name.lower().endswith(ext) for ext in VALID_EXTENSIONS):
            media_files.append(f)

    if not media_files and files:
        note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · dropped: no video files found")
        return StepResult.drop("MEDIA_VIDEO_MISSING")

    # If a custom media verifier is supplied in context
    verifier = getattr(context, "media_verifier", None)
    if verifier is not None:
        try:
            await verifier(series, torrent, context)
        except (MediaInvalid, AudioMissing, ProbeUnavailable, MediaUnavailable) as exc:
            reason = getattr(exc, "reason_code", "MEDIA_INVALID")
            note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · dropped: {reason}")
            return StepResult.drop(reason)

    torrent["step"] = "transcode"
    note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · media checked (check media finished)")
    return StepResult.done(next_step="transcode")
