"""Read source context and persist declared-episode facts under worker ownership."""
import time
import sqlite3

from src.anime.history import integer
from src.anime.storage.recognition import remember_verified_release, remember_verified_episode, RecognitionConflict
from src.anime.job_contracts import parse_payload
from src.anime.release_contracts import ReleaseInput, VerifiedRelease, ReleaseConflict, ReleaseNotReady
from src.anime.schema.anime_series import AnimeSeries
from src.anime.storage.job_queue import QueueUnavailable
from src.anime.storage.migrations import LATEST_VERSION, MigrationError, _open


def _source(db, event_id, aid):
    row=db.execute("SELECT r.result_id,CASE WHEN length(CAST(r.series_json AS BLOB))<=65536 THEN r.series_json ELSE '' END,"
                   "CASE WHEN length(e.raw_body)<=65536 THEN e.raw_body ELSE NULL END,e.state "
                   "FROM identity_results r JOIN incoming_events e ON e.event_id=r.event_id "
                   "WHERE r.event_id=? AND r.anidb_id=?",(event_id,aid)).fetchone()
    if row is None or row[3]!="IDENTIFIED": raise ReleaseNotReady()
    try:
        series=AnimeSeries.from_dict(parse_payload(row[1]))
        if series.anime.anidb_id!=aid: raise ReleaseNotReady()
        return ReleaseInput(event_id,row[0],series.anime.anidb_id,series.anime.tvdb_series_id,row[2])
    except (ValueError,TypeError,RecursionError):
        raise ReleaseNotReady() from None


def load_release(database, event_id, aid):
    integer(event_id,positive=True)
    integer(aid,positive=True)
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0]!=LATEST_VERSION: raise QueueUnavailable()
        return _source(db,event_id,aid)
    except (OSError,sqlite3.Error,MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None: db.close()


def persist_release(db, claim, value):
    """No transaction ownership, logging or successor decisions here."""
    if not db.in_transaction or type(value) is not VerifiedRelease:
        raise ValueError("worker-owned release transaction and typed facts required")
    source=value.source
    if claim.event_id!=source.event_id: raise ReleaseConflict()
    current=_source(db,source.event_id,source.anidb_id)
    if current!=source: raise ReleaseConflict()
    origin=db.execute("SELECT source FROM incoming_events WHERE event_id=?",
                      (source.event_id,)).fetchone()
    if origin==('release-discovery',):
        from src.anime.storage.release_selection import require_verified_delivery
        require_verified_delivery(db,value)
    if origin==('manual-release',):
        # The user-selected placement is a request, not a verified identity.
        # Provider verification must resolve EXACTLY that episode before any
        # downstream reservation or torrent ownership can begin.
        target=db.execute(
            'SELECT anidb_id,tvdb_season,tvdb_episode FROM manual_release_submissions '
            'WHERE event_id=?',(source.event_id,)).fetchone()
        if target != (source.anidb_id,value.tvdb_season,value.tvdb_episode):
            raise ReleaseConflict()
    stored=db.execute("SELECT tvdb_series_id FROM identified_anime WHERE anidb_id=?",(source.anidb_id,)).fetchone()
    if stored is None or stored[0]!=source.tvdb_series_id: raise ReleaseConflict()
    owner=db.execute("SELECT anidb_id FROM identified_episodes WHERE anidb_episode_id=?",(value.anidb_episode_id,)).fetchone()
    if owner is not None and owner[0]!=source.anidb_id: raise ReleaseConflict()
    # No mutation of the original, possibly series-only AnimeSeries snapshot.
    db.execute("INSERT OR IGNORE INTO identified_episodes VALUES(?,?)",(value.anidb_episode_id,source.anidb_id))
    cursor=db.execute("INSERT INTO release_verifications(job_id,identity_result_id,anidb_id,release_sha256,verification_scope,"
               "anidb_episode_number,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms) "
               "VALUES(?,?,?,?,'DECLARED_SINGLE_EPISODE',?,?,?,?,?,?,?)",
               (claim.job_id,source.identity_result_id,source.anidb_id,source.release_sha256,
                value.anidb_episode_number,value.anidb_episode_id,source.tvdb_series_id,value.tvdb_season,
                value.tvdb_episode,value.tvdb_episode_id,integer(time.time_ns()//1000000)))

    if value.tvdb_season is not None and value.tvdb_episode is not None and value.tvdb_episode_id is not None:
        try:
            remember_verified_release(db,verification_id=cursor.lastrowid,source=source,season=value.tvdb_season)
            remember_verified_episode(db,verification_id=cursor.lastrowid)
        except RecognitionConflict:
            # Contradictory season identity is not silently reclassified.
            raise ReleaseConflict() from None
