"""SJ-4: Worker loop for series jobs."""
import asyncio
import logging
import os
import time
import traceback

from src.anime.series_job.steps.check_release import check_release
from src.anime.series_job.steps.reserve_episodes import reserve_episodes
from src.anime.series_job.steps.download import download
from src.anime.series_job.steps.check_media import check_media
from src.anime.series_job.steps.transcode import transcode
from src.anime.series_job.steps.stage import stage
from src.anime.series_job.steps.drop import drop_torrent
from src.anime.series_job.steps.common import next_step_name
from src.anime.series_job.store import claim, save, Claim, SeriesRow

logger = logging.getLogger("akon.series")

STEP_HANDLERS = {
    "check_release": check_release,
    "reserve_episodes": reserve_episodes,
    "download": download,
    "check_media": check_media,
    "transcode": transcode,
    "stage": stage,
}


async def run_series_attempt(db, context, now: int | None = None) -> bool:
    """Take one claimed series, advance up to N torrents by one step, and save."""
    if now is None:
        now = int(time.time() * 1000)
    context.now = now

    claimed = claim(db, now)
    if claimed is None:
        return False

    claim_obj, series = claimed

    active_torrents = [t for t in series.torrents if t.get("state") in ("waiting", "working")]
    if not active_torrents:
        series.state = "ASLEEP"
        series.wake_at_ms = None
        save(db, claim_obj, series, now=now)
        return True

    # Limit to N torrents at once
    n = series.torrents_at_once or 1
    to_run = active_torrents[:n]
    min_wake_ms: int | None = None

    for torrent in to_run:
        step_name = torrent.get("step") or "check_release"
        handler = STEP_HANDLERS.get(step_name)
        if handler is None:
            await drop_torrent(series, torrent, f"UNKNOWN_STEP_{step_name}", context)
            continue

        try:
            result = await handler(series, torrent, context)
            if result.action == "done":
                next_step = result.next_step or next_step_name(step_name)
                torrent["step"] = next_step
                if next_step == "done":
                    torrent["state"] = "done"
                else:
                    torrent["state"] = "working"
            elif result.action == "wait":
                torrent["state"] = "working"
                if result.wake_at_ms is not None:
                    if min_wake_ms is None or result.wake_at_ms < min_wake_ms:
                        min_wake_ms = result.wake_at_ms
            elif result.action == "drop":
                await drop_torrent(series, torrent, result.why or "DROPPED", context)
        except Exception as exc:
            # Unexpected error boundary: log full trace, drop torrent, send JOB_FAILED notification
            tb = exc.__traceback__
            while tb and tb.tb_next:
                tb = tb.tb_next
            frame = tb.tb_frame if tb else None
            filename = os.path.basename(frame.f_code.co_filename) if frame else "unknown"
            lineno = tb.tb_lineno if tb else 0
            error_loc = f"program error at {filename}:{lineno}"

            logger.error(
                "Series %s torrent %s failed with unexpected error:\n%s",
                series.anidb_id, torrent.get("infohash"), traceback.format_exc()
            )
            await drop_torrent(series, torrent, error_loc, context)

            try:
                from src.anime.notifications import notify
                notify(db, {
                    "type": "JOB_FAILED",
                    "severity": "attention",
                    "title": f"Series error: {series.title}"[:120],
                    "message": f"{torrent.get('release_name', 'Release')} stopped: {error_loc}"[:500],
                    "anidb_id": series.anidb_id,
                    "release_name": torrent.get("release_name", "")[:1024],
                }, now=now)
            except Exception as notify_err:
                logger.warning("Could not send JOB_FAILED notification: %s", notify_err)

    # Determine series state after running steps
    remaining_active = [t for t in series.torrents if t.get("state") in ("waiting", "working")]
    if not remaining_active:
        series.state = "ASLEEP"
        series.wake_at_ms = None
    elif min_wake_ms is not None:
        series.state = "ASLEEP"
        series.wake_at_ms = min_wake_ms
    else:
        series.state = "WORKING"
        series.wake_at_ms = None

    # Release lease on completion of attempt
    series.lease_token = None
    series.lease_expires_at_ms = None
    save(db, claim_obj, series, now=now)
    return True


class SeriesWorker:
    """Background series worker loop."""

    def __init__(self, database_path: str, context, *, poll_interval_s: float = 0.5):
        self.database_path = database_path
        self.context = context
        self.poll_interval_s = poll_interval_s
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        from src.anime.storage.sqlite import connect
        while not self._stop.is_set():
            worked = False
            try:
                db = connect(self.database_path)
                try:
                    worked = await run_series_attempt(db, self.context)
                finally:
                    db.close()
            except Exception as exc:
                logger.error("Series worker loop error:\n%s", traceback.format_exc())

            if not worked:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
                except asyncio.TimeoutError:
                    pass
