"""Series storage and lease management.

The only module that reads and writes `series` table rows.

JSON shapes:

torrent: {
    "infohash": str,                # 40-character hexadecimal infohash
    "release_name": str,            # release name / title
    "raw_body": str | None,         # autobrr JSON payload or magnet text
    "step": str,                    # check_release, reserve_episodes, download, check_media, transcode, stage, etc.
    "state": str,                   # "waiting", "working", "done", "dropped", "stopped" (program error)
    "why": str | None,              # reason when dropped or why waiting
    "episodes": list[str],          # episode codes, e.g. ["S01E01", "S01E02"]
    "files": list[dict],            # media files discovered or matched
    "progress": float,              # download or transcode progress (0.0 to 1.0)
    "submitted_at_ms": int | None,  # timestamp when submitted to download client
    "identity": dict | None,        # the identity process's series: anime.anidb_id, tvdb_series_id, title
}

episode: {
    # Keyed by episode code, e.g. "S01E03":
    "state": str,                   # "reserved", "downloading", "verified", "transcoding", "staged", "kept"
    "torrent": str,                 # infohash of the assigned torrent
    "file": str | None,             # path to staged or verified media file
}

recent: list of up to 50 formatted string lines, newest last.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import time
import uuid

logger = logging.getLogger("akon.series")

DEFAULT_LEASE_MS = 300_000


class LeaseLost(RuntimeError):
    """Raised when saving a series whose lease has expired or was taken by another worker."""
    pass


@dataclass(frozen=True)
class Claim:
    anidb_id: int
    token: str
    expires_at_ms: int


@dataclass
class SeriesRow:
    anidb_id: int
    title: str
    state: str                             # 'WORKING', 'ASLEEP', 'KEPT', 'REJECTED'
    wake_at_ms: int | None
    torrents_at_once: int
    torrents: list[dict] = field(default_factory=list)
    episodes: dict[str, dict] = field(default_factory=dict)
    recent: list[str] = field(default_factory=list)
    lease_token: str | None = None
    lease_expires_at_ms: int | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    work: dict = field(default_factory=dict)  # Series-level work: keep (Sonarr, Shoko, dubs, Jellyfin, cleanup)


@contextmanager
def _atomic(db):
    if db.in_transaction:
        yield
    else:
        from src.anime.storage.sqlite import transaction
        with transaction(db):
            yield


def _row_from_tuple(t: tuple) -> SeriesRow:
    return SeriesRow(
        anidb_id=t[0],
        title=t[1],
        state=t[2],
        wake_at_ms=t[3],
        torrents_at_once=t[4],
        torrents=json.loads(t[5]) if t[5] else [],
        episodes=json.loads(t[6]) if t[6] else {},
        recent=json.loads(t[7]) if t[7] else [],
        lease_token=t[8],
        lease_expires_at_ms=t[9],
        created_at_ms=t[10],
        updated_at_ms=t[11],
        work=json.loads(t[12]) if t[12] else {},
    )


def claim(db, now: int, lease_ms: int = DEFAULT_LEASE_MS) -> tuple[Claim, SeriesRow] | None:
    """Take one series that is WORKING, or ASLEEP with wake_at_ms due, and lease it."""
    with _atomic(db):
        query = (
            "SELECT anidb_id, title, state, wake_at_ms, torrents_at_once, "
            "       torrents_json, episodes_json, recent_json, "
            "       lease_token, lease_expires_at_ms, created_at_ms, updated_at_ms, work_json "
            "FROM series "
            "WHERE (state = 'WORKING' OR (state = 'ASLEEP' AND wake_at_ms IS NOT NULL AND wake_at_ms <= ?)) "
            "  AND (lease_token IS NULL OR lease_expires_at_ms <= ?) "
            "ORDER BY COALESCE(wake_at_ms, updated_at_ms), anidb_id "
            "LIMIT 1"
        )
        row = db.execute(query, (now, now)).fetchone()
        if row is None:
            return None

        anidb_id = row[0]
        token = uuid.uuid4().hex
        expires_at_ms = now + lease_ms

        cur = db.execute(
            "UPDATE series "
            "SET state = 'WORKING', lease_token = ?, lease_expires_at_ms = ?, updated_at_ms = ? "
            "WHERE anidb_id = ? AND (lease_token IS ? OR lease_expires_at_ms <= ?)",
            (token, expires_at_ms, now, anidb_id, row[8], now)
        )
        if cur.rowcount == 0:
            return None

        series = _row_from_tuple(row)
        series.state = 'WORKING'
        series.lease_token = token
        series.lease_expires_at_ms = expires_at_ms
        series.updated_at_ms = now
        return Claim(anidb_id, token, expires_at_ms), series


def renew(db, claim: Claim, now: int, lease_ms: int = DEFAULT_LEASE_MS) -> None:
    """Extend the lease between torrents, so a long attempt keeps its series."""
    with _atomic(db):
        cur = db.execute("UPDATE series SET lease_expires_at_ms=? WHERE anidb_id=? AND lease_token=?",
                         (int(time.time() * 1000) + lease_ms, claim.anidb_id, claim.token))
        if cur.rowcount == 0:
            raise LeaseLost(f"Lease on series {claim.anidb_id} expired or lost")


def save(db, claim: Claim, row: SeriesRow, now: int | None = None) -> None:
    """Write the row back while the lease is held."""
    if now is None:
        now = int(time.time() * 1000)
    with _atomic(db):
        cur = db.execute(
            "UPDATE series "
            "SET title = ?, state = ?, wake_at_ms = ?, torrents_at_once = ?, "
            "    torrents_json = ?, episodes_json = ?, recent_json = ?, "
            "    lease_token = ?, lease_expires_at_ms = ?, updated_at_ms = ?, work_json = ? "
            "WHERE anidb_id = ? AND lease_token = ? AND lease_expires_at_ms > ?",
            (
                row.title,
                row.state,
                row.wake_at_ms,
                row.torrents_at_once,
                json.dumps(row.torrents),
                json.dumps(row.episodes),
                json.dumps(row.recent[-50:]),
                row.lease_token,
                row.lease_expires_at_ms,
                now,
                json.dumps(row.work),
                claim.anidb_id,
                claim.token,
                now,
            )
        )
        if cur.rowcount == 0:
            raise LeaseLost(f"Lease on series {claim.anidb_id} expired or lost")
        row.updated_at_ms = now


def wake(db, anidb_id: int, title: str, now: int | None = None, torrents_at_once: int = 1,
         *, reopen: bool = False) -> SeriesRow:
    """Create the row if new; set WORKING. A decided series stays decided unless reopen
    ("Process anyway" on a release of a series you decided)."""
    if now is None:
        now = int(time.time() * 1000)
    with _atomic(db):
        row = db.execute(
            "SELECT anidb_id, title, state, wake_at_ms, torrents_at_once, "
            "       torrents_json, episodes_json, recent_json, "
            "       lease_token, lease_expires_at_ms, created_at_ms, updated_at_ms, work_json "
            "FROM series WHERE anidb_id = ?",
            (anidb_id,)
        ).fetchone()

        if row is None:
            db.execute(
                "INSERT INTO series(anidb_id, title, state, wake_at_ms, torrents_at_once, "
                "                   torrents_json, episodes_json, recent_json, "
                "                   lease_token, lease_expires_at_ms, created_at_ms, updated_at_ms) "
                "VALUES(?, ?, 'WORKING', NULL, ?, '[]', '{}', '[]', NULL, NULL, ?, ?)",
                (anidb_id, title, torrents_at_once, now, now)
            )
            return SeriesRow(
                anidb_id=anidb_id,
                title=title,
                state='WORKING',
                wake_at_ms=None,
                torrents_at_once=torrents_at_once,
                torrents=[],
                episodes={},
                recent=[],
                lease_token=None,
                lease_expires_at_ms=None,
                created_at_ms=now,
                updated_at_ms=now,
            )

        series = _row_from_tuple(row)
        series.title = title
        if reopen or series.state not in ('KEPT', 'REJECTED'):
            series.state = 'WORKING'
            series.wake_at_ms = None
        series.updated_at_ms = now

        db.execute(
            "UPDATE series "
            "SET title = ?, state = ?, wake_at_ms = ?, updated_at_ms = ? "
            "WHERE anidb_id = ?",
            (series.title, series.state, series.wake_at_ms, now, anidb_id)
        )
        return series


def add_torrent(db, anidb_id: int, release: dict, now: int | None = None) -> bool:
    """Append to torrents_json once per info hash. Returns True if added, False if duplicate."""
    if now is None:
        now = int(time.time() * 1000)
    infohash = release.get("infohash") or release.get("torrent_infohash") or ""
    if isinstance(infohash, str):
        infohash = infohash.lower().strip()

    raw_body = release.get("raw_body")
    if isinstance(raw_body, (bytes, bytearray)):
        raw_body = raw_body.decode("utf-8", errors="replace")

    with _atomic(db):
        row = db.execute(
            "SELECT torrents_json FROM series WHERE anidb_id = ?",
            (anidb_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Series {anidb_id} does not exist")

        torrents = json.loads(row[0]) if row[0] else []
        for t in torrents:
            if t.get("infohash") == infohash:
                return False  # already present: no-op

        torrent_entry = {
            "infohash": infohash,
            "release_name": release.get("release_name") or release.get("title") or "",
            "raw_body": raw_body,
            "step": release.get("step", "get_metadata"),
            "state": release.get("state", "waiting"),
            "why": release.get("why"),
            "episodes": release.get("episodes", []),
            "files": release.get("files", []),
            "progress": release.get("progress", 0.0),
            "submitted_at_ms": release.get("submitted_at_ms"),
            "identity": release.get("identity"),   # The identity process's series (TVDB ID, title).
        }
        torrents.append(torrent_entry)

        db.execute(
            "UPDATE series SET torrents_json = ?, updated_at_ms = ? WHERE anidb_id = ?",
            (json.dumps(torrents), now, anidb_id)
        )
        return True


def note(row: SeriesRow, line: str, custom_logger: logging.Logger | None = None) -> None:
    """Append to recent_json, keeping the last 50 lines; the same line also goes to the log."""
    row.recent.append(line)
    if len(row.recent) > 50:
        row.recent = row.recent[-50:]
    (custom_logger or logger).info(line)


def get_series(db, anidb_id: int) -> SeriesRow | None:
    """Fetch a series row without leasing it."""
    row = db.execute(
        "SELECT anidb_id, title, state, wake_at_ms, torrents_at_once, "
        "       torrents_json, episodes_json, recent_json, "
        "       lease_token, lease_expires_at_ms, created_at_ms, updated_at_ms, work_json "
        "FROM series WHERE anidb_id = ?",
        (anidb_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_from_tuple(row)


def update_series(db, series: SeriesRow, now: int | None = None) -> None:
    """Directly update a series row (used by administrative actions)."""
    if now is None:
        now = int(time.time() * 1000)
    with _atomic(db):
        db.execute(
            "UPDATE series "
            "SET title = ?, state = ?, wake_at_ms = ?, torrents_at_once = ?, "
            "    torrents_json = ?, episodes_json = ?, recent_json = ?, "
            "    lease_token = ?, lease_expires_at_ms = ?, updated_at_ms = ?, work_json = ? "
            "WHERE anidb_id = ?",
            (
                series.title,
                series.state,
                series.wake_at_ms,
                series.torrents_at_once,
                json.dumps(series.torrents),
                json.dumps(series.episodes),
                json.dumps(series.recent[-50:]),
                series.lease_token,
                series.lease_expires_at_ms,
                now,
                json.dumps(series.work),
                series.anidb_id,
            )
        )

