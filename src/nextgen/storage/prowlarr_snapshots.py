"""Durable Prowlarr search diagnostic snapshots for explainable matching reports.

Persisted search observations only; never live queries, authorization permits,
full magnet URLs, API keys or arbitrary provider payloads.
"""
import json
import re
import time

ANSI_ESCAPE = re.compile(r'\x1B(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])')
MAX_TITLE_LEN = 2048
MAX_INDEXER_LEN = 256
MAX_STORED_ROWS = 1000


def sanitize_text(value, max_len=MAX_TITLE_LEN):
    """Strip ANSI sequences and non-printable characters; bound length."""
    if not isinstance(value, str):
        return ''
    cleaned = ANSI_ESCAPE.sub('', value)
    cleaned = ''.join(ch for ch in cleaned if ord(ch) >= 32 and ord(ch) != 127)
    return cleaned.strip()[:max_len]


def save_snapshots(db, job_id, attempt, snapshots, now_ms=None):
    """Save captured search snapshots within the caller's active transaction."""
    if not snapshots:
        return
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    for item in snapshots:
        data = item if isinstance(item, dict) else getattr(item, 'data', None)
        if data is None and hasattr(item, '__dict__'):
            data = item.__dict__
        if not isinstance(data, dict):
            continue
        plan_id = data.get('plan_id')
        tvdb_series_id = data.get('tvdb_series_id')
        season = data.get('season')
        episode = data.get('episode')
        search_mode = str(data.get('search_mode') or 'initial')
        status = str(data.get('status') or 'COMPLETE')
        if status not in ('COMPLETE', 'PARTIAL', 'ERROR'):
            status = 'COMPLETE'

        payload = dict(data)
        payload['job_id'] = job_id
        payload['attempt'] = attempt
        payload['captured_at_ms'] = now_ms

        raw_results = payload.get('results') or []
        if len(raw_results) > MAX_STORED_ROWS:
            payload['truncated'] = True
            payload['total_raw_count'] = len(raw_results)
            payload['truncated_reason'] = f'Exceeded maximum diagnostic capacity of {MAX_STORED_ROWS} rows'
            payload['results'] = raw_results[:MAX_STORED_ROWS]

        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        db.execute(
            """INSERT INTO prowlarr_search_snapshots (
                job_id, attempt, captured_at_ms, plan_id, tvdb_series_id,
                season, episode, search_mode, status, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, attempt, now_ms, plan_id, tvdb_series_id,
                season, episode, search_mode, status, payload_json
            )
        )


def load_latest_snapshot(db, job_id, attempt=None):
    """Load the latest captured Prowlarr search snapshot for a job."""
    if attempt is not None:
        row = db.execute(
            """SELECT snapshot_id, job_id, attempt, captured_at_ms, plan_id,
                      tvdb_series_id, season, episode, search_mode, status, snapshot_json
               FROM prowlarr_search_snapshots
               WHERE job_id = ? AND attempt = ?
               ORDER BY snapshot_id DESC LIMIT 1""",
            (job_id, attempt)
        ).fetchone()
    else:
        # Default to latest completed captured search attempt; fall back to any captured.
        row = db.execute(
            """SELECT snapshot_id, job_id, attempt, captured_at_ms, plan_id,
                      tvdb_series_id, season, episode, search_mode, status, snapshot_json
               FROM prowlarr_search_snapshots
               WHERE job_id = ? AND status = 'COMPLETE'
               ORDER BY attempt DESC, snapshot_id DESC LIMIT 1""",
            (job_id,)
        ).fetchone()
        if row is None:
            row = db.execute(
                """SELECT snapshot_id, job_id, attempt, captured_at_ms, plan_id,
                          tvdb_series_id, season, episode, search_mode, status, snapshot_json
                   FROM prowlarr_search_snapshots
                   WHERE job_id = ?
                   ORDER BY attempt DESC, snapshot_id DESC LIMIT 1""",
                (job_id,)
            ).fetchone()

    if row is None:
        return None

    try:
        data = json.loads(row[10])
    except (ValueError, TypeError):
        data = {}

    data['snapshot_id'] = row[0]
    data['job_id'] = row[1]
    data['attempt'] = row[2]
    data['captured_at_ms'] = row[3]
    data['plan_id'] = row[4]
    data['tvdb_series_id'] = row[5]
    data['season'] = row[6]
    data['episode'] = row[7]
    data['search_mode'] = row[8]
    data['status'] = row[9]
    return data


def prune_snapshots(db, *, keep_per_job=5, terminal_age_days=30, now_ms=None):
    """Prune old search snapshots without ever touching active or held evidence.

    Protects jobs in active, held, or review states (ON_HOLD, NEEDS_REVIEW, etc.).
    Only terminal jobs (DONE, CANCELED) are pruned.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cutoff_ms = now_ms - (terminal_age_days * 86400 * 1000)

    # 1. Age-based cleanup for terminal jobs
    db.execute(
        """DELETE FROM prowlarr_search_snapshots
           WHERE captured_at_ms < ?
             AND job_id IN (
                 SELECT job_id FROM jobs
                 WHERE state IN ('DONE', 'CANCELED')
             )""",
        (cutoff_ms,)
    )

    # 2. Count-based pruning per terminal job (keep latest keep_per_job snapshots)
    terminal_jobs = [
        r[0] for r in db.execute(
            """SELECT DISTINCT s.job_id
               FROM prowlarr_search_snapshots s
               JOIN jobs j ON j.job_id = s.job_id
               WHERE j.state IN ('DONE', 'CANCELED')"""
        ).fetchall()
    ]
    for jid in terminal_jobs:
        snapshots = db.execute(
            """SELECT snapshot_id FROM prowlarr_search_snapshots
               WHERE job_id = ?
               ORDER BY captured_at_ms DESC, snapshot_id DESC""",
            (jid,)
        ).fetchall()
        if len(snapshots) > keep_per_job:
            to_remove = [s[0] for s in snapshots[keep_per_job:]]
            placeholders = ','.join('?' * len(to_remove))
            db.execute(
                f"DELETE FROM prowlarr_search_snapshots WHERE snapshot_id IN ({placeholders})",
                to_remove
            )
