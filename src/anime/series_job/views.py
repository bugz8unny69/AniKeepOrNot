"""What the dashboard and adm show of the series job: Intake (each series) and Running (now).

Read-only views of the series rows; no job numbers anywhere (owner, 2026-11-22).
"""
import json

from src.anime.anime_titles import title_for
from src.anime.series_job.store import get_series

ACTIVE = ("waiting", "working")


def _title(db, aid, title, language):
    return title_for(db, aid, title, language)     # The metadata, then AniDB's cached title list.


# The steps that measure their progress, and where they keep it (owner, 2026-11-28: the
# download's 100% stayed on screen through the transcode). Other steps show none.
STEP_PROGRESS = {"download": "progress", "transcode": "transcode_progress"}


def step_progress(t):
    """The current step's progress, 0.0 to 1.0, or None when the step has none (or the torrent is not working)."""
    key = STEP_PROGRESS.get(t.get("step"))
    if key is None or t.get("state") not in ACTIVE:
        return None
    return t.get(key, 0.0)


def _torrent(t):
    return {"infohash": t.get("infohash"), "release_name": t.get("release_name"), "step": t.get("step"),
            "state": t.get("state"), "why": t.get("why"), "episodes": t.get("episodes", []),
            "progress": step_progress(t), "cleanup": t.get("cleanup")}


def _keep(work):
    keep = work.get("keep")
    if keep is None:
        return None
    return {"step": keep["step"], "stopped": keep.get("stopped"), "requested_at_ms": keep["requested_at_ms"],
            "search": keep.get("search"), "shoko_incomplete": (keep.get("shoko") or {}).get("incomplete")}


def series_view(db, aid, language="english"):
    """One series: its torrents, its episodes and its recent history (Intake's series page)."""
    s = get_series(db, aid)
    if s is None:
        return None
    search = s.work.get("search") or {}
    from src.anime.series_job.store import promoted_at
    return {"anidb_id": s.anidb_id, "title": _title(db, s.anidb_id, s.title, language), "state": s.state,
            "promoted_at_ms": promoted_at(db, s.anidb_id),
            "wake_at_ms": s.wake_at_ms, "torrents_at_once": s.torrents_at_once,
            "torrents": [_torrent(t) for t in s.torrents], "episodes": s.episodes, "recent": s.recent,
            "keep": _keep(s.work), "rejecting": "reject" in s.work, "stopped": s.work.get("stopped"),
            "waiting": s.work.get("waiting"),
            "search": {"wanted": search.get("wanted", []), "due_ms": search.get("due_ms")},
            "created_at_ms": s.created_at_ms, "updated_at_ms": s.updated_at_ms}


def series_list(db, language="english", *, limit=30, before=(1 << 63) - 1, state=None, match=None):
    """Intake: series by AniDB ID, newest first (paged by the last AniDB ID shown).

    match: (sql, params) from search.match('intake', words).
    """
    where, params = ["anidb_id<?"], [before]
    if state is not None:
        where.append("state=?")
        params.append(state)
    if match is not None:
        where.append(match[0])
        params.extend(match[1])
    rows = db.execute("SELECT anidb_id,title,state,wake_at_ms,torrents_json,episodes_json,updated_at_ms,work_json,"
                      "promoted_at_ms "
                      f"FROM series WHERE {' AND '.join(where)} ORDER BY anidb_id DESC LIMIT ?",
                      (*params, limit + 1)).fetchall()
    items = []
    for aid, title, state_, wake, torrents, episodes, updated, work, promoted in rows[:limit]:
        torrents, episodes, work = json.loads(torrents), json.loads(episodes), json.loads(work)
        counts = {}
        for data in episodes.values():
            counts[data.get("state")] = counts.get(data.get("state"), 0) + 1
        items.append({"anidb_id": aid, "title": _title(db, aid, title, language), "state": state_,
                      "wake_at_ms": wake, "updated_at_ms": updated, "episodes": counts,
                      "torrents_active": sum(t.get("state") in ACTIVE for t in torrents),
                      "torrents_stopped": sum(t.get("state") == "stopped" for t in torrents),
                      "keep": _keep(work), "stopped": work.get("stopped"), "promoted_at_ms": promoted})
    return {"items": items, "next_before_id": rows[limit - 1][0] if len(rows) > limit else None}


# Keep's steps up to Sonarr having every episode. After them only follow-ups are left (Shoko
# linking, the dub search, Jellyfin's rescan, cleanup): nothing to watch or do, and new
# webhooks for the series are refused, so it leaves Running (owner, 2026-11-28).
KEEP_IN_HAND = ("cancel", "sonarr_series", "sonarr_import")


def keep_in_hand(work):
    """A Keep Running shows: Sonarr does not have every episode yet, or it stopped and waits for you."""
    keep = work.get("keep")
    return keep is not None and (keep.get("step") in KEEP_IN_HAND or bool(keep.get("stopped")))


def running(db, language="english"):
    """Running: every series with work in hand (a torrent on a step, Keep until Sonarr has it, Reject), and the
    releases the API is still identifying. A series waiting on a download is asleep between
    checks, so this is not only state WORKING."""
    rows = db.execute("SELECT anidb_id,title,state,wake_at_ms,torrents_json,work_json,promoted_at_ms FROM series "
                      "WHERE state IN ('WORKING','ASLEEP') "
                      "ORDER BY (promoted_at_ms IS NULL), promoted_at_ms DESC, updated_at_ms DESC").fetchall()
    series = []
    for aid, title, state, wake, torrents, work, promoted in rows:
        torrents, work = json.loads(torrents), json.loads(work)
        active = [_torrent(t) for t in torrents if t.get("state") in ACTIVE]
        if not active and not keep_in_hand(work) and "reject" not in work:
            continue
        series.append({"anidb_id": aid, "title": _title(db, aid, title, language), "state": state, "wake_at_ms": wake,
                       "promoted_at_ms": promoted, "torrents": active,
                       "keep": _keep(work), "rejecting": "reject" in work, "waiting": work.get("waiting")})
    waiting = db.execute("SELECT request_id,infohash,received_at_ms,tries FROM identity_requests "
                         "WHERE outcome IS NULL ORDER BY received_at_ms LIMIT 50").fetchall()
    return {"series": series, "waiting_identity": [
        {"request_id": r, "infohash": h, "received_at_ms": at, "tries": n} for r, h, at, n in waiting]}
