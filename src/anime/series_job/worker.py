"""SJ-4: Worker loop for series jobs."""
import asyncio
import logging
import os
import time
import traceback

from src.anime.series_job.steps.metadata import get_metadata
from src.anime.series_job.steps.check_release import check_release
from src.anime.series_job.steps.reserve_episodes import reserve_episodes
from src.anime.series_job.steps.download import download
from src.anime.series_job.steps.check_media import check_media
from src.anime.series_job.steps.transcode import transcode
from src.anime.series_job.steps.stage import stage
from src.anime.series_job.steps.drop import drop_torrent
from src.anime.series_job.steps.common import STEP_SEQUENCE, next_step_name
from src.anime.series_job.store import LeaseLost, _atomic, claim, note, renew, save

logger = logging.getLogger("akon.series")

STEP_HANDLERS = {
    "get_metadata": get_metadata,
    "check_release": check_release,
    "reserve_episodes": reserve_episodes,
    "download": download,
    "check_media": check_media,
    "transcode": transcode,
    "stage": stage,
}


def _ready_first(torrents):
    """Torrents further along go first, so a torrent waiting for an episode never takes
    the slot of the torrent that holds it."""
    order = {name: i for i, name in enumerate(STEP_SEQUENCE)}
    return sorted(torrents, key=lambda t: -order.get(t.get("step") or "get_metadata", 0))


async def _decided(db, context, claim_obj, series, now):
    """Keep or Reject: series work instead of torrent steps; then the series is KEPT or REJECTED."""
    from src.anime.series_job.keep import run_keep
    from src.anime.series_job.reject import run_reject
    keeping = "keep" in series.work
    result = await (run_keep if keeping else run_reject)(series, context)
    if result.action == "done":
        series.state, series.wake_at_ms = ("KEPT" if keeping else "REJECTED"), None
    else:
        series.state, series.wake_at_ms = "ASLEEP", result.wake_at_ms
    series.lease_token = series.lease_expires_at_ms = None
    save(db, claim_obj, series, now=now)
    return True


async def run_series_attempt(db, context, now: int | None = None) -> bool:
    """Take one due series, advance up to N torrents by one step, and save.

    After the attempt the series is WORKING only when a torrent moved on;
    when every torrent is waiting it sleeps until the soonest wake time, or
    until something wakes it (the API, an action, another torrent finishing).
    """
    if now is None:
        now = int(time.time() * 1000)
    context.now = now

    claimed = claim(db, now)
    if claimed is None:
        return False

    claim_obj, series = claimed
    if "keep" in series.work or "reject" in series.work:
        return await _decided(db, context, claim_obj, series, now)
    # A step records what it is about to send (a torrent add, a Sonarr import) before sending it.
    # Its own connection: Keep steps run in a thread.
    database = db.execute("PRAGMA database_list").fetchone()[2]

    def checkpoint():
        from contextlib import closing
        from src.anime.storage.sqlite import connect
        if not database:                      # An in-memory database (tests): same thread only.
            return save(db, claim_obj, series, now=now)
        with closing(connect(database)) as own:
            save(own, claim_obj, series, now=now)
    context.checkpoint = checkpoint
    context.claim = claim_obj
    if series.work.pop("cancel", None) is not None:
        for torrent in series.torrents:
            if torrent.get("state") in ("waiting", "working", "stopped"):
                await drop_torrent(series, torrent, "canceled by you", context)
        series.work.get("search", {})["due_ms"] = None
    from src.anime.series_job.search import run_search
    search_wake = await run_search(series, context)        # May add torrents for the trial episodes.
    active_torrents = [t for t in series.torrents if t.get("state") in ("waiting", "working")]
    to_run = _ready_first(active_torrents)[:series.torrents_at_once or 1]
    min_wake_ms: int | None = None
    moved = False
    if any(t.get("state") == "waiting" for t in to_run):
        # New work; a timed re-check of a download logs nothing (repeated waits are quiet).
        logger.info("series %s %s · work started", series.anidb_id, series.title)

    for torrent in to_run:
        step_name = torrent.get("step") or "get_metadata"
        episodes = torrent.get("episodes", [])
        ep_label = ", ".join(episodes) if episodes else "release"
        release_name = torrent.get("release_name", "")

        if torrent.get("state") == "waiting":
            logger.info("series %s %s · %s %s · %s started",
                        series.anidb_id, series.title, ep_label, release_name, step_name.replace("_", " "))
        try:
            result = await STEP_HANDLERS[step_name](series, torrent, context)
        except LeaseLost:
            raise                                  # An action took the series; the attempt is redone.
        except Exception as exc:
            # Boundary (plan, Errors): an unexpected error is a program bug. The torrent
            # stops with "program error" and keeps its files; Retry resumes it at this step.
            where = traceback.extract_tb(exc.__traceback__)[-1]
            error_loc = f"program error at {os.path.basename(where.filename)}:{where.lineno} ({type(exc).__name__})"
            logger.error("series %s %s · %s %s · %s stopped: %s", series.anidb_id, series.title, ep_label,
                         release_name, step_name.replace("_", " "), error_loc, exc_info=exc)
            torrent["state"], torrent["why"] = "stopped", error_loc
            note(series, f"series {series.anidb_id} {series.title} · {ep_label} {release_name} · "
                         f"stopped: {error_loc}; Retry resumes it")
            from src.anime.notifications import notify
            with _atomic(db):
                notify(db, {
                    "type": "JOB_FAILED",
                    "severity": "attention",
                    "title": f"Series error: {series.title}"[:120],
                    "message": f"{release_name or 'Release'} stopped: {error_loc}"[:500],
                    "anidb_id": series.anidb_id,
                    "release_name": release_name[:1024],
                }, now=now)
            continue
        if result.action == "done":
            moved = True
            next_step = result.next_step or next_step_name(step_name)
            torrent["step"] = next_step
            torrent["state"] = "done" if next_step == "done" else "working"
        elif result.action == "wait":
            torrent["state"] = "working"
            if result.wake_at_ms is not None and (min_wake_ms is None or result.wake_at_ms < min_wake_ms):
                min_wake_ms = result.wake_at_ms
        else:
            moved = True
            await drop_torrent(series, torrent, result.why or "DROPPED", context)
        renew(db, claim_obj, now)

    remaining_active = [t for t in series.torrents if t.get("state") in ("waiting", "working")]
    if remaining_active and moved:
        series.state, series.wake_at_ms = "WORKING", None
    else:
        wakes = [w for w in (min_wake_ms if remaining_active else None, search_wake) if w is not None]
        series.state, series.wake_at_ms = "ASLEEP", (min(wakes) if wakes else None)

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
