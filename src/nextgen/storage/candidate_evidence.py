"""Durable candidate evidence persistence for explainable AniDB matching.

Stores evaluated AniDB candidate information per job attempt, supporting
explainable ranking, eligibility status, and diagnostics.
"""
import json
import time

MAX_STORED_CANDIDATES = 500


def save_candidate_evidence(db, job_id, attempt, candidates, now_ms=None):
    """Save candidate evidence records within the caller's active transaction."""
    if not candidates:
        return
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    for cand in candidates[:MAX_STORED_CANDIDATES]:
        data = cand if isinstance(cand, dict) else getattr(cand, '__dict__', {})
        aid = data.get('anidb_id')
        if aid is None:
            continue

        display_title = str(data.get('display_title') or '')[:512]
        matched_title = str(data.get('matched_anidb_title') or data.get('matched_title') or '')[:512]
        matched_variant = data.get('matched_variant')
        variant_text = str(
            getattr(matched_variant, 'text', None) or
            (matched_variant.get('text') if isinstance(matched_variant, dict) else matched_variant) or ''
        )[:512]

        similarity_pct = int(data.get('similarity_pct') or 0)
        rank = int(data.get('rank') or 1)
        eligible = 1 if data.get('eligible') else 0
        selected = 1 if data.get('selected') else 0
        evidence = data.get('evidence') or {}
        evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)

        db.execute(
            """INSERT INTO identity_candidate_evidence (
                job_id, attempt, captured_at_ms, anidb_id, display_title,
                matched_title, matched_variant, similarity_pct, rank,
                eligible, selected, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, attempt, now_ms, aid, display_title,
                matched_title, variant_text, similarity_pct, rank,
                eligible, selected, evidence_json
            )
        )


def load_candidate_evidence(db, job_id, attempt=None, *, limit=None, offset=None):
    """Load candidate evidence rows for a job, ordered by rank."""
    if attempt is None:
        row = db.execute(
            "SELECT max(attempt) FROM identity_candidate_evidence WHERE job_id = ?",
            (job_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return []
        attempt = row[0]

    sql = """SELECT evidence_id, job_id, attempt, captured_at_ms, anidb_id,
                    display_title, matched_title, matched_variant, similarity_pct,
                    rank, eligible, selected, evidence_json
             FROM identity_candidate_evidence
             WHERE job_id = ? AND attempt = ?
             ORDER BY rank ASC"""
    params = [job_id, attempt]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
        if offset is not None:
            sql += " OFFSET ?"
            params.append(int(offset))

    rows = db.execute(sql, params).fetchall()
    results = []
    for r in rows:
        try:
            ev = json.loads(r[12] or '{}')
        except (ValueError, TypeError):
            ev = {}
        results.append({
            'evidence_id': r[0],
            'job_id': r[1],
            'attempt': r[2],
            'captured_at_ms': r[3],
            'anidb_id': r[4],
            'display_title': r[5],
            'matched_title': r[6],
            'matched_variant': r[7],
            'similarity_pct': r[8],
            'rank': r[9],
            'eligible': bool(r[10]),
            'selected': bool(r[11]),
            'evidence': ev,
        })
    return results


def count_candidate_evidence(db, job_id, attempt=None):
    """Return total count of recorded candidate evidence rows for a job."""
    if attempt is None:
        row = db.execute(
            "SELECT max(attempt) FROM identity_candidate_evidence WHERE job_id = ?",
            (job_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return 0
        attempt = row[0]
    row = db.execute(
        "SELECT count(*) FROM identity_candidate_evidence WHERE job_id = ? AND attempt = ?",
        (job_id, attempt)
    ).fetchone()
    return row[0] if row else 0
