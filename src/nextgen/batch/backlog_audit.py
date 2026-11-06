"""Batch backlog audit, dry run and safe consolidation (WI-08, WI-16).

`adm recover batch-audit` (a preview) lists, read-only, what consolidation would change:
for each anime season, the operations and jobs found, the one operation that
stays (the primary), the duplicate operations that would be cancelled and
the candidates that would be registered with the series-scoped controller.

`adm recover batch-audit --apply` plans again and applies in one write
transaction, and prints what it applied. It is safe while jobs run: other
writers wait for that transaction, and running jobs are never interrupted.
Duplicates are never marked
cleaned directly: their cancellation is requested through the established
lifecycle and the existing cleanup job does the rest. Running jobs drain
first, only torrents the journals own are deleted, reservations are
released, and a failed cleanup stays pending or goes to review. Candidate
history, timestamps and failure reasons are kept. Consolidation never runs
automatically, for example during a schema migration.
"""
import argparse
import json
import os
import sys
import time

from src.nextgen.acquisition.metadata import infohash
from src.nextgen.acquisition.metadata_service import verify_torrent_ownership
from src.nextgen.batch.recognition import release_scope_hint
from src.nextgen.storage.batch_candidates import (
    _metadata_evidence, activate_candidate, release_facts, add_candidate, get_active_candidate, get_candidate,
    get_candidate_pool, get_or_create_scope, rank_candidates, reorder_candidates,
)

BATCH_KINDS = ('VERIFY_RELEASE', 'EVALUATE_BATCH', 'RESERVE_BATCH', 'SUBMIT_BATCH', 'TRANSFER_BATCH',
               'VERIFY_BATCH_PROBE', 'UNLOCK_BATCH', 'VERIFY_BATCH_EPISODE', 'STAGE_BATCH_EPISODE')
# Journals that record which torrents Anime Discovery owns: (table, marker column).
_JOURNALS = (('batch_metadata_admissions', 'ownership_marker'),
             ('batch_torrent_submissions', 'ownership_marker'),
             ('sonarr_magnet_intents', 'marker'))


def observe_torrents(client, digests):
    """Read-only qBittorrent observations, gathered before any transaction."""
    found = {}
    for digest in dict.fromkeys(infohash(d) for d in digests):
        observation = client.inspect(digest, include_files=True)
        if observation is not None:
            found[digest] = observation
    return found


def torrent_ownership(db, digest, observation):
    """OWNED when a journal owns this exact torrent; UNCERTAIN when a journal or
    an Anime Discovery tag points at it but ownership cannot be proven;
    FOREIGN otherwise. Consolidation never touches FOREIGN or UNCERTAIN."""
    journaled = False
    for table, marker in _JOURNALS:
        for mark, path, category in db.execute(
                f"SELECT {marker},save_path,category FROM {table} WHERE info_hash=?", (digest,)):
            journaled = True
            if verify_torrent_ownership(observation, mark, path, category):
                return 'OWNED'
    if journaled or any(t.startswith('ad-') for t in observation.tags):
        return 'UNCERTAIN'
    return 'FOREIGN'


def _file_evidence(digest, observation):
    """A complete qBittorrent file list as read-only evidence ({digest: ExistingMetadata}), else {}."""
    from src.nextgen.acquisition.existing_metadata import METADATA_PENDING, ExistingMetadata
    from src.nextgen.acquisition.metadata import MetadataInvalid, ObservedBatchInventory
    if observation is None or not observation.files or observation.state in METADATA_PENDING:
        return {}
    try:
        inventory = ObservedBatchInventory(digest, observation.files)
    except MetadataInvalid:
        return {}
    return {digest: ExistingMetadata(inventory, observation.state, observation.category, None)}


def _release(db, operation_id):
    """The season-pack delivery an operation runs: (event_id, info_hash, title), from its VERIFY_RELEASE event."""
    row = db.execute(
        "SELECT e.event_id, e.raw_body FROM jobs j JOIN job_operation_members m USING(job_id) "
        "JOIN incoming_events e ON e.event_id=j.event_id "
        "WHERE m.operation_id=? AND j.kind='VERIFY_RELEASE' ORDER BY j.job_id LIMIT 1", (operation_id,)).fetchone()
    facts = None if row is None else release_facts(*row)
    return None if facts is None else (facts.event_id, facts.info_hash, facts.title)


def operation_scope(db, operation_id):
    """(anidb_id, season, evidence) from verified relations, never from payload fields.

    Prefer the batch verification proof of the operation; otherwise the
    verified identity of its delivery plus the season the release declares.
    """
    row = db.execute(
        "SELECT v.anidb_id, v.tvdb_season FROM batch_verifications v "
        "JOIN job_operation_members m ON m.job_id=v.job_id WHERE m.operation_id=? LIMIT 1",
        (operation_id,)).fetchone()
    if row is not None:
        return row[0], row[1], 'batch_verification'
    release = _release(db, operation_id)
    if release is None:
        return None
    identity = db.execute("SELECT anidb_id FROM identity_results WHERE event_id=? "
                          "ORDER BY validated_at_ms DESC LIMIT 1", (release[0],)).fetchone()
    hint = release_scope_hint(release[2])
    if identity is None or hint.kind not in ('season_pack', 'mixed_pack') or not hint.season_hint:
        return None
    return identity[0], hint.season_hint, 'verified_identity'


def audit_batch_backlog(db, *, observations=None):
    """Read-only plan. observations: {infohash: Observation} gathered beforehand."""
    observations = observations or {}
    marks = ','.join('?' * len(BATCH_KINDS))
    operations = [r[0] for r in db.execute(
        f"SELECT DISTINCT m.operation_id FROM jobs j JOIN job_operation_members m USING(job_id) "
        f"WHERE j.kind IN ({marks}) ORDER BY m.operation_id", BATCH_KINDS)]
    scopes, unassociated = {}, []
    for op in operations:
        state, cleanup = db.execute("SELECT state,cleanup_state FROM job_operations WHERE operation_id=?",
                                    (op,)).fetchone()
        jobs = [{'job_id': j, 'kind': k, 'state': s} for j, k, s in db.execute(
            "SELECT j.job_id,j.kind,j.state FROM jobs j JOIN job_operation_members m USING(job_id) "
            "WHERE m.operation_id=? ORDER BY j.job_id", (op,))]
        entry = {'operation_id': op, 'state': state, 'cleanup_state': cleanup, 'jobs': jobs,
                 'running': any(j['state'] == 'RUNNING' for j in jobs)}
        found = operation_scope(db, op)
        if found is None:
            unassociated.append(entry)       # Reported, never changed.
            continue
        anidb_id, season, evidence = found
        entry['association'] = evidence
        entry['release'] = _release(db, op)
        scopes.setdefault((anidb_id, season), []).append(entry)

    plan = []
    for (anidb_id, season), ops in sorted(scopes.items()):
        tvdb = db.execute("SELECT tvdb_series_id FROM identified_anime WHERE anidb_id=?", (anidb_id,)).fetchone()
        controller = db.execute("SELECT scope_id,operation_id FROM batch_candidate_scopes "
                                "WHERE anidb_id=? AND tvdb_season=?", (anidb_id, season)).fetchone()
        active = [o for o in ops if o['state'] == 'ACTIVE']
        primary = next((o for o in active if controller and o['operation_id'] == controller[1]), None) \
            or next((o for o in active if o['running']), None) \
            or (active[-1] if active else None)
        candidates = []
        for o in ops:
            if o['release'] is None:
                continue
            event_id, digest, title = o['release']
            observed = observations.get(digest)
            has_meta, meta_json = _metadata_evidence(db, digest, _file_evidence(digest, observed))
            candidates.append({
                'info_hash': digest, 'title': title, 'source_event_id': event_id,
                'operation_id': o['operation_id'], 'has_metadata': has_meta, 'metadata_json': meta_json,
                'ownership': torrent_ownership(db, digest, observed) if observed is not None else 'NOT_IN_QBITTORRENT',
            })
        owners = {}
        for c in candidates:
            owners[c['ownership']] = owners.get(c['ownership'], 0) + 1
        plan.append({
            'summary': {
                'known_candidates': len(candidates),
                'metadata_ready': sum(1 for c in candidates if c['has_metadata']),
                'ownership': owners,
                'competing_operations': len(active),
                # Cancelled earlier, cleanup not finished yet: resources may still exist.
                'cleanup_in_progress': [o['operation_id'] for o in ops
                                        if o['state'] in ('CANCEL_REQUESTED', 'CANCELED')
                                        and o['cleanup_state'] != 'COMPLETE'],
            },
            'anidb_id': anidb_id, 'tvdb_series_id': tvdb[0] if tvdb else None, 'season': season,
            'controller_scope_id': controller[0] if controller else None,
            'operations': ops,
            'primary_operation_id': primary['operation_id'] if primary else None,
            'cancel_operations': [
                {'operation_id': o['operation_id'], 'running': o['running'],
                 'jobs': [j for j in o['jobs'] if j['state'] not in ('DONE', 'CANCELED', 'FAILED')]}
                for o in active if o is not primary],
            'register_candidates': candidates,
        })
    ownership = [c['ownership'] for s in plan for c in s['register_candidates']]
    return {
        'scopes': plan,
        'unassociated_operations': unassociated,
        'owned_torrents': ownership.count('OWNED'),
        'foreign_torrents': ownership.count('FOREIGN'),
        'uncertain_torrents': ownership.count('UNCERTAIN'),
    }


def _request_cleanup(db, operation_id, now):
    """Cancel a duplicate operation through the established lifecycle.

    Same transition and cleanup control job as a user Cancel of running
    work (management_actions CLEANUP + CLEANUP_CANCELED_OPERATION). Running
    jobs are only asked to stop; cleanup waits for them, deletes only
    journal-owned torrents and releases reservations. Returns receipts.
    """
    from src.nextgen.storage.cancellation import request_cancellation
    from src.nextgen.storage.sqlite import enqueue_job_with_receipt
    target = db.execute(
        "SELECT j.job_id,j.event_id FROM jobs j JOIN job_operation_members m USING(job_id) "
        "WHERE m.operation_id=? AND NOT EXISTS (SELECT 1 FROM management_actions a WHERE a.job_id=j.job_id) "
        "ORDER BY j.job_id LIMIT 1", (operation_id,)).fetchone()
    if target is None:
        raise ValueError('no job to cancel in this operation')
    canceled, receipts = request_cancellation(db, target[0], now, component='worker')
    control, created = enqueue_job_with_receipt(
        db, component='worker', kind='CLEANUP_CANCELED_OPERATION', event_id=target[1],
        payload={'target_job_id': target[0]}, available_at_ms=now)
    db.execute('INSERT INTO management_actions VALUES(?,?,?,?,?)', (canceled, 'CLEANUP', target[0], control, now))
    return (*receipts, created)


def apply_batch_consolidation(db, audit, *, now_ms=None):
    """Apply an audit's plan inside the caller's write transaction. Idempotent.

    Each step re-checks the current state, so a repeat or a plan that is
    partly applied already only does what is still missing.
    """
    from src.nextgen.storage.cancellation import CancellationError
    if not db.in_transaction:
        raise ValueError('Active database transaction required for consolidation.')
    now = now_ms if now_ms is not None else time.time_ns() // 1000000
    result = {'scopes': 0, 'candidates_registered': 0, 'primaries_linked': 0, 'cleanups_requested': 0,
              'needs_review': [], 'receipts': []}
    for item in audit['scopes']:
        if item['tvdb_series_id'] is None:
            result['needs_review'].append({'anidb_id': item['anidb_id'], 'season': item['season'],
                                           'reason': 'the anime is not identified'})
            continue
        scope = get_or_create_scope(db, item['anidb_id'], item['tvdb_series_id'], item['season'], now_ms=now)
        result['scopes'] += 1
        added = False
        for cand in item['register_candidates']:
            before = get_candidate(db, scope.scope_id, cand['info_hash'])
            add_candidate(db, scope.scope_id, cand['info_hash'], cand['title'],
                          source_event_id=cand['source_event_id'], has_metadata=cand['has_metadata'],
                          metadata_json=cand['metadata_json'], now_ms=now)
            if before is None:
                result['candidates_registered'] += 1
                added = True
        if added:   # Only new candidates change the ranking; a repeat rewrites nothing.
            reorder_candidates(db, scope.scope_id, rank_candidates(get_candidate_pool(db, scope.scope_id)), now_ms=now)

        primary = item['primary_operation_id']
        state = db.execute("SELECT state FROM job_operations WHERE operation_id=?", (primary,)).fetchone() \
            if primary else None
        primary_release = next((c for c in item['register_candidates'] if c['operation_id'] == primary), None)
        if (state == ('ACTIVE',) and primary_release is not None
                and get_active_candidate(db, scope.scope_id) is None):
            current = get_candidate(db, scope.scope_id, primary_release['info_hash'])
            if current is not None and current.state == 'READY':
                activate_candidate(db, scope.scope_id, current.info_hash, operation_id=primary, now_ms=now)
                result['primaries_linked'] += 1

        for duplicate in item['cancel_operations']:
            op = duplicate['operation_id']
            if db.execute("SELECT state FROM job_operations WHERE operation_id=?", (op,)).fetchone() != ('ACTIVE',):
                continue                     # Already cancelled or finished: nothing to do.
            try:
                result['receipts'].extend(_request_cleanup(db, op, now))
                result['cleanups_requested'] += 1
            except (CancellationError, ValueError) as exc:
                result['needs_review'].append({'operation_id': op,
                                               'reason': getattr(exc, 'reason_code', None) or str(exc)})
    # Two different things happened: the candidate pool is consolidated now;
    # resource cleanup was only requested and is done later by cleanup jobs.
    result['message'] = (
        f"Candidate pool: registered {result['candidates_registered']} candidates across {result['scopes']} "
        f"seasons and linked {result['primaries_linked']} primary operations.\n"
        f"Cleanup: requested cleanup of {result['cleanups_requested']} duplicate operations. This is NOT "
        f"completed cleanup: the worker's cleanup jobs drain them, remove only owned torrents and release "
        f"reservations. Run 'adm recover batch-audit' to see cleanup still in progress."
        + (f"\nNeeds review: {len(result['needs_review'])}." if result['needs_review'] else ''))
    return result


def _qbittorrent(config_dir, configuration, digests, *, client_factory=None):
    """Read-only observations, or (None, reason) when qBittorrent could not be checked.

    Logs in and reads torrent info and file lists only; nothing is sent that
    changes a torrent.
    """
    try:
        from src.nextgen.acquisition.qbittorrent import Client
        from src.nextgen.acquisition.settings import download_options
        from src.nextgen.secrets import SecretStore
        client = (client_factory or Client)(download_options(configuration),
                                            SecretStore(config_dir).qbittorrent_credentials())
        client.login()
        return observe_torrents(client, digests), None
    except Exception as exc:          # Reported to the operator, never hidden.
        return None, f'{type(exc).__name__}: {exc}'


def _print_plan(audit, qb_note, *, applied=False):
    print('\n=== Batch backlog consolidation: ' + ('applied plan ===' if applied else
                                                    'dry run, nothing is changed ==='))
    if qb_note:
        print(f'qBittorrent was not checked ({qb_note}); torrent ownership is unknown.')
    for s in audit['scopes']:
        summary = s['summary']
        owners = ', '.join(f'{k.lower()} {v}' for k, v in sorted(summary['ownership'].items())) or 'none'
        print(f"\nAniDB {s['anidb_id']} season {s['season']}: {summary['known_candidates']} known candidate(s), "
              f"{summary['metadata_ready']} with a file list; torrents: {owners}; "
              f"{summary['competing_operations']} competing active operation(s); keep operation "
              f"{s['primary_operation_id']}")
        if summary['cleanup_in_progress']:
            print(f"  cleanup still in progress for operation(s) "
                  f"{', '.join(map(str, summary['cleanup_in_progress']))}")
        for c in s['register_candidates']:
            print(f"  candidate {c['info_hash']} (operation {c['operation_id']}, torrent {c['ownership']}): {c['title']}")
        for d in s['cancel_operations']:
            jobs = ', '.join(f"#{j['job_id']} {j['kind']} {j['state']}" for j in d['jobs']) or 'no open jobs'
            print(f"  cancel duplicate operation {d['operation_id']}"
                  f"{' (running jobs drain first)' if d['running'] else ''}: {jobs}")
    for o in audit['unassociated_operations']:
        print(f"\nOperation {o['operation_id']} ({o['state']}) has no verified anime/season; left unchanged.")
    print(f"\nTorrents: owned {audit['owned_torrents']}, foreign {audit['foreign_torrents']}, "
          f"uncertain {audit['uncertain_torrents']} (foreign and uncertain torrents are never touched).")
    if not applied:
        print("\nTo apply it: adm recover batch-audit --apply (safe while jobs run).\n")


def main(argv=None, *, qb_client_factory=None):
    p = argparse.ArgumentParser(prog='adm recover batch-audit',
                                description='Batch backlog audit and consolidation. Without --apply this is '
                                            'a read-only preview (WI-08, WI-16).')
    p.add_argument('--apply', action='store_true', help='Plan again and apply it in one transaction')
    p.add_argument('--config-dir', default=os.environ.get('NEXTGEN_CONFIG_DIR', '/etc/anime-discovery'))
    p.add_argument('--json', action='store_true', help='Print JSON')
    args = p.parse_args(argv)
    args.action = 'apply' if args.apply else 'preview'

    import logging
    from src.nextgen.configuration import ConfigurationStore
    from src.nextgen.diagnostics import log_history
    from src.nextgen.storage.job_queue import JobQueue
    from src.nextgen.storage.sqlite import transaction
    configuration = ConfigurationStore(args.config_dir).read()
    queue = JobQueue(configuration.value('storage.database_path'))
    from src.nextgen.storage.migrations import _open
    db = _open(configuration.value('storage.database_path'), readonly=True, timeout_ms=5000)
    try:
        db.execute('PRAGMA query_only=ON')
        digests = [c['info_hash'] for s in audit_batch_backlog(db)['scopes'] for c in s['register_candidates']]
    finally:
        db.close()
    # qBittorrent is read before any write transaction is opened.
    observations, qb_note = _qbittorrent(args.config_dir, configuration, digests, client_factory=qb_client_factory)
    if args.action == 'preview':
        from src.nextgen.storage.migrations import _open
        db = _open(configuration.value('storage.database_path'), readonly=True, timeout_ms=5000)
        try:
            db.execute('PRAGMA query_only=ON')           # The preview can never write.
            audit = audit_batch_backlog(db, observations=observations)
        finally:
            db.close()
        if args.json:
            print(json.dumps({**audit, 'qbittorrent_note': qb_note}, indent=2))
        else:
            _print_plan(audit, qb_note)
        return 0
    # Planned and applied in one write transaction: the worker and API wait
    # for it, so the plan cannot change half-way.
    with queue.connection() as db, transaction(db):
        audit = audit_batch_backlog(db, observations=observations)
        result = apply_batch_consolidation(db, audit)
    logger = logging.getLogger('anime-discovery.batch-audit')
    for record in result.pop('receipts'):
        log_history(logger, record)
    if args.json:
        print(json.dumps({'applied_plan': audit, 'result': result, 'qbittorrent_note': qb_note}, indent=2))
    else:
        _print_plan(audit, qb_note, applied=True)
        print(f"{result['message']}\n")
    return 0


if __name__ == '__main__':
    sys.exit(main())
