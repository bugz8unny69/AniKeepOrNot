"""Read-only: every ranked release tried for one backfill episode, and why it stopped.

Used by the job History view so a person can see why earlier releases were
rejected without querying SQLite. Nothing here changes state.
"""
import json

from src.anime.history import trace_id

KIND = 'DISCOVER_EPISODE_RELEASES'
STATES = {'READY': 'NOT_TRIED', 'ACTIVE': 'IN_PROGRESS', 'FAILED': 'FAILED',
          'SUCCEEDED': 'STAGED'}


def _body(raw):
    try:
        value = json.loads(bytes(raw).decode('utf-8'))
        return value if type(value) is dict else {}
    except (ValueError, TypeError, UnicodeError):
        return {}


def _episode(db, job_id):
    """(plan_id, episode) for a job in a backfill episode, or None."""
    row = db.execute('SELECT j.kind,j.payload_json,e.source,e.raw_body,j.event_id FROM jobs j '
                     'LEFT JOIN incoming_events e ON e.event_id=j.event_id WHERE j.job_id=?',
                     (job_id,)).fetchone()
    if row is None: return None
    kind, payload, source, raw, event_id = row
    if kind == KIND:
        try: data = json.loads(payload or '{}')
        except ValueError: return None
        episode = next((data[k] for k in ('fallback_episode', 'exhausted_episode', 'retry_episode')
                        if type(data.get(k)) is int), None)
        return (data['plan_id'], episode) if episode and type(data.get('plan_id')) is int else None
    if source != 'release-discovery' or raw is None: return None
    digest = _body(raw).get('torrent_infohash')
    parent = db.execute("SELECT parent.payload_json FROM jobs verify JOIN jobs parent "
                        "ON parent.job_id=verify.created_by_job_id AND parent.kind=? "
                        "WHERE verify.event_id=? AND verify.kind='VERIFY_RELEASE' LIMIT 1",
                        (KIND, event_id)).fetchone()
    if parent is None or type(digest) is not str: return None
    try: plan_id = json.loads(parent[0])['plan_id']
    except (ValueError, KeyError, TypeError): return None
    found = db.execute('SELECT anidb_episode_number FROM episode_release_candidates '
                       'WHERE plan_id=? AND info_hash=?', (plan_id, digest.lower())).fetchone()
    return (plan_id, found[0]) if found else None


def _deliveries(db, plan_id):
    """info hash -> (event_id, release name) for this plan's backfill deliveries."""
    rows = db.execute("SELECT e.event_id,e.raw_body FROM incoming_events e "
                      "JOIN jobs verify ON verify.event_id=e.event_id AND verify.kind='VERIFY_RELEASE' "
                      "JOIN jobs parent ON parent.job_id=verify.created_by_job_id AND parent.kind=? "
                      "WHERE e.source='release-discovery' "
                      "AND json_extract(parent.payload_json,'$.plan_id')=? ORDER BY e.event_id",
                      (KIND, plan_id)).fetchall()
    found = {}
    for event_id, raw in rows:
        body = _body(raw)
        digest = body.get('torrent_infohash')
        if type(digest) is str:
            name = body.get('source_release_name') or body.get('release_name')
            found[digest.lower()] = (event_id, name if type(name) is str else None)
    return found


def _stop(db, event_id):
    """The latest review stop of this attempt: (reason code, details)."""
    row = db.execute("SELECT h.reason_code,h.details_json FROM job_history h JOIN jobs j USING(job_id) "
                     "WHERE j.event_id=? AND h.event_code='NEEDS_REVIEW' "
                     "ORDER BY h.history_id DESC LIMIT 1", (event_id,)).fetchone()
    if row is None: return None, {}
    try: details = json.loads(row[1] or '{}')
    except ValueError: details = {}
    return row[0], details if type(details) is dict else {}


def backfill_attempts(db, job_id):
    """Ranked releases for the backfill episode this job belongs to; [] for other jobs."""
    scope = _episode(db, job_id)
    if scope is None: return None
    plan_id, episode = scope
    deliveries = _deliveries(db, plan_id)
    attempts = []
    for rank, digest, state, reason_code in db.execute(
            'SELECT rank,info_hash,state,reason_code FROM episode_release_candidates '
            'WHERE plan_id=? AND anidb_episode_number=? ORDER BY rank', (plan_id, episode)).fetchall():
        event_id, name = deliveries.get(digest, (None, None))
        stop, details = _stop(db, event_id) if event_id is not None else (None, {})
        attempts.append({
            'rank': rank, 'state': STATES.get(state, state), 'release_name': name,
            'info_hash': digest, 'trace_id': trace_id(event_id=event_id) if event_id else None,
            'reason_code': (stop or reason_code) if state == 'FAILED' else None,
            'details': {k: details[k] for k in ('video_mb', 'limit_mb', 'plan_used_mb')
                        if type(details.get(k)) is int} if state == 'FAILED' else {},
        })
    return {'plan_id': plan_id, 'episode': episode, 'attempts': attempts}
