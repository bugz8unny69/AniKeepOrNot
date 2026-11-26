"""SJ-5: Actions on series (keep, reject, search, retry, cancel).

Decisions and search act directly on the series row, not via separate jobs.
"""
import asyncio
import logging
import os
import time

from src.anime.series_job.store import (
    get_series, update_series, note
)

logger = logging.getLogger("akon.series")


def _run_sync(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def keep_series(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    """Keep: record it and wake the series; the worker does the work (series_job/keep.py)."""
    from src.anime.series_job.keep import request
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")
    if series.state == "KEPT" or "keep" in series.work:
        return {"anidb_id": anidb_id, "state": series.state, "message": f"Series {anidb_id} is already kept or keeping."}
    if series.state == "REJECTED" or "reject" in series.work:
        raise ValueError(f"Series {anidb_id} was already rejected")
    request(series, now)
    series.lease_token = series.lease_expires_at_ms = None    # A running attempt is redone, never saved over this.
    note(series, f"series {series.anidb_id} {series.title} · Keep requested: Sonarr will own it")
    update_series(db, series, now=now)
    return {"anidb_id": anidb_id, "state": "WORKING",
            "message": f"Keep requested for {series.title}; the worker imports it into Sonarr."}


def keep_series_sync(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    return _run_sync(keep_series(db, anidb_id, context=context, now=now))


async def reject_series(
    db, anidb_id: int, context=None, reason: str = "REJECTED_BY_USER", note_text: str = "", now: int | None = None
) -> dict:
    """Reject: record it and wake the series; the worker removes its torrents and staged files."""
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")
    if series.state == "REJECTED" or "reject" in series.work:
        return {"anidb_id": anidb_id, "state": series.state, "message": f"Series {anidb_id} is already rejected."}
    if series.state == "KEPT" or "keep" in series.work:
        raise ValueError(f"Series {anidb_id} was already kept")
    series.work["reject"] = {"requested_at_ms": now, "reason": reason, "note": note_text}
    series.state, series.wake_at_ms = "WORKING", None
    series.lease_token = series.lease_expires_at_ms = None
    note(series, f"series {series.anidb_id} {series.title} · Reject requested ({note_text or reason})")
    update_series(db, series, now=now)
    return {"anidb_id": anidb_id, "state": "WORKING",
            "message": f"Reject requested for {series.title}; the worker removes its torrents and staged files."}


def reject_series_sync(
    db, anidb_id: int, context=None, reason: str = "REJECTED_BY_USER", note_text: str = "", now: int | None = None
) -> dict:
    return _run_sync(reject_series(db, anidb_id, context=context, reason=reason, note_text=note_text, now=now))


async def search_series(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    """Search now for the trial episodes no torrent covers (series_job/search.py)."""
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")
    if series.state in ("KEPT", "REJECTED"):
        raise ValueError(f"Series {anidb_id} is already {series.state.lower()}")
    series.work.setdefault("search", {"wanted": [], "tried": {}})["due_ms"] = now
    series.state, series.wake_at_ms = "WORKING", None
    series.lease_token = series.lease_expires_at_ms = None
    note(series, f"series {series.anidb_id} {series.title} · search triggered")
    update_series(db, series, now=now)
    return {"anidb_id": anidb_id, "message": f"Series {anidb_id} {series.title} search triggered."}


def search_series_sync(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    return _run_sync(search_series(db, anidb_id, context=context, now=now))


async def retry_series(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    """Wake the series: a stopped torrent (program error) resumes at its step; a dropped one starts over."""
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")

    retried_count = 0
    if series.work.get("keep", {}).pop("stopped", None):
        retried_count += 1                  # Keep resumes at the step it stopped at.
    for t in series.torrents:
        if t.get("state") == "stopped":
            t["state"] = "working"          # Its files are still there.
            t["why"] = None
            retried_count += 1
        elif t.get("state") == "dropped":
            t["state"] = "waiting"
            t["step"] = "get_metadata"
            t["why"] = None
            retried_count += 1

    series.state = "WORKING"
    series.wake_at_ms = None
    series.lease_token = None
    series.lease_expires_at_ms = None

    note(series, f"series {series.anidb_id} {series.title} · retried")
    update_series(db, series, now=now)

    return {
        "anidb_id": anidb_id,
        "message": f"Series {anidb_id} {series.title} retried.",
        "torrents_retried": retried_count,
    }


def retry_series_sync(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    return _run_sync(retry_series(db, anidb_id, context=context, now=now))


async def cancel_series(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    """Cancel: the worker drops the series' working torrents (AKON's, with their files) and stops searching."""
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")
    if series.state in ("KEPT", "REJECTED"):
        return {"anidb_id": anidb_id, "state": series.state, "message": f"Series {anidb_id} is already {series.state.lower()}."}
    series.work["cancel"] = now
    series.state, series.wake_at_ms = "WORKING", None
    series.lease_token = series.lease_expires_at_ms = None
    note(series, f"series {series.anidb_id} {series.title} · Cancel requested")
    update_series(db, series, now=now)
    return {"anidb_id": anidb_id, "state": "WORKING", "message": f"Cancel requested for {series.title}."}


def cancel_series_sync(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    return _run_sync(cancel_series(db, anidb_id, context=context, now=now))
