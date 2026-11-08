"""Authenticated manual episode candidates. Reuse existing VERIFY_RELEASE pipeline.

A submission creates a distinct incoming event and acquisition operation.
The exclusive episode reservation is still granted only by RESERVE_EPISODE:
multiple manual candidates can wait without claiming or downloading the
same episode simultaneously.
"""
import base64
import binascii
import hashlib
import json
import sqlite3
import time
from pathlib import PurePosixPath

from src.anime.acquisition.metadata import (
    _Decoder, MAX_METAINFO, MetadataInvalid, hash_magnet, parse_metainfo)
from src.anime.acquisition.prowlarr_search import _magnet_hash
from src.anime.history import append_event_history
from src.anime.job_contracts import parse_payload
from src.anime.providers.release_mapping import declared_episode
from src.anime.release_contracts import ReleaseNotReady
from src.anime.storage.job_queue import JobQueue, QueueUnavailable
from src.anime.storage.sqlite import enqueue_job_with_receipt, persist_identity, transaction
from src.anime.storage.series_intake import plan_window, _placements


class ManualSubmissionInvalid(ValueError):
    """A submission the API refuses; reason_code says why, for the person submitting."""
    def __init__(self, reason_code='API_REQUEST_INVALID'):
        super().__init__(reason_code)
        self.reason_code = reason_code


SOURCE_INVALID = 'MANUAL_RELEASE_SOURCE_INVALID'
TITLE_UNREADABLE = 'MANUAL_RELEASE_TITLE_UNREADABLE'
EPISODE_MISMATCH = 'MANUAL_RELEASE_EPISODE_MISMATCH'


class ManualSubmissionUnavailable(ValueError):
    pass


def _metainfo(raw):
    """Compute the v1 hash over the original info bytes before full validation."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_METAINFO:
        raise ManualSubmissionInvalid(SOURCE_INVALID)
    try:
        decoder = _Decoder(raw)
        root = decoder.value()
        if (type(root) is not dict or decoder.pos != len(raw)
                or decoder.info_span is None):
            raise ManualSubmissionInvalid(SOURCE_INVALID)
        a, b = decoder.info_span
        digest = hashlib.sha1(raw[a:b]).hexdigest()
        manifest = parse_metainfo(raw, digest)
        return digest, manifest
    except (MetadataInvalid, TypeError, ValueError, KeyError, IndexError, RecursionError):
        raise ManualSubmissionInvalid(SOURCE_INVALID) from None


def _admit_release(name, season, episode, known_anidb=None):
    if (type(name) is not str or not 1 <= len(name.strip()) <= 512
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ManualSubmissionInvalid(TITLE_UNREADABLE)
    try:
        origin, declared_season, declared_episode_number = declared_episode(name)
    except (ReleaseNotReady, ValueError, TypeError):
        raise ManualSubmissionInvalid(TITLE_UNREADABLE) from None
    if origin == 'tvdb':
        if (declared_season, declared_episode_number) != (season, episode):
            raise ManualSubmissionInvalid(EPISODE_MISMATCH)
    elif known_anidb is None:
        # An unrecognized AniDB number cannot establish the requested TVDB
        # placement; a user can supply an explicit SxxEyy release title.
        raise ManualSubmissionInvalid(TITLE_UNREADABLE)
    elif declared_episode_number != known_anidb:
        raise ManualSubmissionInvalid(EPISODE_MISMATCH)


def _candidate(db, aid, season, episode):
    row = db.execute(
        'SELECT p.plan_id,p.tvdb_series_id,p.anchor_episode,p.backfill_count,p.following_count '
        'FROM user_evaluation_plans p WHERE p.anidb_id=?', (aid,)).fetchone()
    if row is None:
        raise ManualSubmissionUnavailable()
    plan_id, tvdb_id, anchor, backfill, following = row
    # The window is in AniDB numbers; the TVDB placement must map to one of them.
    number = next((n for n, place in _placements(db, aid, plan_id).items()
                   if place == (season, episode)), None)
    if number is None or (number != anchor and
                          number not in plan_window(db, plan_id, anchor, backfill, following)):
        raise ManualSubmissionUnavailable()
    if db.execute('SELECT 1 FROM anime_review_decisions WHERE anidb_id=?', (aid,)).fetchone():
        raise ManualSubmissionUnavailable()
    # Never add a candidate for an episode that is already staged.
    if db.execute(
        "SELECT 1 FROM staging_publications s "
        "JOIN acquisition_plans p USING(plan_id) "
        "JOIN episode_reservations r USING(reservation_id) "
        "WHERE s.state='PUBLISHED' AND r.anidb_id=? AND r.tvdb_season=? AND r.tvdb_episode=? "
        'LIMIT 1', (aid, season, episode)).fetchone():
        raise ManualSubmissionUnavailable()
    if db.execute(
        "SELECT 1 FROM batch_staging_publications s "
        "JOIN batch_episode_reservations r ON r.plan_id=s.plan_id AND r.file_index=s.file_index "
        "WHERE s.state='PUBLISHED' AND r.anidb_id=? AND r.tvdb_season=? AND r.tvdb_episode=? "
        'LIMIT 1', (aid, season, episode)).fetchone():
        raise ManualSubmissionUnavailable()
    known = db.execute(
        'SELECT r.anidb_episode_number FROM recognized_episodes e '
        'JOIN release_verifications r ON r.verification_id=e.evidence_verification_id '
        'WHERE e.anidb_id=? AND e.tvdb_series_id=? AND e.tvdb_season=? AND e.tvdb_episode=?',
        (aid, tvdb_id, season, episode)).fetchone()
    return tvdb_id, known[0] if known else None


def submit(database, aid, season, episode, *, release_name, magnet_uri=None, torrent_b64=None):
    """Create an independent manual candidate with existing per-job controls."""
    if (any(type(value) is not int for value in (aid,season,episode))
            or aid < 1 or season < 0 or not 1 <= episode <= 1000
            or (magnet_uri is None) == (torrent_b64 is None)):
        raise ManualSubmissionInvalid()
    if magnet_uri is not None:
        digest = _magnet_hash(magnet_uri)
        if digest is None:
            raise ManualSubmissionInvalid(SOURCE_INVALID)
        magnet = hash_magnet(digest, magnet_uri)
        if magnet is None:
            raise ManualSubmissionInvalid(SOURCE_INVALID)
        metainfo = None
        method = 'magnet'
    else:
        if (type(torrent_b64) is not str
                or len(torrent_b64) > ((MAX_METAINFO + 2)//3)*4):
            raise ManualSubmissionInvalid(SOURCE_INVALID)
        try:
            metainfo = base64.b64decode(torrent_b64, validate=True)
        except (ValueError, binascii.Error):
            raise ManualSubmissionInvalid(SOURCE_INVALID) from None
        digest, manifest = _metainfo(metainfo)
        # The file inventory must independently identify the requested episode.
        # (The selected release title alone cannot authorize a different file.)
        file_title = PurePosixPath(manifest.files[manifest.selected_index].path).stem
        magnet = hash_magnet(digest)
        method = 'torrent'
    queue = JobQueue(database)
    with queue.connection() as db, transaction(db):
        tvdb_id, known_anidb = _candidate(db,aid,season,episode)
        _admit_release(release_name,season,episode,known_anidb)
        if metainfo is not None:
            _admit_release(file_title,season,episode,known_anidb)
        existing = db.execute(
            'SELECT s.event_id,s.verify_job_id FROM manual_release_submissions s '
            'WHERE s.anidb_id=? AND s.tvdb_season=? AND s.tvdb_episode=? AND s.info_hash=?',
            (aid,season,episode,digest)).fetchone()
        if existing is not None:
            return {'event_id':existing[0],'job_id':existing[1],'reused':True}
        anchor = db.execute(
            'SELECT i.series_json FROM user_evaluation_plans p '
            'JOIN release_verifications r ON r.verification_id=p.anchor_verification_id '
            'JOIN identity_results i ON i.result_id=r.identity_result_id '
            'WHERE p.anidb_id=? AND p.tvdb_series_id=? '
            'ORDER BY i.validated_at_ms DESC LIMIT 1',
            (aid,tvdb_id)).fetchone()
        if anchor is None:
            raise ManualSubmissionUnavailable()
        document = parse_payload(anchor[0])
        if (document.get('anime',{}).get('anidb_id') != aid or
                document['anime'].get('tvdb_series_id') != tvdb_id):
            raise ManualSubmissionUnavailable()
        body = json.dumps({
            'release_name':release_name.strip(),
            'source_release_name':file_title if metainfo is not None else release_name.strip(),
            'torrent_infohash':digest, 'magnet_uri':magnet
        }, separators=(',',':'),sort_keys=True).encode('utf-8')
        if len(body) > 65536:
            raise ManualSubmissionInvalid()
        now = time.time_ns()//1_000_000
        eid = db.execute(
            'INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES(?,?,?)',
            ('manual-release',body,now)).lastrowid
        append_event_history(db,event_id=eid,event_code='ACCEPTED',component='api',occurred_at_ms=now)
        persist_identity(db,eid,document,validated_at_ms=now)
        job_id,_ = enqueue_job_with_receipt(
            db,kind='VERIFY_RELEASE',payload={'anidb_id':aid},
            component='api',event_id=eid)
        db.execute(
            'INSERT INTO manual_release_submissions '
            '(event_id,verify_job_id,anidb_id,tvdb_season,tvdb_episode,info_hash,source,metainfo,created_at_ms) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (eid,job_id,aid,season,episode,digest,method,metainfo,now))
        return {'event_id':eid,'job_id':job_id,'reused':False}


def metainfo_for(database, event_id, digest):
    """The original uploaded BEP3 bytes; never supplied through a job payload."""
    if type(event_id) is not int or event_id < 1:
        return None
    try:
        with JobQueue(database).connection() as db:
            row = db.execute(
                'SELECT metainfo FROM manual_release_submissions '
                'WHERE event_id=? AND info_hash=? AND source=\'torrent\'',
                (event_id,digest)).fetchone()
        return None if row is None else bytes(row[0])
    except (QueueUnavailable, OSError, sqlite3.Error):
        return None
