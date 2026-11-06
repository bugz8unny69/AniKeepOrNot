"""Read-only backlog audit, dry-run reconciliation, and safe consolidation (WI-08).

Coordinates with batch candidate controllers to consolidate historical and duplicate
batch workflows into a single series-scoped candidate controller per (anidb_id, season).
Never performs unverified or destructive deletions of external/foreign torrents.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

from src.nextgen.acquisition.metadata import infohash
from src.nextgen.acquisition.metadata_service import verify_torrent_ownership
from src.nextgen.batch.recognition import release_scope_hint
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.history import integer
from src.nextgen.storage.batch_candidates import (
    add_candidate, get_candidate_pool, get_or_create_scope, rank_candidates, reorder_candidates,
)
from src.nextgen.storage.sqlite import transaction


def _check_torrent_ownership(db, qb_info, digest: str) -> str:
    """Classify torrent ownership as OWNED, FOREIGN, or UNCERTAIN."""
    tor_cat = getattr(qb_info, 'category', '') or ''

    for table, path_col in (
        ('batch_metadata_admissions', 'save_path'),
        ('batch_torrent_submissions', 'save_path'),
        ('sonarr_magnet_intents', 'download_root'),
    ):
        try:
            row = db.execute(
                f"SELECT ownership_marker, {path_col}, category FROM {table} WHERE info_hash=?",
                (digest,)).fetchone()
            if row:
                marker, path, cat = row
                if verify_torrent_ownership(qb_info, marker, path, cat):
                    return 'OWNED'
        except sqlite3.OperationalError:
            pass

    if tor_cat == 'anime':
        return 'UNCERTAIN'
    return 'FOREIGN'


def audit_batch_backlog(db, *, client=None, qb_torrents=None, now_ms=None):
    """Read-only audit of historical batch jobs, duplicate workflows, and torrent ownership (WI-08)."""
    now = now_ms if now_ms is not None else int(__import__('time').time() * 1000)

    # 1. Inspect all batch-related jobs in the system
    cursor = db.execute(
        """SELECT j.job_id, j.kind, j.state, j.payload_json, j.last_reason_code,
                  j.created_at_ms, j.updated_at_ms, j.event_id,
                  e.raw_body, e.source,
                  m.operation_id, o.state AS op_state, o.cleanup_state
           FROM jobs j
           LEFT JOIN incoming_events e ON e.event_id = j.event_id
           LEFT JOIN job_operation_members m ON m.job_id = j.job_id
           LEFT JOIN job_operations o ON o.operation_id = m.operation_id
           WHERE j.kind IN (
               'VERIFY_RELEASE', 'EVALUATE_BATCH', 'RESERVE_BATCH', 'SUBMIT_BATCH',
               'TRANSFER_BATCH', 'VERIFY_BATCH_PROBE', 'UNLOCK_BATCH',
               'VERIFY_BATCH_EPISODE', 'STAGE_BATCH_EPISODE'
           )
           ORDER BY j.job_id ASC"""
    )
    job_rows = cursor.fetchall()

    # 2. Group batch workflows by (anidb_id, season)
    scopes_map = {}
    total_batch_jobs = 0
    duplicate_workflows_count = 0

    for row in job_rows:
        job_id, kind, state, payload_raw, reason, created_at, updated_at, event_id, raw_body, source, op_id, op_state, cleanup_state = row

        payload = {}
        if payload_raw:
            try:
                payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            except Exception:
                pass

        anidb_id = payload.get('anidb_id')

        title = ''
        hash_val = ''
        if raw_body:
            try:
                body = json.loads(bytes(raw_body).decode('utf-8')) if isinstance(raw_body, (bytes, memoryview)) else json.loads(raw_body)
                title = body.get('release_name') or body.get('title') or ''
                hash_val = body.get('torrent_infohash') or body.get('info_hash') or ''
            except Exception:
                pass

        hint = release_scope_hint(title) if title else None
        # Only process season packs / batch jobs
        if hint is not None and hint.kind in ('season_pack', 'mixed_pack'):
            season = hint.season_hint or 1
        elif kind.startswith(('EVALUATE_BATCH', 'RESERVE_BATCH', 'SUBMIT_BATCH', 'TRANSFER_BATCH', 'VERIFY_BATCH', 'UNLOCK_BATCH', 'STAGE_BATCH')):
            season = payload.get('season') or payload.get('tvdb_season') or 1
        else:
            # Single episode VERIFY_RELEASE, not a batch workflow
            continue

        if not anidb_id:
            continue

        total_batch_jobs += 1
        scope_key = (anidb_id, season)
        if scope_key not in scopes_map:
            # Retrieve tvdb_series_id
            tvdb_series_id = anidb_id
            id_row = db.execute("SELECT tvdb_series_id FROM identified_anime WHERE anidb_id=?", (anidb_id,)).fetchone()
            if id_row:
                tvdb_series_id = id_row[0]

            scopes_map[scope_key] = {
                'anidb_id': anidb_id,
                'tvdb_series_id': tvdb_series_id,
                'season': season,
                'jobs': [],
                'operations': set(),
                'candidates': {},
                'stale_admissions': [],
                'stale_reservations': [],
            }

        scope_data = scopes_map[scope_key]
        scope_data['jobs'].append({
            'job_id': job_id,
            'kind': kind,
            'state': state,
            'reason': reason,
            'operation_id': op_id,
            'op_state': op_state,
        })
        if op_id:
            scope_data['operations'].add(op_id)

        if hash_val:
            try:
                digest = infohash(hash_val)
                if digest not in scope_data['candidates']:
                    scope_data['candidates'][digest] = {
                        'info_hash': digest,
                        'title': title,
                        'source_event_id': event_id,
                        'job_id': job_id,
                    }
            except Exception:
                pass

    # 3. Discovered candidates, duplicate operations, and torrent ownership
    owned_torrents_count = 0
    foreign_torrents_count = 0
    uncertain_torrents_count = 0

    qb_ownership_map = {}
    if qb_torrents:
        for tor_hash, qb_info in qb_torrents.items():
            digest = infohash(tor_hash)
            ownership = _check_torrent_ownership(db, qb_info, digest)
            qb_ownership_map[digest] = ownership
            if ownership == 'OWNED':
                owned_torrents_count += 1
            elif ownership == 'FOREIGN':
                foreign_torrents_count += 1
            else:
                uncertain_torrents_count += 1

    consolidation_plan = []

    for (anidb_id, season), scope_data in scopes_map.items():
        op_count = len(scope_data['operations'])
        is_duplicate = op_count > 1 or (len(scope_data['jobs']) > 1 and op_count > 0)
        if is_duplicate:
            duplicate_workflows_count += 1

        # Check existing metadata admissions for candidate hashes
        for digest, cand in scope_data['candidates'].items():
            adm = db.execute(
                "SELECT inventory_json, state, created_at_ms FROM batch_metadata_admissions WHERE info_hash=?",
                (digest,)).fetchone()
            if adm is not None and adm[0]:
                cand['has_metadata'] = 1
                cand['metadata_json'] = adm[0]
                if adm[1] in ('RELEASED', 'EXTERNAL', 'REVIEW'):
                    scope_data['stale_admissions'].append({
                        'info_hash': digest,
                        'state': adm[1],
                        'created_at_ms': adm[2],
                    })
            else:
                cand['has_metadata'] = 0
                cand['metadata_json'] = None

            # Examine existing qBittorrent file inventories read-only (WI-08, review item 6)
            if not cand['has_metadata']:
                if qb_torrents and digest in qb_torrents:
                    qb_info = qb_torrents[digest]
                    files = getattr(qb_info, 'files', None)
                    if files:
                        cand['has_metadata'] = 1
                        file_entries = [{'index': getattr(f, 'index', i), 'name': getattr(f, 'name', None) or getattr(f, 'path', str(f)), 'size': getattr(f, 'size', 0)} for i, f in enumerate(files)]
                        cand['metadata_json'] = json.dumps({'files': file_entries})
                        cand['files'] = file_entries
                elif client is not None and hasattr(client, 'inspect'):
                    try:
                        obs = client.inspect(digest, include_files=True)
                        if obs is not None and getattr(obs, 'files', None):
                            cand['has_metadata'] = 1
                            file_entries = [{'index': getattr(f, 'index', i), 'name': getattr(f, 'name', None) or getattr(f, 'path', str(f)), 'size': getattr(f, 'size', 0)} for i, f in enumerate(obs.files)]
                            cand['metadata_json'] = json.dumps({'files': file_entries})
                            cand['files'] = file_entries
                    except Exception:
                        pass

            # Verify torrent ownership if qB torrents or client passed
            if digest in qb_ownership_map:
                cand['ownership'] = qb_ownership_map[digest]
            elif client is not None and hasattr(client, 'inspect'):
                try:
                    obs = client.inspect(digest, include_files=False)
                    if obs is not None:
                        ownership = _check_torrent_ownership(db, obs, digest)
                        cand['ownership'] = ownership
                        qb_ownership_map[digest] = ownership
                        if ownership == 'OWNED':
                            owned_torrents_count += 1
                        elif ownership == 'FOREIGN':
                            foreign_torrents_count += 1
                        else:
                            uncertain_torrents_count += 1
                    else:
                        cand['ownership'] = 'UNKNOWN'
                except Exception:
                    cand['ownership'] = 'UNKNOWN'
            else:
                cand['ownership'] = 'UNKNOWN'

        consolidation_plan.append({
            'anidb_id': anidb_id,
            'tvdb_series_id': scope_data['tvdb_series_id'],
            'season': season,
            'total_jobs': len(scope_data['jobs']),
            'operations_count': op_count,
            'is_duplicate_workflow': is_duplicate,
            'candidates_count': len(scope_data['candidates']),
            'candidates': list(scope_data['candidates'].values()),
            'stale_admissions_count': len(scope_data['stale_admissions']),
            'jobs': scope_data['jobs'],
        })

    return {
        'total_batch_jobs': total_batch_jobs,
        'unique_scopes_count': len(scopes_map),
        'duplicate_workflows_count': duplicate_workflows_count,
        'owned_torrents': owned_torrents_count,
        'foreign_torrents': foreign_torrents_count,
        'uncertain_torrents': uncertain_torrents_count,
        'consolidation_plan': consolidation_plan,
    }


def apply_batch_consolidation(db, audit_result, *, now_ms=None):
    """Consolidate audited candidate facts into series-scoped controllers (WI-08).

    Accounts for existing running jobs and operations (Item 7):
    - Identifies primary operation and links to batch_candidate_scopes.operation_id
    - Drains and cancels non-primary duplicate operations and competing jobs
    - Never deletes or mutates external or unowned torrents.
    """
    if not db.in_transaction:
        raise ValueError("Active database transaction required for consolidation.")

    now = now_ms if now_ms is not None else int(__import__('time').time() * 1000)
    migrated_scopes = 0
    migrated_candidates = 0
    reconciled_operations = 0
    reconciled_jobs = 0
    drained_duplicate_operations = 0

    from src.nextgen.storage.cancellation import cancel_job, CANCELABLE

    for item in audit_result.get('consolidation_plan', ()):
        anidb_id = item['anidb_id']
        tvdb_series_id = item['tvdb_series_id']
        season = item['season']

        scope = get_or_create_scope(db, anidb_id, tvdb_series_id, season, now_ms=now)
        migrated_scopes += 1

        for cand in item.get('candidates', ()):
            digest = cand['info_hash']
            title = cand.get('title') or f"Consolidated Batch S{season:02d}"
            has_meta = cand.get('has_metadata', 0)
            meta_json = cand.get('metadata_json')
            source_event = cand.get('source_event_id')

            add_candidate(
                db, scope.scope_id, digest, title,
                source_event_id=source_event,
                has_metadata=has_meta,
                metadata_json=meta_json,
                now_ms=now,
            )
            migrated_candidates += 1

        # Reorder pool with standard ranking
        pool = get_candidate_pool(db, scope.scope_id)
        ranked = rank_candidates(pool)
        reorder_candidates(db, scope.scope_id, ranked, now_ms=now)

        # 2. Account for existing running jobs and operations (Item 7)
        scope_jobs = item.get('jobs', ())
        reconciled_jobs += len(scope_jobs)

        op_ids = []
        for j in scope_jobs:
            op_id = j.get('operation_id')
            if op_id and op_id not in op_ids:
                op_ids.append(op_id)

        reconciled_operations += len(op_ids)

        primary_op_id = scope.operation_id
        if not primary_op_id and op_ids:
            for oid in op_ids:
                row = db.execute("SELECT state FROM job_operations WHERE operation_id=?", (oid,)).fetchone()
                if row and row[0] in ('ACTIVE', 'PENDING'):
                    primary_op_id = oid
                    break
            if not primary_op_id:
                primary_op_id = op_ids[-1]

            db.execute(
                "UPDATE batch_candidate_scopes SET operation_id=?, updated_at_ms=? WHERE scope_id=?",
                (primary_op_id, now, scope.scope_id))

        # Drain / cancel non-primary duplicate operations and competing jobs
        for oid in op_ids:
            if oid == primary_op_id:
                continue

            drained_duplicate_operations += 1
            members = db.execute(
                "SELECT job_id, state FROM jobs WHERE job_id IN "
                "(SELECT job_id FROM job_operation_members WHERE operation_id=?)",
                (oid,)).fetchall()
            for m_id, m_state in members:
                if m_state in CANCELABLE:
                    cancel_job(db, m_id, now, component="worker",
                               reason_code="JOB_CANCELED")

            db.execute(
                "UPDATE job_operations SET state='CANCELED', cleanup_state='COMPLETE', "
                "reason_code='CANCELLATION_COMPLETE', updated_at_ms=? "
                "WHERE operation_id=? AND state NOT IN ('CANCELED', 'STAGED')",
                (now, oid))

        # Cancel any orphaned cancelable batch jobs not in the primary operation
        for j in scope_jobs:
            j_id = j['job_id']
            j_op = j.get('operation_id')
            j_state = j.get('state')
            if j_op != primary_op_id and j_state in CANCELABLE:
                cancel_job(db, j_id, now, component="worker",
                           reason_code="JOB_CANCELED")

    return {
        'migrated_scopes': migrated_scopes,
        'migrated_candidates': migrated_candidates,
        'reconciled_operations': reconciled_operations,
        'reconciled_jobs': reconciled_jobs,
        'drained_duplicate_operations': drained_duplicate_operations,
        'message': (
            f"Consolidated {migrated_candidates} candidates across {migrated_scopes} batch scopes "
            f"(reconciled {reconciled_jobs} jobs, {reconciled_operations} operations; "
            f"drained {drained_duplicate_operations} duplicate operations)."
        ),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Batch Backlog Audit and Reconciliation (WI-08)")
    p.add_argument('action', choices=('preview', 'apply'), default='preview', nargs='?',
                   help='Action to perform (preview=dry-run, apply=consolidate candidates)')
    p.add_argument('--config-dir', default=os.environ.get('NEXTGEN_CONFIG_DIR', '/etc/anime-discovery'),
                   help='Configuration directory path')
    p.add_argument('--json', action='store_true', help='Output JSON report')
    args = p.parse_args(argv)

    db_path = ConfigurationStore(args.config_dir).read().value('storage.database_path')
    readonly = args.action == 'preview'
    uri = f"file:{db_path}?mode={'ro' if readonly else 'rw'}"

    # Attempt to initialize read-only qB client if credentials and options available
    client = None
    try:
        from src.nextgen.acquisition.qbittorrent import Client as QBClient
        from src.nextgen.acquisition.settings import download_options
        from src.nextgen.configuration import SecretStore
        cfg = ConfigurationStore(args.config_dir).read()
        secrets = SecretStore(args.config_dir)
        opts = download_options(cfg)
        creds = secrets.qbittorrent_credentials()
        client = QBClient(opts, creds)
        if hasattr(client, 'login'):
            client.login()
    except Exception:
        client = None

    with sqlite3.connect(uri, uri=True, timeout=15) as db:
        if readonly:
            db.execute('PRAGMA query_only=ON')
            audit = audit_batch_backlog(db, client=client)
            if args.json:
                print(json.dumps(audit, indent=2))
            else:
                print("\n=== Batch Backlog Reconciliation Audit (Dry-Run) ===")
                print(f"Total Batch Jobs: {audit['total_batch_jobs']}")
                print(f"Unique Anime/Season Scopes: {audit['unique_scopes_count']}")
                print(f"Duplicate Active Workflows: {audit['duplicate_workflows_count']}")
                print(f"Owned Torrents: {audit['owned_torrents']} | Foreign: {audit['foreign_torrents']} | Uncertain: {audit['uncertain_torrents']}\n")
                for s in audit['consolidation_plan']:
                    print(f"Scope AniDB {s['anidb_id']} Season {s['season']}: {s['total_jobs']} jobs, {s['candidates_count']} candidates, Duplicate: {s['is_duplicate_workflow']}")
                print("\nRun 'adm batch-audit apply' to consolidate candidates into series-scoped controllers.\n")
            return 0
        else:
            with transaction(db):
                audit = audit_batch_backlog(db, client=client)
                result = apply_batch_consolidation(db, audit)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"\n{result['message']}\n")
            return 0


if __name__ == '__main__':
    sys.exit(main())
