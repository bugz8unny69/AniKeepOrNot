"""What the dashboard and adm show of the series job: Intake (each series) and Running (now).

Read-only views of the series rows; no job numbers anywhere (owner, 2026-11-22).
"""
import json

from src.anime.anime_titles import preferred_title
from src.anime.series_job.store import get_series

ACTIVE = ("waiting", "working")


def _title(db, aid, title, language):
    row = db.execute("SELECT english_title,romaji_title FROM anime_title_metadata WHERE anidb_id=?", (aid,)).fetchone()
    return preferred_title(title, row[0] if row else None, row[1] if row else None, language)


def _torrent(t):
    return {"infohash": t.get("infohash"), "release_name": t.get("release_name"), "step": t.get("step"),
            "state": t.get("state"), "why": t.get("why"), "episodes": t.get("episodes", []),
            "progress": t.get("progress", 0.0), "cleanup": t.get("cleanup")}


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
    return {"anidb_id": s.anidb_id, "title": _title(db, s.anidb_id, s.title, language), "state": s.state,
            "wake_at_ms": s.wake_at_ms, "torrents_at_once": s.torrents_at_once,
            "torrents": [_torrent(t) for t in s.torrents], "episodes": s.episodes, "recent": s.recent,
            "keep": _keep(s.work), "rejecting": "reject" in s.work,
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
    rows = db.execute("SELECT anidb_id,title,state,wake_at_ms,torrents_json,episodes_json,updated_at_ms,work_json "
                      f"FROM series WHERE {' AND '.join(where)} ORDER BY anidb_id DESC LIMIT ?",
                      (*params, limit + 1)).fetchall()
    items = []
    for aid, title, state_, wake, torrents, episodes, updated, work in rows[:limit]:
        torrents, episodes, work = json.loads(torrents), json.loads(episodes), json.loads(work)
        counts = {}
        for data in episodes.values():
            counts[data.get("state")] = counts.get(data.get("state"), 0) + 1
        items.append({"anidb_id": aid, "title": _title(db, aid, title, language), "state": state_,
                      "wake_at_ms": wake, "updated_at_ms": updated, "episodes": counts,
                      "torrents_active": sum(t.get("state") in ACTIVE for t in torrents),
                      "torrents_stopped": sum(t.get("state") == "stopped" for t in torrents),
                      "keep": _keep(work)})
    return {"items": items, "next_before_id": rows[limit - 1][0] if len(rows) > limit else None}


def running(db):
    """Running: every series with work in hand (a torrent on a step, Keep or Reject), and the
    releases the API is still identifying. A series waiting on a download is asleep between
    checks, so this is not only state WORKING."""
    rows = db.execute("SELECT anidb_id,title,state,wake_at_ms,torrents_json,work_json FROM series "
                      "WHERE state IN ('WORKING','ASLEEP') ORDER BY updated_at_ms DESC").fetchall()
    series = []
    for aid, title, state, wake, torrents, work in rows:
        torrents, work = json.loads(torrents), json.loads(work)
        active = [_torrent(t) for t in torrents if t.get("state") in ACTIVE]
        if not active and "keep" not in work and "reject" not in work:
            continue
        series.append({"anidb_id": aid, "title": title, "state": state, "wake_at_ms": wake, "torrents": active,
                       "keep": _keep(work), "rejecting": "reject" in work})
    waiting = db.execute("SELECT request_id,infohash,received_at_ms,tries FROM identity_requests "
                         "WHERE outcome IS NULL ORDER BY received_at_ms LIMIT 50").fetchall()
    return {"series": series, "waiting_identity": [
        {"request_id": r, "infohash": h, "received_at_ms": at, "tries": n} for r, h, at, n in waiting]}
