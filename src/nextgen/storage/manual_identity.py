"""Operator-confirmed, season-scoped series identification hints.

These are explicit manual choices, not provider-verified episode mappings.
The existing release-verification job still owns episode placement.
"""
import json
from src.nextgen.release_names import release_title
from src.nextgen.release_names import norm
from src.nextgen.batch.recognition import release_scope_hint
from src.nextgen.storage.job_queue import JobQueue, QueueUnavailable
from src.nextgen.storage.sqlite import transaction


class ManualMatchError(ValueError):
    pass


def _key(release):
    aliases, _ = release_title(release)
    if not aliases:
        raise ManualMatchError("Unable to extract an anime title from this release")
    key = norm(aliases[0])
    if not 3 <= len(key) <= 256:
        raise ManualMatchError("Invalid anime title")
    return key


def _scope(release):
    """Reuse the existing intake classification; unknown stays unnumbered."""
    hint = release_scope_hint(release)
    if hint.kind == "ambiguous":
        return None
    return hint.season_hint if hint.kind in ("single_episode", "season_pack", "mixed_pack") else (
        0 if hint.kind == "special" and hint.season_hint == 0 else -1)


def for_release(database, release, season=None):
    scope = _scope(release)
    if scope is None:
        return None
    if season is not None and season != scope:
        return None
    try:
        key = _key(release)
    except ManualMatchError:
        return None
    try:
        with JobQueue(database).connection() as db:
            row = db.execute(
                "SELECT anidb_id,tvdb_series_id,title FROM manual_identity_matches "
                "WHERE alias_key=? AND tvdb_season=?", (key, scope)).fetchone()
    except QueueUnavailable:
        return None
    if row is None:
        return None
    return {"schema_version": 2, "anime": {
        "anidb_id": row[0], "tvdb_series_id": row[1], "title": row[2]},
        "episodes": [], "attributes": {}}


def save(database, job_id, *, anidb_id, tvdb_series_id, title, season):
    if (type(anidb_id) is not int or not 0 < anidb_id < (1 << 63)
            or type(tvdb_series_id) is not int or not 0 < tvdb_series_id < (1 << 63)
            or (season is not None and (type(season) is not int or not 0 <= season <= 999))
            or type(title) is not str or not 1 <= len(title.strip()) <= 512
            or any(ord(c) < 32 or ord(c) == 127 for c in title)):
        raise ManualMatchError("Invalid identity fields")
    with JobQueue(database).connection() as db, transaction(db):
        row = db.execute(
            "SELECT j.kind,j.state,e.raw_body FROM jobs j "
            "JOIN incoming_events e ON e.event_id=j.event_id WHERE j.job_id=?",
            (job_id,)).fetchone()
        if row is None or row[0] != "IDENTIFY_ANIME" or row[1] != "NEEDS_REVIEW":
            raise ManualMatchError("Only unidentified jobs awaiting review can be corrected")
        try:
            raw = json.loads(row[2])
            release = raw["release_name"]
            if type(release) is not str:
                raise ValueError()
            key = _key(release)
            scope = _scope(release)
            if scope is None:
                raise ManualMatchError('Ambiguous release; cannot reuse automatically')
            if season is not None and season != scope:
                raise ManualMatchError('Season conflicts with parsed release')
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ManualMatchError("Release name unavailable") from None
        prior = db.execute(
            "SELECT anidb_id,tvdb_series_id,title FROM manual_identity_matches "
            "WHERE alias_key=? AND tvdb_season=?", (key, scope)).fetchone()
        wanted = (anidb_id, tvdb_series_id, title.strip())
        if prior is not None and prior != wanted:
            raise ManualMatchError("An existing manual match differs; edit the existing match first")
        db.execute(
            "INSERT OR IGNORE INTO manual_identity_matches "
            "(alias_key,tvdb_season,anidb_id,tvdb_series_id,title) VALUES(?,?,?,?,?)",
            (key, scope, *wanted))
    return {"job_id": job_id, "alias": key, "season": season, "saved": True}
