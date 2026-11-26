"""SJ-5: Actions on series (keep, reject, search, retry, cancel).

Decisions and search act directly on the series row, not via separate jobs.
"""
import asyncio
import inspect
import logging
import os
import time

from src.anime.series_job.store import (
    get_series, update_series, wake, add_torrent, note, SeriesRow
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
    """Keep series in Sonarr, mark episodes kept, drop remaining torrents."""
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")
    if series.state == "KEPT":
        return {"anidb_id": anidb_id, "state": "KEPT", "message": f"Series {anidb_id} already kept."}
    if series.state == "REJECTED":
        raise ValueError(f"Series {anidb_id} was already rejected")

    # If context has a custom keeper, invoke it
    keeper = getattr(context, "keeper", None)
    if keeper is not None:
        res = keeper(series, context)
        if inspect.iscoroutine(res):
            await res

    # Drop/cancel active torrents that were not completed
    qb_client = getattr(context, "qb_client", None)
    for t in series.torrents:
        if t.get("state") in ("waiting", "working"):
            if qb_client is not None and t.get("infohash") and hasattr(qb_client, "delete_torrent"):
                try:
                    res = qb_client.delete_torrent(t["infohash"], delete_files=True)
                    if inspect.iscoroutine(res):
                        await res
                except Exception:
                    pass
            t["state"] = "dropped"
            t["why"] = "series kept; active torrent cancelled"

    # Mark staged episodes as kept
    for ep_code, ep_data in series.episodes.items():
        if ep_data.get("state") == "staged":
            ep_data["state"] = "kept"

    series.state = "KEPT"
    series.wake_at_ms = None
    series.lease_token = None
    series.lease_expires_at_ms = None

    note(series, f"series {series.anidb_id} {series.title} · kept; Sonarr owns it")
    update_series(db, series, now=now)

    try:
        db.execute(
            "INSERT INTO anime_review_decisions(anidb_id, operation_id, job_id, decision, cleanup_state, decided_at_ms) "
            "VALUES(?, 0, ?, 'ACCEPT', 'COMPLETE', ?) "
            "ON CONFLICT(anidb_id) DO UPDATE SET decision='ACCEPT', cleanup_state='COMPLETE', decided_at_ms=excluded.decided_at_ms",
            (anidb_id, anidb_id, now)
        )
    except Exception:
        pass

    return {
        "anidb_id": anidb_id,
        "state": "KEPT",
        "message": f"Series {anidb_id} {series.title} kept; Sonarr owns it.",
    }


def keep_series_sync(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    return _run_sync(keep_series(db, anidb_id, context=context, now=now))


async def reject_series(
    db, anidb_id: int, context=None, reason: str = "REJECTED_BY_USER", note_text: str = "", now: int | None = None
) -> dict:
    """Reject series, delete staged files, remove torrents with files."""
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")
    if series.state == "REJECTED":
        return {"anidb_id": anidb_id, "state": "REJECTED", "message": f"Series {anidb_id} already rejected."}
    if series.state == "KEPT":
        raise ValueError(f"Series {anidb_id} was already kept")

    # If context has a custom remover/cleaner, invoke it
    remover = getattr(context, "remover", None)
    if remover is not None:
        res = remover(series, context)
        if inspect.iscoroutine(res):
            await res

    # Delete staged files if keep_root/staging is configured
    keep_root = None
    if context and hasattr(context, "configuration"):
        try:
            keep_root = context.configuration.value("storage.keep_root")
        except Exception:
            pass
    for ep_code, ep_data in series.episodes.items():
        file_path = ep_data.get("file")
        if file_path and keep_root:
            full_path = os.path.join(keep_root, file_path) if not os.path.isabs(file_path) else file_path
            try:
                if os.path.exists(full_path):
                    os.remove(full_path)
            except OSError:
                pass
        ep_data["state"] = "dropped"

    # Drop all active torrents from qBittorrent with files
    qb_client = getattr(context, "qb_client", None)
    for t in series.torrents:
        if t.get("infohash") and qb_client is not None and t.get("state") in ("waiting", "working"):
            if hasattr(qb_client, "delete_torrent"):
                try:
                    res = qb_client.delete_torrent(t["infohash"], delete_files=True)
                    if inspect.iscoroutine(res):
                        await res
                except Exception:
                    pass
        t["state"] = "dropped"
        t["why"] = reason

    series.state = "REJECTED"
    series.wake_at_ms = None
    series.lease_token = None
    series.lease_expires_at_ms = None

    note(series, f"series {series.anidb_id} {series.title} · rejected")
    update_series(db, series, now=now)

    try:
        db.execute(
            "INSERT INTO anime_review_decisions(anidb_id, operation_id, job_id, decision, cleanup_state, decided_at_ms, reason_text) "
            "VALUES(?, 0, ?, 'REJECT', 'COMPLETE', ?, ?) "
            "ON CONFLICT(anidb_id) DO UPDATE SET decision='REJECT', cleanup_state='COMPLETE', decided_at_ms=excluded.decided_at_ms, reason_text=excluded.reason_text",
            (anidb_id, anidb_id, now, note_text or reason)
        )
    except Exception:
        pass

    return {
        "anidb_id": anidb_id,
        "state": "REJECTED",
        "message": f"Series {anidb_id} {series.title} rejected.",
    }


def reject_series_sync(
    db, anidb_id: int, context=None, reason: str = "REJECTED_BY_USER", note_text: str = "", now: int | None = None
) -> dict:
    return _run_sync(reject_series(db, anidb_id, context=context, reason=reason, note_text=note_text, now=now))


async def search_series(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    """Find candidate releases for missing episodes, add as torrents, wake series."""
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")
    if series.state in ("KEPT", "REJECTED"):
        raise ValueError(f"Series {anidb_id} is already {series.state.lower()}")

    searcher = getattr(context, "searcher", None)
    candidates_found = 0
    if searcher is not None:
        res = searcher(series, context)
        candidates = await res if inspect.iscoroutine(res) else res
        if candidates:
            for release in candidates:
                if add_torrent(db, anidb_id, release, now=now):
                    candidates_found += 1

    series = wake(db, anidb_id, series.title, now=now)
    note(series, f"series {series.anidb_id} {series.title} · search triggered")
    update_series(db, series, now=now)

    return {
        "anidb_id": anidb_id,
        "message": f"Series {anidb_id} {series.title} search triggered.",
        "candidates_added": candidates_found,
    }


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
    """Cancel active torrents and put series to sleep."""
    if now is None:
        now = int(time.time() * 1000)
    series = get_series(db, anidb_id)
    if series is None:
        raise ValueError(f"Series {anidb_id} not found")
    if series.state in ("KEPT", "REJECTED"):
        return {"anidb_id": anidb_id, "state": series.state, "message": f"Series {anidb_id} is already {series.state.lower()}."}

    qb_client = getattr(context, "qb_client", None)
    for t in series.torrents:
        if t.get("state") in ("waiting", "working"):
            if qb_client is not None and t.get("infohash") and hasattr(qb_client, "delete_torrent"):
                try:
                    res = qb_client.delete_torrent(t["infohash"], delete_files=False)
                    if inspect.iscoroutine(res):
                        await res
                except Exception:
                    pass
            t["state"] = "dropped"
            t["why"] = "canceled by user"

    series.state = "ASLEEP"
    series.wake_at_ms = None
    series.lease_token = None
    series.lease_expires_at_ms = None

    note(series, f"series {series.anidb_id} {series.title} · canceled")
    update_series(db, series, now=now)

    return {
        "anidb_id": anidb_id,
        "state": "ASLEEP",
        "message": f"Series {anidb_id} {series.title} canceled.",
    }


def cancel_series_sync(db, anidb_id: int, context=None, now: int | None = None) -> dict:
    return _run_sync(cancel_series(db, anidb_id, context=context, now=now))
