"""Verified season-level Recognized Names memory.

Only a successfully persisted, exact episode verification can seed identity
memory. Aliases are hints for identity reuse, never episode authorization.
"""
from src.nextgen.release_names import norm, release_title
from src.nextgen.providers.http import json_object
from src.nextgen.providers.errors import ProviderInvalidResponse

class RecognitionConflict(ValueError):
    pass


def _alias(text):
    if type(text) is not str or not 1 <= len(text) <= 512:
        return None
    key = norm(text)
    return key if 3 <= len(key) <= 256 else None


def remember_verified_release(db, *, verification_id, source, season):
    """Within the release-verification transaction, never from webhook hints alone."""
    if not db.in_transaction:
        raise ValueError("Verification transaction required")
    if type(season) is not int or season < 0:
        raise ValueError("Invalid season")
    proof = db.execute(
        "SELECT v.anidb_id,v.tvdb_series_id,v.tvdb_season,v.verified_at_ms,a.title "
        "FROM release_verifications v JOIN identified_anime a ON a.anidb_id=v.anidb_id "
        "WHERE v.verification_id=?", (verification_id,)).fetchone()
    if proof is None or proof[:3] != (source.anidb_id, source.tvdb_series_id, season):
        raise RecognitionConflict("Verification provenance mismatch")
    aid,tid,_,when,title = proof
    if not 1 <= len(title.strip()) <= 512:
        raise RecognitionConflict("Unsafe verified title")
    db.execute(
        "INSERT OR IGNORE INTO recognized_seasons "
        "(tvdb_series_id,tvdb_season,anidb_id,display_title,evidence_verification_id,recognized_at_ms) "
        "VALUES(?,?,?,?,?,?)",(tid,season,aid,title,verification_id,when))
    aliases = [title]
    try:
        document=json_object(source.raw_body)
        release=document.get("release_name")
        if type(release) is str:
            parsed,_ = release_title(release)
            if parsed:
                aliases.append(parsed[0])
    except (ValueError,TypeError,ProviderInvalidResponse):
        pass
    for text in aliases:
        key=_alias(text)
        if key is None:
            continue
        db.execute(
            "INSERT OR IGNORE INTO recognized_aliases(alias_key,tvdb_season,tvdb_series_id,anidb_id) "
            "VALUES(?,?,?,?)",(key,season,tid,aid))


def recognized_for_release(db, name, *, season=None):
    """Exact normalized lookup. Ambiguity fails closed; never guesses seasons."""
    aliases,_=release_title(name)
    if not aliases:
        return None
    key=_alias(aliases[0])
    if key is None:
        return None
    if season is not None and (type(season) is not int or season<0):
        return None
    rows=db.execute(
        "SELECT s.tvdb_series_id,s.tvdb_season,s.anidb_id,s.display_title "
        "FROM recognized_aliases a JOIN recognized_seasons s "
        "ON s.tvdb_series_id=a.tvdb_series_id AND s.tvdb_season=a.tvdb_season AND s.anidb_id=a.anidb_id "
        "WHERE a.alias_key=?" + (" AND a.tvdb_season=?" if season is not None else ""),
        (key,season) if season is not None else (key,)).fetchall()
    if len(rows)!=1:
        return None
    tid,selected_season,aid,title=rows[0]
    return {"tvdb_series_id":tid,"tvdb_season":selected_season,
            "anidb_id":aid,"title":title}


def remember_verified_episode(db, *, verification_id):
    """Bind a uniquely verified episode to its already recognized TVDB season.

    Called in the same worker transaction that persists release verification.
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
    tid,season,aid,number,eid,episode,teid,when = proof
    parent = db.execute(
        "SELECT 1 FROM recognized_seasons WHERE tvdb_series_id=? AND tvdb_season=? AND anidb_id=?",
        (tid,season,aid)).fetchone()
    if parent is None:
        raise RecognitionConflict("Unrecognized parent season")
    existing = db.execute(
        "SELECT tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,"
        "anidb_episode_id,tvdb_episode,tvdb_episode_id "
        "FROM recognized_episodes WHERE (anidb_id=? AND anidb_episode_number=?) "
        "OR anidb_episode_id=? OR (tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=?) "
        "OR tvdb_episode_id=?",
        (aid,number,eid,tid,season,episode,teid)).fetchall()
    expected = (tid,season,aid,number,eid,episode,teid)
    if any(row != expected for row in existing):
        raise RecognitionConflict("Conflicting episode placement")
    if existing:
        return False
    db.execute(
        "INSERT INTO recognized_episodes "
        "(tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,"
        "anidb_episode_id,tvdb_episode,tvdb_episode_id,evidence_verification_id,recognized_at_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (*expected,verification_id,when))
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
               "AND anidb_episode_number=?")
        args=(aid,tid,number)
    rows=db.execute(query,args).fetchall()
    if len(rows)!=1:
        return None
    an,ae,ts,te,tid_episode=rows[0]
    return {"anidb_episode_number":an,"anidb_episode_id":ae,
            "tvdb_season":ts,"tvdb_episode":te,"tvdb_episode_id":tid_episode}
