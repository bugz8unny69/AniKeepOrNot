"""Fenced, additive batch-verification proof persistence; no side effects.

This module is not wired into VERIFY_RELEASE yet. The future worker must
call persist_batch_verification in its existing leased completion transaction
and must validate a trusted provider result before that call.
"""
from pathlib import PurePosixPath
import json

from src.nextgen.acquisition.metadata import BatchInventory, ObservedBatchInventory, infohash
from src.nextgen.batch.verification import VerifiedBatch
from src.nextgen.batch.selection import VIDEO, file_episode_number
from src.nextgen.history import integer
from src.nextgen.release_contracts import ReleaseInput, ReleaseConflict
from src.nextgen.storage.releases import _source


def persist_batch_verification(db, claim, source, inventory, verified, *, now_ms):
    """Insert immutable per-file batch facts under a live VERIFY_RELEASE lease.

    A caller must already hold the worker-owned SQLite transaction. The
    worker is responsible for invoking this only after provider corroboration;
    this function checks the ownership, source event and full inventory.
    """
    if not db.in_transaction:
        raise ValueError('Worker-owned transaction required')
    if type(source) is not ReleaseInput or type(inventory) not in (BatchInventory,ObservedBatchInventory) or type(verified) is not VerifiedBatch:
        raise ValueError('Typed batch source, inventory and proof required')
    now_ms=integer(now_ms)
    if (claim.kind!='VERIFY_RELEASE' or claim.event_id!=source.event_id
            or verified.anidb_id!=source.anidb_id or verified.tvdb_series_id!=source.tvdb_series_id
            or source.identity_result_id<=0 or verified.season<1):
        raise ReleaseConflict()
    if (inventory.info_hash,inventory.info_sha256)!=(verified.info_hash,verified.info_sha256):
        raise ReleaseConflict()
    if _source(db,source.event_id,source.anidb_id)!=source:
        raise ReleaseConflict()
    row=db.execute(
        "SELECT state,attempt_count,lease_token,lease_expires_at_ms "
        "FROM jobs WHERE job_id=? AND event_id=? AND kind='VERIFY_RELEASE'",
        (claim.job_id,source.event_id)).fetchone()
    if (row is None or row[0]!='RUNNING' or row[1]!=claim.attempt
            or row[2]!=claim.token or row[3] is None or row[3]<=now_ms):
        raise ReleaseConflict()
    try:
        declaration=json.loads(source.raw_body)
        incoming_hash=declaration.get('torrent_infohash')
    except (ValueError,TypeError,AttributeError,UnicodeDecodeError):
        raise ReleaseConflict() from None
    try:
        if infohash(incoming_hash)!=inventory.info_hash:
            raise ReleaseConflict()
    except ValueError:
        raise ReleaseConflict() from None
    if db.execute("SELECT 1 FROM release_verifications WHERE job_id=?",(claim.job_id,)).fetchone():
        raise ReleaseConflict()
    if db.execute("SELECT 1 FROM batch_verifications WHERE job_id=?",(claim.job_id,)).fetchone():
        raise ReleaseConflict()
    regular={}
    by_index={item.index:item for item in inventory.files}
    for item in inventory.files:
        if PurePosixPath(item.path).suffix.lower() in VIDEO:
            number=file_episode_number(item.path,verified.season)
            if number is not None:regular[item.index]=number
    if not regular or len(regular)!=len(verified.files):
        raise ReleaseConflict()
    seen_anidb,seen_tvdb=set(),set()
    for fact in verified.files:
        raw=by_index.get(fact.index)
        if (raw is None or fact.index not in regular or fact.path!=raw.path
                or fact.release_episode!=regular[fact.index]
                or fact.tvdb_season!=verified.season or fact.anidb_episode_number<1
                or fact.anidb_episode_id<1 or fact.tvdb_episode<1 or fact.tvdb_episode_id<1
                or fact.anidb_episode_id in seen_anidb or fact.tvdb_episode in seen_tvdb):
            raise ReleaseConflict()
        seen_anidb.add(fact.anidb_episode_id)
        seen_tvdb.add(fact.tvdb_episode)
    cursor=db.execute(
        "INSERT INTO batch_verifications(job_id,identity_result_id,anidb_id,"
        "tvdb_series_id,tvdb_season,release_sha256,info_hash,info_sha256,verified_at_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (claim.job_id,source.identity_result_id,verified.anidb_id,
         verified.tvdb_series_id,verified.season,source.release_sha256,
         inventory.info_hash,inventory.info_sha256,now_ms))
    batch_id=cursor.lastrowid
    for fact in verified.files:
        raw=by_index[fact.index]
        db.execute(
            "INSERT INTO batch_verified_files(batch_id,file_index,file_path,file_size_bytes,"
            "release_episode,anidb_episode_number,anidb_episode_id,tvdb_season,tvdb_episode,tvdb_episode_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (batch_id,fact.index,fact.path,raw.size,fact.release_episode,
             fact.anidb_episode_number,fact.anidb_episode_id,fact.tvdb_season,
             fact.tvdb_episode,fact.tvdb_episode_id))
    return batch_id
