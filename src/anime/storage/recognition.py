"""Recognized Names: every series (AniDB anime) Anime Discovery understands.

A series is remembered once its identity is verified (IDENTIFY_ANIME), with
its AniDB title and the release-name alias. Episodes are remembered by AniDB
number once a release verifies them; their TVDB placement is an optional
label. Aliases are hints for identity reuse, never episode authorization.
"""
import re

from src.anime.release_names import norm, release_title
from src.anime.providers.http import json_object
from src.anime.providers.errors import ProviderInvalidResponse

class RecognitionConflict(ValueError):
    pass


def _alias(text):
    if type(text) is not str or not 1 <= len(text) <= 512:
        return None
    key = norm(text)
    return key if 3 <= len(key) <= 256 else None


def release_alias(raw_body):
    """The title part of the delivery's release name, or None."""
    try:
        document = json_object(raw_body)
        release = document.get("release_name")
        if type(release) is str:
            parsed, _ = release_title(release)
            if parsed:
                return parsed[0]
    except (ValueError, TypeError, ProviderInvalidResponse):
        pass
    return None


def remember_series(db, *, anidb_id, title, when, tvdb_series_id=None, anime_type=None, aliases=()):
    """Record the series and its aliases, in the caller's transaction.

    A known TVDB series label is never replaced by a different one.
    """
    if not db.in_transaction:
        raise ValueError("Transaction required")
    if type(title) is not str or not 1 <= len(title.strip()) <= 512:
        raise RecognitionConflict("Unsafe title")
    if anime_type is not None and (type(anime_type) is not str or not 1 <= len(anime_type.strip()) <= 64):
        anime_type = None
    known = db.execute("SELECT tvdb_series_id FROM recognized_series WHERE anidb_id=?", (anidb_id,)).fetchone()
    if known is not None and None not in (known[0], tvdb_series_id) and known[0] != tvdb_series_id:
        raise RecognitionConflict("Conflicting TVDB series")
    db.execute(
        "INSERT INTO recognized_series(anidb_id,display_title,tvdb_series_id,anime_type,recognized_at_ms) "
        "VALUES(?,?,?,?,?) ON CONFLICT(anidb_id) DO UPDATE SET "
        "tvdb_series_id=COALESCE(recognized_series.tvdb_series_id,excluded.tvdb_series_id),"
        "anime_type=COALESCE(excluded.anime_type,recognized_series.anime_type)",
        (anidb_id, title, tvdb_series_id, anime_type, when))
    for text in (title, *aliases):
        key = _alias(text)
        if key is not None:
            db.execute("INSERT OR IGNORE INTO recognized_aliases(alias_key,anidb_id) VALUES(?,?)", (key, anidb_id))


def remember_identified(db, *, anidb_id, title, tvdb_series_id, anime_type, raw_body, when):
    """IDENTIFY_ANIME persisted a verified identity: recognize the series."""
    alias = release_alias(raw_body)
    remember_series(db, anidb_id=anidb_id, title=title, when=when, tvdb_series_id=tvdb_series_id,
                    anime_type=anime_type, aliases=(alias,) if alias else ())


def ensure_series(db, anidb_id):
    """A series row for an identified anime (from its identity), for episode rows."""
    if db.execute("SELECT 1 FROM recognized_series WHERE anidb_id=?", (anidb_id,)).fetchone():
        return
    row = db.execute("SELECT title,tvdb_series_id FROM identified_anime WHERE anidb_id=?", (anidb_id,)).fetchone()
    if row is None:
        raise RecognitionConflict("Unidentified anime")
    remember_series(db, anidb_id=anidb_id, title=row[0][:512].strip(), when=0, tvdb_series_id=row[1])


def remember_verified_release(db, *, verification_id, source, season):
    """Within the release-verification transaction, never from webhook hints alone.

    season is the verified TVDB season, or None when the episode has no TVDB
    placement (the series and alias are still recognized).
    """
    if not db.in_transaction:
        raise ValueError("Verification transaction required")
    if season is not None and (type(season) is not int or season < 0):
        raise ValueError("Invalid season")
    proof = db.execute(
        "SELECT v.anidb_id,v.tvdb_series_id,v.tvdb_season,v.verified_at_ms,a.title "
        "FROM release_verifications v JOIN identified_anime a ON a.anidb_id=v.anidb_id "
        "WHERE v.verification_id=?", (verification_id,)).fetchone()
    if proof is None or proof[:3] != (source.anidb_id, source.tvdb_series_id, season):
        raise RecognitionConflict("Verification provenance mismatch")
    aid, tid, _, when, title = proof
    if not 1 <= len(title.strip()) <= 512:
        raise RecognitionConflict("Unsafe verified title")
    alias = release_alias(source.raw_body)
    remember_series(db, anidb_id=aid, title=title, when=when, tvdb_series_id=tid,
                    aliases=(alias,) if alias else ())
    if season is not None:
        # The TVDB season label of this anime.
        db.execute(
            "INSERT OR IGNORE INTO recognized_seasons "
            "(tvdb_series_id,tvdb_season,anidb_id,display_title,evidence_verification_id,recognized_at_ms) "
            "VALUES(?,?,?,?,?,?)", (tid, season, aid, title, verification_id, when))


# Release tags that name the AniDB anime type; they narrow an alias shared by
# the TV series, its OVA and its movie.
_TYPE_TAGS = ((re.compile(r"\b(?:OVA|OAD)\b", re.I), "OVA"),
              (re.compile(r"\b(?:Movie|Gekijouban|Gekijoban)\b", re.I), "Movie"),
              (re.compile(r"\bONA\b", re.I), "Web"))


def _release_type(name):
    found = {kind for pattern, kind in _TYPE_TAGS if pattern.search(name)}
    return found.pop() if len(found) == 1 else None


def recognized_for_release(db, name, *, season=None):
    """The unique recognized series for a release name, or None.

    Exact normalized alias lookup. A season marker narrows to anime whose
    verified TVDB season it is; an OVA, movie or ONA tag narrows to that AniDB
    type. Anything still matching more than one anime fails closed.
    """
    aliases, _ = release_title(name)
    if not aliases:
        return None
    key = _alias(aliases[0])
    if key is None:
        return None
    if season is not None and (type(season) is not int or season < 0):
        return None
    rows = db.execute(
        "SELECT s.anidb_id,s.tvdb_series_id,s.display_title,s.anime_type "
        "FROM recognized_aliases a JOIN recognized_series s USING(anidb_id) WHERE a.alias_key=?",
        (key,)).fetchall()
    if season is not None and rows:
        labelled = {row[0] for row in db.execute(
            "SELECT anidb_id FROM recognized_seasons WHERE tvdb_season=? AND anidb_id IN (%s)"
            % ",".join("?" * len(rows)), (season, *(row[0] for row in rows))).fetchall()}
        rows = [row for row in rows if row[0] in labelled]
    kind = _release_type(name)
    if kind is not None:
        rows = [row for row in rows if row[3] == kind]
    if len(rows) != 1:
        return None
    aid, tid, title, _ = rows[0]
    return {"tvdb_series_id": tid, "tvdb_season": season, "anidb_id": aid, "title": title}


def remember_verified_episode(db, *, verification_id):
    """Record a uniquely verified AniDB episode; its TVDB placement is a label.

    Called in the same worker transaction that persists release verification.
    A placement found later fills in an episode recorded without one.
    Conflicting mappings fail closed and roll back; never silently replace.
    """
    if not db.in_transaction:
        raise ValueError("Verification transaction required")
    proof = db.execute(
        "SELECT v.tvdb_series_id,v.tvdb_season,v.anidb_id,v.anidb_episode_number,"
        "v.anidb_episode_id,v.tvdb_episode,v.tvdb_episode_id,v.verified_at_ms "
        "FROM release_verifications v WHERE v.verification_id=?",
        (verification_id,)).fetchone()
    if proof is None:
        raise RecognitionConflict("Missing episode verification")
    tid, season, aid, number, eid, episode, teid, when = proof
    if teid is None:
        tid = season = episode = None
    ensure_series(db, aid)
    existing = db.execute(
        "SELECT tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,"
        "anidb_episode_id,tvdb_episode,tvdb_episode_id "
        "FROM recognized_episodes WHERE (anidb_id=? AND anidb_episode_number=?) "
        "OR anidb_episode_id=? OR (tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=?) "
        "OR tvdb_episode_id=?",
        (aid, number, eid, tid, season, episode, teid)).fetchall()
    expected = (tid, season, aid, number, eid, episode, teid)
    for row in existing:
        if row[2:5] != expected[2:5]:
            raise RecognitionConflict("Conflicting episode placement")
        if teid is not None and row[6] is not None and row != expected:
            raise RecognitionConflict("Conflicting episode placement")
    if existing:
        if teid is None or existing[0][6] is not None:
            return False
        db.execute(
            "UPDATE recognized_episodes SET tvdb_series_id=?,tvdb_season=?,tvdb_episode=?,tvdb_episode_id=? "
            "WHERE anidb_id=? AND anidb_episode_number=? AND tvdb_episode_id IS NULL",
            (tid, season, episode, teid, aid, number))
        return True
    db.execute(
        "INSERT INTO recognized_episodes "
        "(tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,"
        "anidb_episode_id,tvdb_episode,tvdb_episode_id,evidence_verification_id,recognized_at_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (*expected, verification_id, when))
    return True


def recognized_episode(db, *, aid, tid, origin, season, number):
    """Return a unique exact placement only; never derive offsets or ranges."""
    if (type(aid) is not int or aid<=0 or type(tid) is not int or tid<=0
            or type(number) is not int or number<=0 or origin not in ("anidb","tvdb")
            or (season is not None and (type(season) is not int or season<0))):
        return None
    if origin=="tvdb":
        if season is None:
            return None
        query=("SELECT anidb_episode_number,anidb_episode_id,tvdb_season,tvdb_episode,tvdb_episode_id "
               "FROM recognized_episodes WHERE anidb_id=? AND tvdb_series_id=? "
               "AND tvdb_season=? AND tvdb_episode=?")
        args=(aid,tid,season,number)
    else:
        query=("SELECT anidb_episode_number,anidb_episode_id,tvdb_season,tvdb_episode,tvdb_episode_id "
               "FROM recognized_episodes WHERE anidb_id=? AND tvdb_series_id=? "
               "AND anidb_episode_number=? AND tvdb_episode_id IS NOT NULL")
        args=(aid,tid,number)
    rows=db.execute(query,args).fetchall()
    if len(rows)!=1:
        return None
    an,ae,ts,te,tid_episode=rows[0]
    return {"anidb_episode_number":an,"anidb_episode_id":ae,
            "tvdb_season":ts,"tvdb_episode":te,"tvdb_episode_id":tid_episode}
