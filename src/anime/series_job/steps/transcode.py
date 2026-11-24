"""SJ-4e: Transcode media step."""
import logging
import time
from src.anime.series_job.steps.common import StepResult
from src.anime.series_job.store import SeriesRow, note
from src.anime.services.transcode import TranscodeWaiting, TranscodeFailed
from src.transcode_agent.encode import EncodeError

logger = logging.getLogger("akon.series")


async def transcode(series: SeriesRow, torrent: dict, context) -> StepResult:
    """Run transcoding if enabled; otherwise skip straight to staging."""
    now = getattr(context, "now", int(time.time() * 1000))
    config = getattr(context, "configuration", None)
    mode = "off"
    if config is not None:
        try:
            mode = config.value("transcode.mode")
        except Exception:
            mode = "off"

    ep_label = ", ".join(torrent.get("episodes", [])) or "release"
    release_name = torrent.get("release_name", "")

    if mode == "off":
        torrent["step"] = "stage"
        return StepResult.done(next_step="stage")

    transcoder = getattr(context, "transcoder", None)
    if transcoder is not None:
        try:
            status = await transcoder(series, torrent, context)
            if status == "waiting":
                return StepResult.wait(now + 10000, why="TRANSCODE_WAITING")
        except TranscodeWaiting as exc:
            return StepResult.wait(now + 10000, why="TRANSCODE_WAITING")
        except (TranscodeFailed, EncodeError) as exc:
            note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · dropped: TRANSCODE_FAILED")
            return StepResult.drop("TRANSCODE_FAILED")

    torrent["step"] = "stage"
    note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · transcode finished")
    return StepResult.done(next_step="stage")
