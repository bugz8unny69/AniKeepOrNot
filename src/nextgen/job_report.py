"""`adm report [--compress] [--dest PATH] job --id N`: one read-only, plain-text report of everything about a job.

Reads the database in read-only mode; never changes state or calls a service.
Covers the job, a diagnostic summary (job_diagnostics.py: state, blockers,
reservation, download/staging, recovery), the work it controls (or is controlled by), every job in that
operation, their full history including which check failed, and every
recorded download/staging path with whether it still exists on disk.
"""
import json
import linecache
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from src.nextgen.messages import EVENTS

ROOT = Path(__file__).resolve().parents[2]

# (table, column holding a job id, label). Only tables that exist are read.
JOB_TABLES = (
    ('acquisition_plans', 'job_id', 'Acquisition plan'),
    ('episode_reservations', 'owner_job_id', 'Episode reservation'),
    ('sonarr_magnet_intents', 'job_id', 'Magnet intake'),
    ('batch_metadata_admissions', 'verification_job_id', 'Batch metadata admission'),
    ('batch_episode_reservations', 'owner_job_id', 'Batch episode reservation'),
    ('staging_publications', 'job_id', 'Staging publication'),
    ('batch_staging_publications', 'job_id', 'Batch staging publication'),
    ('staging_cleanup_artifacts', 'job_id', 'Staging cleanup artifact'),
)
# Tables keyed by plan_id, reached from the plans above.
PLAN_TABLES = (
    ('torrent_submissions', 'acquisition_plans', 'Torrent submission'),
    ('cancellation_cleanup', 'acquisition_plans', 'Cancellation cleanup journal'),
    ('batch_torrent_submissions', 'batch_episode_reservations', 'Batch torrent submission'),
    ('batch_cancellation_cleanup', 'batch_episode_reservations', 'Batch cancellation cleanup journal'),
)
SKIP = {'files_json', 'manifest_json', 'inventory_json'}  # Large; paths are pulled out below.


def when(ms):
    return '-' if ms is None else datetime.fromtimestamp(ms / 1000).strftime('%Y-%m-%d %H:%M:%S')


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def paths_in(value, found):
    """Collect absolute paths from a value or JSON text, in order, no repeats."""
    if type(value) is str:
        if value.startswith('/') and '\n' not in value:
            if value not in found: found.append(value)
            return
        if value[:1] in '{[':
            try: value = json.loads(value)
            except ValueError: return
            paths_in(value, found)
    elif type(value) is dict:
        for item in value.values(): paths_in(item, found)
    elif type(value) is list:
        for item in value: paths_in(item, found)


def source(check):
    """The code around a failed check, read from this installed release."""
    try:
        location = check.split(' ')[0]
        file, line = location.rsplit(':', 1); line = int(line)
    except ValueError:
        return []
    out = []
    for number in range(max(1, line - 4), line + 2):
        text = linecache.getline(str(ROOT / file), number).rstrip()
        if text: out.append(('>> ' if number == line else '   ') + f'{number:5} {text}')
    return out


def reason_text(code):
    if code is None: return ''
    from src.nextgen.reasons import get_reason
    reason = get_reason(code)
    return code if reason is None else f'{code} — {reason.message}'


def report(db, job_id, out=print):
    from src.nextgen.reasons import get_reason
    have = tables(db)
    job = rows(db, 'SELECT * FROM jobs WHERE job_id=?', (job_id,))
    if not job:
        out(f'Job #{job_id} does not exist.'); return 1
    job = job[0]
    out(f'=== Job #{job_id} · {job["kind"]} · {job["state"]} · attempt {job["attempt_count"]}')
    out(f'Created {when(job["created_at_ms"])} · updated {when(job["updated_at_ms"])}'
        + (f' · created by Job #{job["created_by_job_id"]}' if job['created_by_job_id'] else ''))
    if job['last_reason_code']:
        out('Reason: ' + reason_text(job['last_reason_code']))
        reason = get_reason(job['last_reason_code'])
        if reason: out('Advice: ' + reason.action)
    event = rows(db, 'SELECT received_at_ms,raw_body FROM incoming_events WHERE event_id=?',
                 (job['event_id'],)) if job['event_id'] else []
    if event:
        try: release = json.loads(event[0]['raw_body'] or b'{}').get('release_name')
        except (ValueError, TypeError, AttributeError): release = None
        out(f'Release: {release or "(not recorded)"} · trace AD-{job["event_id"]} · received {when(event[0]["received_at_ms"])}')
    out('Payload: ' + job['payload_json'])

    # The work this job controls, and the controls acting on this job's work.
    target = rows(db, 'SELECT action,target_job_id FROM management_actions WHERE job_id=?', (job_id,))
    focus = target[0]['target_job_id'] if target else job_id
    if target:
        out(f'This is the {target[0]["action"]} action for Job #{focus}; the report below covers that work.')
    from src.nextgen.job_diagnostics import diagnose
    diagnostics = diagnose(db, job_id)
    render(out, diagnostics)
    op = rows(db, 'SELECT o.* FROM job_operations o JOIN job_operation_members m USING(operation_id) '
                  'WHERE m.job_id=?', (focus,))
    members = [focus]
    if op:
        op = op[0]
        out(f'\n=== Operation #{op["operation_id"]}: {op["state"]} · cleanup {op["cleanup_state"]}'
            + (f' · {reason_text(op["reason_code"])}' if op['reason_code'] else ''))
        member_rows = rows(db, 'SELECT j.job_id,j.kind,j.state,j.last_reason_code FROM jobs j '
                               'JOIN job_operation_members m USING(job_id) WHERE m.operation_id=? '
                               'ORDER BY j.job_id', (op['operation_id'],))
        members = [m['job_id'] for m in member_rows]
        for m in member_rows:
            out(f'  Job #{m["job_id"]:<6} {m["kind"]:<28} {m["state"]:<12} {m["last_reason_code"] or ""}')
        for action in rows(db, 'SELECT a.action,a.job_id,j.state,j.last_reason_code FROM management_actions a '
                               'JOIN jobs j USING(job_id) WHERE a.operation_id=?', (op['operation_id'],)):
            out(f'  {action["action"]} action: Job #{action["job_id"]} {action["state"]} {action["last_reason_code"] or ""}')
        if diagnostics['operation'] and diagnostics['operation']['operation_id'] == op['operation_id']:
            overview(out, diagnostics['operation'])

    # History of this job, its target and every member, oldest first.
    scope = sorted({job_id, focus, *members})
    marks = ','.join('?' * len(scope))
    history = rows(db, f'SELECT * FROM job_history WHERE job_id IN ({marks}) ORDER BY occurred_at_ms,history_id', scope)
    out(f'\n=== History ({len(history)} events)')
    failed = []; failed_paths = []
    for h in history:
        details = json.loads(h['details_json'] or '{}')
        line = f'{when(h["occurred_at_ms"])} Job #{h["job_id"]:<6} {h["event_code"]}'
        if h['reason_code']: line += ' · ' + reason_text(h['reason_code'])
        elif h['event_code'] in EVENTS: line += ' · ' + EVENTS[h['event_code']].message
        if details.get('cause_reason_code'): line += f' (cause {reason_text(details["cause_reason_code"])})'
        if details.get('attempt'): line += f' · attempt {details["attempt"]}'
        if h['handler_label']: line += f' [{h["handler_label"]}]'
        out(line)
        if h['event_code'] == 'ON_HOLD' and h['reason_code'] == 'EPISODE_RESERVED':
            out('    (blocking reservation not recorded in this event; see Reservation above)')
        if details.get('observed_audio') is not None or details.get('missing_audio') is not None:
            obs = details.get('observed_audio') or 'none'
            mis = details.get('missing_audio') or 'none'
            out(f'    audio check: observed=[{obs}] missing required=[{mis}] (ffprobe verified)')
        if details.get('failed_check'):
            out(f'    failed check: {details["failed_check"]}')
            failed.append(details['failed_check'])
        if details.get('failed_path'):
            out(f'    file: {details["failed_path"]}')
            failed_paths.append(details['failed_path'])
    if failed:
        out('\n=== Code of the last failed check')
        for text in source(failed[-1]): out(text)
    elif job['state'] in ('NEEDS_REVIEW', 'BLOCKED'):
        out('\n(No failed check was recorded; this stop happened before check recording existed. '
            'Resume it once with Retry to record which check fails.)')

    # Every recorded record and path for the work, with on-disk existence.
    out('\n=== Records and paths')
    paths = list(dict.fromkeys(failed_paths))
    plans = {'acquisition_plans': set(), 'batch_episode_reservations': set()}
    for table, column, label in JOB_TABLES:
        if table not in have: continue
        for row in rows(db, f'SELECT * FROM {table} WHERE {column} IN ({",".join("?" * len(members))})', members):
            if table in plans and row.get('plan_id') is not None: plans[table].add(row['plan_id'])
            show(out, label, row, paths)
    for table, source_table, label in PLAN_TABLES:
        ids = sorted(plans[source_table])
        if table not in have or not ids: continue
        for row in rows(db, f'SELECT * FROM {table} WHERE plan_id IN ({",".join("?" * len(ids))})', ids):
            show(out, label, row, paths)
    markers = sorted({p for p in paths if Path(p).name.startswith('ad-')})
    out('\n=== Paths on disk (live filesystem check, read-only)')
    if not paths: out('  (none recorded)')
    for p in paths:
        try: state = 'directory' if os.path.isdir(p) else 'file' if os.path.exists(p) else 'MISSING'
        except OSError as exc: state = f'unreadable ({exc.__class__.__name__})'
        out(f'  [{state}] {p}')
    if markers:
        out('\nDownload folders owned by this work: ' + ', '.join(Path(p).name for p in markers))
    return 0


def overview(out, op):
    """Member jobs grouped by status; ACTIVE alone is not proof of current work."""
    for name in ('complete', 'executing', 'waiting', 'intervention', 'canceled'):
        if op['groups'][name]: out(f'  {name:<12} ' + ', '.join(f'#{j}' for j in op['groups'][name]))
    out(f'  Path to progress: {op["progress"]} (operation status alone is not proof of current work)')


def render(out, d):
    """Diagnostic sections, in the order: state, dependencies, reservation, downloads, recovery."""
    from src.nextgen.job_diagnostics import when as w, NOT_RECORDED, NOT_CHECKED
    s = d['state']
    out('\n=== Current state')
    out(f'  Now:              {s["doing"]}')
    out(f'  Waiting for:      {s["waiting_for"] or "Nothing"}')
    out(f'  Continues when:   {s["condition"] or "-"}')
    out(f'  Automatic resume: {s["automatic"]}')
    out(f'  Manual action:    {s["manual"]}')
    out(f'  Last success:     {s["last_success"]}')
    out(f'  Last activity:    {s["last_activity"]}')
    if s['suspected_stranded']:
        out('  ! Suspected stranded: unfinished, with no known automatic way to continue.')

    out('\n=== Blocking dependencies')
    out(f'  Created by: {d["creator"] or "none"} (creator, not necessarily a blocker)')
    if not d['dependencies']: out('  No blocking dependency recorded.')
    for dep in d['dependencies']:
        out(f'  {dep["type"]}: ' + ' · '.join(x for x in (
            dep.get('blocking_job') and f'Job #{dep["blocking_job"]}',
            dep.get('blocking_operation') and f'Operation #{dep["blocking_operation"]}',
            dep.get('blocking_status')) if x))
        out(f'    why: {dep["reason"]} · since {dep["since"]} · validity {dep["valid"]}')
        out(f'    resolves when: {dep["resolves_when"]}')
    if d['chain']:
        out('  Chain: ' + ' -> '.join(d['chain']))

    r = d['reservation']
    if r is not None:
        out('\n=== Episode reservation')
        ep = r['episode']
        out('  Episode: ' + (f'AniDB {ep["anidb_id"]} ep-id {ep["anidb_episode_id"]} · TVDB {ep["tvdb_series_id"]} '
                             f'S{ep["tvdb_season"]:02}E{ep["tvdb_episode"]:02} (ep-id {ep["tvdb_episode_id"]}) · '
                             f'selected={bool(ep["selected"])}' if ep else NOT_RECORDED))
        own = r['own']
        out('  This job\'s reservation: ' + (f'#{own["reservation_id"]} {own["state"]} since {w(own["reserved_at_ms"])}'
                                            if own else 'none'))
        for c in r['conflicts']:
            op = c.get('owner_operation')
            out(f'  Blocking {"batch " if c["batch"] else ""}reservation #{c["reservation_id"]} ACTIVE since '
                f'{w(c["reserved_at_ms"])} · matched on {", ".join(c["matched_on"])}')
            out(f'    owner: {"Job #" + str(c["owner_job_id"])} '
                + (f'{c["owner_job"]["kind"]} {c["owner_job"]["state"]}' if c.get('owner_job') else '(not found)')
                + (f' · Operation #{op["operation_id"]} {op["state"]} cleanup {op["cleanup_state"]}' if op else ''))
            out(f'    validity: {c["validity"]} — {c["detail"]}')
        for c in r['released'][:5]:
            out(f'  Released {"batch " if c["batch"] else ""}reservation #{c["reservation_id"]} (owner Job '
                f'#{c["owner_job_id"]}) ended {w(c["ended_at_ms"])} by Job #{c["ended_by_job_id"]}')
        if r.get('finding'): out('  ' + r['finding'])
        for check in r.get('wake_checks', ()):
            out(f'  wake check: [{"ok" if check["ok"] else "FAILS"}] {check["check"]} ({check["detail"]})')
        for note in r['limitations']: out(f'  Limitation: {note}')

    dl, nxt = d['downloads'], d['next_step']
    out('\n=== Download and staging')
    if dl is None: out('  No download or staging records for this work.')
    for p in (dl or {}).get('plans', ()):
        plan, sub, tr, ver, st = p['plan'], p['submission'], p['transfer'], p['verification'], p['staging']
        out(f'  Plan #{plan["plan_id"]} {plan["state"]} · hash {plan["info_hash"]} · prepared by Job #{plan["job_id"]}')
        out('    torrent: ' + (f'{sub["state"]} ({sub["reason_code"]}) · save path {sub["save_path"]}' if sub else NOT_RECORDED)
            + f' · live qBittorrent state: {NOT_CHECKED}')
        out('    download: ' + (f'{tr["phase"]} {tr["selected_progress"]:.0%} · updated {w(tr["updated_at_ms"])}'
                                + (f' · Job #{tr["start_job_id"]}' if tr['start_job_id'] else '') if tr else NOT_RECORDED))
        out('    verified: ' + (f'Job #{ver["job_id"]} at {w(ver["verified_at_ms"])}' if ver else NOT_RECORDED))
        out('    staging: ' + (f'{st["state"]} · Job #{st["job_id"]} · published {w(st["published_at_ms"])}' if st else NOT_RECORDED))
    for b in (dl or {}).get('batches', ()):
        sub, tr = b['submission'], b['transfer']
        out(f'  Batch plan #{sub["plan_id"]} torrent {sub["state"]} · hash {sub["info_hash"]} · save path {sub["save_path"]}'
            f' · live qBittorrent state: {NOT_CHECKED}')
        out('    download: ' + (f'{tr.get("phase", NOT_RECORDED)} · updated {w(tr.get("updated_at_ms"))}' if tr else NOT_RECORDED))
        staged = [x for x in b['staging'] if x['state'] == 'PUBLISHED']
        out(f'    staging: {len(staged)} of {len(b["staging"])} recorded files published')
    out(f'  Next step: {nxt["status"]} — {nxt["detail"]}')

    rec = d['recovery']
    out('\n=== Recovery and retry')
    if rec['lease']: out(f'  Lease: expires {rec["lease"]["expires"]}' + (' (EXPIRED)' if rec['lease']['expired'] else ''))
    out(f'  Interrupted attempts (started, never ended): {len(rec["interrupted_attempts"])}'
        + ''.join(f' · {a["started"]}' for a in rec['interrupted_attempts'][-3:]))
    out(f'  Lease expirations reclaimed: {rec["lease_expired_events"]}')
    out(f'  Automatic retries in current window: {rec["retries_in_window"]} '
        f'(default limit {rec["retry_limit"]}; the worker\'s configured limit is not recorded)')
    out(f'  Manual retry requests: {rec["retry_requests"]} · Retry action: {rec["retry_action"]}')
    if rec['next_attempt']: out(f'  Next attempt: {rec["next_attempt"]}')
    if rec['recovery_events']:
        out('  Recovery events: ' + ', '.join(f'{k}×{v}' for k, v in rec['recovery_events'].items()))
    out(f'  Operation cleanup: {rec["cleanup_state"]}')



def show(out, label, row, paths):
    found = []
    for key, value in row.items(): paths_in(value, found)
    shown = {k: v for k, v in row.items() if k not in SKIP and v is not None and not str(k).endswith('_ms')}
    out(f'  {label}: ' + ' '.join(f'{k}={v}' for k, v in shown.items()))
    for p in found:
        if p not in paths: paths.append(p)


def connect(config_dir):
    from src.nextgen.configuration import ConfigurationStore
    database = ConfigurationStore(config_dir).read().value('storage.database_path')
    db = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON')
    return db


def save(text, name, *, dest=None, compress=False):
    """Write a report as NAME.txt or NAME.tar.gz; a directory dest keeps the name.

    Owner-only (0600). Under sudo the file is handed to the invoking user so it
    can be copied or attached without root.
    """
    import io, tarfile, time
    target = Path(dest or '/tmp').expanduser()
    if dest is None or target.is_dir():
        target = target / (name + ('.tar.gz' if compress else '.txt'))
    target.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode('utf-8')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        if compress:
            with tarfile.open(fileobj=handle, mode='w:gz') as archive:
                info = tarfile.TarInfo(name + '.txt'); info.size = len(data)
                info.mtime = int(time.time()); info.mode = 0o600
                archive.addfile(info, io.BytesIO(data))
        else:
            handle.write(data)
    uid, gid = os.environ.get('SUDO_UID'), os.environ.get('SUDO_GID')
    if os.geteuid() == 0 and uid and gid:
        try: os.chown(target, int(uid), int(gid))
        except (OSError, ValueError): pass
    return target


def report_prowlarr_matching(db, job_id, attempt=None, out=print, is_short=False):
    from src.nextgen.storage.prowlarr_snapshots import load_latest_snapshot
    snapshot = load_latest_snapshot(db, job_id, attempt=attempt)
    if snapshot is None:
        if is_short:
            out(f'=== Prowlarr matching · Job #{job_id}')
        else:
            out('=== Prowlarr matching')
        out('Prowlarr matching: Not recorded for this attempt (older search or capture unavailable). No Prowlarr request was made.')
        return 0

    att = snapshot.get('attempt', 1)
    out(f'=== Prowlarr matching · Job #{job_id} · attempt {att}')
    observed = when(snapshot.get('captured_at_ms'))
    status_suffix = f' (status: {snapshot["status"]})' if snapshot.get('status') != 'COMPLETE' else ''
    out(f'Search observed: {observed} local{status_suffix}')

    target = snapshot.get('target') or {}
    anime_titles = target.get('anime_titles') or []
    anime_name = anime_titles[0] if anime_titles else 'Anime'
    season = target.get('season') or snapshot.get('season') or 1
    episode = target.get('episode') or snapshot.get('episode') or 1
    anidb_ep = target.get('anidb_episode') or episode
    out(f'Anime: {anime_name} · TVDB S{season:02d}E{episode:02d} · AniDB episode {anidb_ep}')
    out('Source: recorded search snapshot (NO live provider request)')

    policy = snapshot.get('policy') or {}
    max_res = policy.get('max_resolution', 1080)
    min_seeders = policy.get('min_seeders', 5)
    max_mb = policy.get('max_episode_mb')
    prefer_audio = policy.get('prefer_audio_tag', False)
    audio_req = 'Japanese+English required' if prefer_audio else 'Any audio'
    mb_str = f'max {max_mb} MB' if max_mb else 'no size limit'
    out(f'Policy: max {max_res}p | min {min_seeders} seeders | {mb_str} | {audio_req}')

    queries = snapshot.get('queries') or []
    out(f'\nQueries ({len(queries)}):')
    for q in queries:
        status_note = f' ({q["status"]})' if q.get('status') != 'SUCCESS' else ''
        out(f'  [{q["index"]}] {q["query"]:<30} {q["result_count"]} raw results{status_note}')

    plan_id = snapshot.get('plan_id') or target.get('plan_id')
    prior_attempts = 0
    candidate_rows = []
    if plan_id is not None and episode is not None:
        have_candidates = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sonarr_episode_candidates'").fetchone()
        if have_candidates:
            candidate_rows = db.execute(
                "SELECT rank, info_hash, state, reason_code FROM sonarr_episode_candidates "
                "WHERE plan_id=? AND anidb_episode_number=? ORDER BY rank",
                (plan_id, target.get('anidb_episode') or episode)
            ).fetchall()
            prior_attempts = len(candidate_rows)

    candidates_map = {row[1].lower(): {'rank': row[0], 'state': row[2], 'reason_code': row[3]} for row in candidate_rows}

    # Audio diagnostic detail lookup from job_history for candidates
    audio_details_by_hash = {}
    have_history = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_history'").fetchone()
    if have_history and candidate_rows:
        try:
            hist_rows = db.execute(
                "SELECT h.job_id, h.details_json FROM job_history h "
                "WHERE h.reason_code='MEDIA_AUDIO_REQUIRED' AND h.details_json IS NOT NULL"
            ).fetchall()
            for jid, d_json in hist_rows:
                try:
                    d = json.loads(d_json)
                    obs = d.get('observed_audio')
                    mis = d.get('missing_audio')
                    if obs is not None or mis is not None:
                        op_row = db.execute(
                            "SELECT p.info_hash FROM job_operation_members m "
                            "JOIN acquisition_plans p ON p.job_id=m.job_id "
                            "WHERE m.operation_id=(SELECT operation_id FROM job_operation_members WHERE job_id=?)",
                            (jid,)
                        ).fetchone()
                        if op_row and op_row[0]:
                            audio_details_by_hash[op_row[0].lower()] = {'observed': obs, 'missing': mis}
                except (ValueError, TypeError, sqlite3.Error):
                    pass
        except sqlite3.Error:
            pass

    results = snapshot.get('results') or []
    policy_accepted_hashes = set()
    previously_tried_hashes = set()
    newly_actionable_hashes = set()

    for r in results:
        disp = r.get('primary_disposition')
        if disp == 'NEW_ELIGIBLE':
            disp = 'PASSES_POLICY'
        is_dup = r.get('is_duplicate', False)
        h = (r.get('info_hash') or '').lower()
        if disp == 'PASSES_POLICY' and not is_dup and h:
            policy_accepted_hashes.add(h)
            if h in candidates_map:
                previously_tried_hashes.add(h)
            else:
                newly_actionable_hashes.add(h)

    passes_policy_count = len(policy_accepted_hashes)
    actionable_count = len(newly_actionable_hashes)
    previously_tried_count = len(previously_tried_hashes)

    counts = snapshot.get('counts') or {}
    raw_count = counts.get('raw', 0)
    unmatched_count = counts.get('unmatched', 0)
    matched_count = counts.get('matched', 0)
    raw_dual_count = counts.get('raw_dual_audio', 0)
    matched_dual_count = counts.get('matched_dual_audio', 0)

    out('\nSearch results:')
    if previously_tried_count > 0:
        out(f'  {raw_count} raw · {unmatched_count} unmatched · {matched_count} EP{episode} matched · '
            f'{passes_policy_count} passes policy ({actionable_count} newly actionable, {previously_tried_count} previously tried)')
    else:
        out(f'  {raw_count} raw · {unmatched_count} unmatched · {matched_count} EP{episode} matched · '
            f'{passes_policy_count} passes policy ({actionable_count} newly actionable)')
    out(f'  {raw_dual_count} raw Dual-Audio hints; {matched_dual_count} matching EP{episode} with a Dual-Audio hint')
    out(f'  Prior database attempts: {prior_attempts} (historical; separately counted)')

    disps = dict(counts.get('dispositions') or {})
    if 'NEW_ELIGIBLE' in disps and 'PASSES_POLICY' not in disps:
        disps['PASSES_POLICY'] = disps.pop('NEW_ELIGIBLE')
    elif 'NEW_ELIGIBLE' in disps and 'PASSES_POLICY' in disps:
        disps['PASSES_POLICY'] += disps.pop('NEW_ELIGIBLE')

    out('\nDispositions for this run (mutually exclusive primary classifications):')
    standard_order = (
        'UNMATCHED_TITLE_OR_EPISODE',
        'MISSING_DUAL_AUDIO_HINT',
        'MISSING_INFO_HASH',
        'RESOLUTION_OUT_OF_POLICY',
        'EPISODE_SIZE_TOO_LARGE',
        'INSUFFICIENT_SEEDERS',
        'DUPLICATE_RESULT',
        'PASSES_POLICY',
        'MAGNET_HASH_CONFLICT',
        'INVALID_TITLE',
        'NON_TORRENT_PROTOCOL',
    )
    for code in standard_order:
        if code in disps or (code in ('UNMATCHED_TITLE_OR_EPISODE', 'PASSES_POLICY') and raw_count > 0):
            out(f'  {code:<28} {disps.get(code, 0):>4}')
    for code, val in disps.items():
        if code not in standard_order:
            out(f'  {code:<28} {val:>4}')
    out(f'  {"TOTAL":<28} {raw_count:>4}')

    failure_reasons = {}
    for row in candidate_rows:
        if row[2] == 'FAILED':
            code = row[3] or 'UNKNOWN'
            failure_reasons[code] = failure_reasons.get(code, 0) + 1

    has_audio_failure = any(row[3] == 'MEDIA_AUDIO_REQUIRED' for row in candidate_rows)
    all_candidates_failed = len(candidate_rows) > 0 and all(row[2] == 'FAILED' for row in candidate_rows)
    is_waiting_for_dub = all_candidates_failed and has_audio_failure

    if candidate_rows or snapshot.get('search_mode') == 'exhausted':
        out('\nCandidate history reconciliation & hold explanation:')
        out(f'  Policy-accepted results : {passes_policy_count} ({previously_tried_count} previously tried, {actionable_count} newly actionable)')
        out(f'  Policy-rejected results : {raw_count - passes_policy_count}')
        if failure_reasons:
            reasons_summary = ', '.join(f'{k}: {v}' for k, v in sorted(failure_reasons.items()))
            out(f'  Previous failure reasons: {reasons_summary}')
        else:
            out('  Previous failure reasons: none')

        for row in candidate_rows:
            rank, h_cand, state, reason = row
            if reason == 'MEDIA_AUDIO_REQUIRED':
                audio_info = audio_details_by_hash.get(h_cand.lower())
                if audio_info:
                    obs = audio_info.get('observed') or 'none'
                    mis = audio_info.get('missing') or 'none'
                    out(f'  Audio diagnostic (#{rank:02d} {h_cand[:8]}): observed=[{obs}], missing required=[{mis}] (ffprobe verified)')
                else:
                    out(f'  Audio diagnostic (#{rank:02d} {h_cand[:8]}): Not recorded')
                out('    (Release-title hints such as MULTi AD or Dual Audio are search hints, not verified audio streams)')

        if is_waiting_for_dub and actionable_count == 0:
            out('  Hold explanation (WAITING_FOR_DUB): All previous candidates failed (with at least one audio failure) '
                'and 0 new actionable releases were found. Job remains on hold; failed torrents are not re-downloaded.')

    out(f'\nAll releases ({len(results)} of {raw_count}):')
    res_hdr = f'{max_res}p?'
    out(f'  #    Query  Episode hint  {res_hdr:<6}  Seeders  Size MB  Dual?  Policy outcome       Candidate history           Title')
    for r in results:
        num = f'{r.get("row_number", 0):03d}'
        q_idx = str(r.get('query_index', 1))
        ep_hint = r.get('parsed_episode_hint') or '-'
        res_val = r.get('resolution', 0)
        res_str = 'Yes' if res_val == max_res else ('No' if res_val == 0 else f'{res_val}p')
        seeders_str = str(r.get('seeders')) if r.get('seeders') is not None else '-'
        size_bytes = r.get('size_bytes') or 0
        size_str = str(size_bytes // 1_000_000) if size_bytes else '-'
        dual_str = 'Yes' if r.get('has_dual_audio_hint') else 'No'
        disp_str = r.get('primary_disposition') or '-'
        if disp_str == 'NEW_ELIGIBLE':
            disp_str = 'PASSES_POLICY'
        h = (r.get('info_hash') or '').lower()
        if h in candidates_map:
            cand = candidates_map[h]
            state = cand.get('state', 'UNKNOWN')
            reason = cand.get('reason_code')
            cand_hist_str = f'FAILED ({reason})' if (state == 'FAILED' and reason) else state
        else:
            cand_hist_str = 'UNTRIED'
        title_str = r.get('title') or ''
        out(f'  {num:<4} {q_idx:<6} {ep_hint:<13} {res_str:<7} {seeders_str:<8} {size_str:<8} {dual_str:<6} {disp_str:<20} {cand_hist_str:<27} {title_str}')

    out('\nDetailed reasons:')
    for r in results:
        num = f'{r.get("row_number", 0):03d}'
        reason_str = r.get('reason') or ''
        disp_str = r.get('primary_disposition') or ''
        if disp_str == 'NEW_ELIGIBLE':
            disp_str = 'PASSES_POLICY'
        h = (r.get('info_hash') or '').lower()
        if h in candidates_map:
            cand = candidates_map[h]
            state = cand.get('state')
            reason_code = cand.get('reason_code')
            hist_note = f'candidate history: {state}' + (f' ({reason_code})' if reason_code else '') + ' - previously tried, not actionable'
            if disp_str == 'PASSES_POLICY':
                out(f'  #{num} passes policy checks; {hist_note}')
            else:
                out(f'  #{num} {reason_str}; {hist_note}')
        else:
            if disp_str == 'PASSES_POLICY':
                out(f'  #{num} passes policy checks; untried, actionable new release')
            else:
                out(f'  #{num} {reason_str}')
    return 0


def prowlarr_matching_json(db, job_id, attempt=None):
    from src.nextgen.storage.prowlarr_snapshots import load_latest_snapshot
    snapshot = load_latest_snapshot(db, job_id, attempt=attempt)
    if snapshot is None:
        return {
            'recorded': False,
            'message': 'Not recorded for this attempt (older search or capture unavailable). No Prowlarr request was made.',
        }
    target = snapshot.get('target') or {}
    episode = target.get('episode') or snapshot.get('episode') or 1
    plan_id = snapshot.get('plan_id') or target.get('plan_id')
    prior_attempts = 0
    candidate_rows = []
    if plan_id is not None and episode is not None:
        have_candidates = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sonarr_episode_candidates'").fetchone()
        if have_candidates:
            candidate_rows = db.execute(
                "SELECT rank, info_hash, state, reason_code FROM sonarr_episode_candidates "
                "WHERE plan_id=? AND anidb_episode_number=? ORDER BY rank",
                (plan_id, target.get('anidb_episode') or episode)
            ).fetchall()
            prior_attempts = len(candidate_rows)

    candidates_map = {row[1].lower(): {'rank': row[0], 'state': row[2], 'reason_code': row[3]} for row in candidate_rows}

    # Audio diagnostic detail lookup from job_history for candidates
    audio_details_by_hash = {}
    have_history = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_history'").fetchone()
    if have_history and candidate_rows:
        try:
            hist_rows = db.execute(
                "SELECT h.job_id, h.details_json FROM job_history h "
                "WHERE h.reason_code='MEDIA_AUDIO_REQUIRED' AND h.details_json IS NOT NULL"
            ).fetchall()
            for jid, d_json in hist_rows:
                try:
                    d = json.loads(d_json)
                    obs = d.get('observed_audio')
                    mis = d.get('missing_audio')
                    if obs is not None or mis is not None:
                        op_row = db.execute(
                            "SELECT p.info_hash FROM job_operation_members m "
                            "JOIN acquisition_plans p ON p.job_id=m.job_id "
                            "WHERE m.operation_id=(SELECT operation_id FROM job_operation_members WHERE job_id=?)",
                            (jid,)
                        ).fetchone()
                        if op_row and op_row[0]:
                            audio_details_by_hash[op_row[0].lower()] = {'observed': obs, 'missing': mis}
                except (ValueError, TypeError, sqlite3.Error):
                    pass
        except sqlite3.Error:
            pass

    results = snapshot.get('results') or []
    policy_accepted_hashes = set()
    previously_tried_hashes = set()
    newly_actionable_hashes = set()
    enriched_releases = []

    for r in results:
        disp = r.get('primary_disposition')
        if disp == 'NEW_ELIGIBLE':
            disp = 'PASSES_POLICY'
        is_dup = r.get('is_duplicate', False)
        h = (r.get('info_hash') or '').lower()
        if disp == 'PASSES_POLICY' and not is_dup and h:
            policy_accepted_hashes.add(h)
            if h in candidates_map:
                previously_tried_hashes.add(h)
            else:
                newly_actionable_hashes.add(h)

        item = dict(r)
        if item.get('primary_disposition') == 'NEW_ELIGIBLE':
            item['primary_disposition'] = 'PASSES_POLICY'
        if h in candidates_map:
            cand = candidates_map[h]
            item['candidate_history'] = {
                'recorded': True,
                'state': cand['state'],
                'reason_code': cand['reason_code'],
                'rank': cand['rank'],
            }
        else:
            item['candidate_history'] = {
                'recorded': False,
                'state': 'UNTRIED',
                'reason_code': None,
                'rank': None,
            }
        enriched_releases.append(item)

    passes_policy_count = len(policy_accepted_hashes)
    actionable_count = len(newly_actionable_hashes)
    previously_tried_count = len(previously_tried_hashes)

    failure_reasons = {}
    for row in candidate_rows:
        if row[2] == 'FAILED':
            code = row[3] or 'UNKNOWN'
            failure_reasons[code] = failure_reasons.get(code, 0) + 1

    has_audio_failure = any(row[3] == 'MEDIA_AUDIO_REQUIRED' for row in candidate_rows)
    all_candidates_failed = len(candidate_rows) > 0 and all(row[2] == 'FAILED' for row in candidate_rows)
    is_waiting_for_dub = all_candidates_failed and has_audio_failure

    counts_out = dict(snapshot.get('counts') or {})
    counts_out['passes_policy'] = passes_policy_count
    counts_out['newly_actionable'] = actionable_count
    counts_out['previously_tried'] = previously_tried_count

    candidate_reconcile = {
        'prior_candidates_count': len(candidate_rows),
        'policy_accepted_count': passes_policy_count,
        'previously_tried_count': previously_tried_count,
        'newly_actionable_count': actionable_count,
        'previous_failure_reasons': failure_reasons,
        'is_waiting_for_dub': is_waiting_for_dub,
        'hold_explanation': (
            'All previous candidates failed (with at least one audio failure) and 0 new actionable releases were found. Job remains on hold; failed torrents are not re-downloaded.'
            if (is_waiting_for_dub and actionable_count == 0) else None
        ),
    }

    return {
        'recorded': True,
        'snapshot_id': snapshot.get('snapshot_id'),
        'job_id': snapshot.get('job_id'),
        'attempt': snapshot.get('attempt'),
        'captured_at_ms': snapshot.get('captured_at_ms'),
        'captured_at': when(snapshot.get('captured_at_ms')),
        'search_mode': snapshot.get('search_mode'),
        'status': snapshot.get('status'),
        'target': snapshot.get('target'),
        'policy': snapshot.get('policy'),
        'queries': snapshot.get('queries'),
        'counts': counts_out,
        'prior_database_attempts': prior_attempts,
        'candidate_reconciliation': candidate_reconcile,
        'releases': enriched_releases,
        'truncated': snapshot.get('truncated', False),
    }


def main(argv):
    import argparse
    # Short form alias support: if 'job' is omitted but '--id' is present, insert 'job'
    if 'job' not in argv and any(arg == '--id' or arg.startswith('--id=') for arg in argv):
        for idx, arg in enumerate(argv):
            if arg == '--id' or arg.startswith('--id='):
                argv = argv[:idx] + ['job'] + argv[idx:]
                break

    parser = argparse.ArgumentParser(prog='adm report', description='Read-only reports')
    parser.add_argument('--compress', action='store_true',
                        help='Save as a .tar.gz (default /tmp/<kind>-report-<id>.tar.gz)')
    parser.add_argument('--dest', help='File or directory to save the report to')
    parser.add_argument('--config-dir', default='/etc/anime-discovery')
    parser.add_argument('--json', action='store_true', default=False,
                        help='Print diagnostics or matching report as JSON')
    parser.add_argument('--short', action='store_true', default=False,
                        help='Output only the selected matching report section')
    parser.add_argument('--include-matching', choices=('prowlarr',), default=None,
                        help='Include matching section for provider')
    parser.add_argument('--attempt', type=int, default=None,
                        help='Specific captured search attempt')
    kinds = parser.add_subparsers(dest='what', required=True, metavar='{job}')
    job = kinds.add_parser('job', help='Everything about one job: history, failed check, paths')
    job.add_argument('--id', dest='job_id', type=int, required=True, help='Job number, e.g. 2593')
    job.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                     help='Print the diagnostics as JSON instead of text')
    job.add_argument('--short', action='store_true', default=argparse.SUPPRESS,
                     help='Output only the selected matching report section')
    job.add_argument('--include-matching', choices=('prowlarr',), default=argparse.SUPPRESS,
                     help='Include matching section for provider')
    job.add_argument('--attempt', type=int, default=argparse.SUPPRESS,
                     help='Specific captured search attempt')
    args = parser.parse_args(argv)

    is_json = getattr(args, 'json', False)
    is_short = getattr(args, 'short', False)
    include_matching = getattr(args, 'include_matching', None)
    attempt = getattr(args, 'attempt', None)

    if is_short and not include_matching:
        parser.error('--short requires --include-matching <section>')

    try:
        db = connect(args.config_dir)
    except Exception as exc:  # Explain plainly; the usual cause is permissions.
        parser.exit(2, f'Cannot open the database read-only ({exc.__class__.__name__}: {exc}). '
                       'Run with sudo, or as the anime-discovery service account.\n')
    lines = []
    printing = not (args.compress or args.dest)
    out = print if printing else lines.append
    try:
        with db:
            if is_json:
                if is_short:
                    matching_doc = prowlarr_matching_json(db, args.job_id, attempt=attempt)
                    result_data = {
                        'job_id': args.job_id,
                        'matching': {
                            'prowlarr': matching_doc,
                        }
                    }
                    out(json.dumps(result_data, indent=2, default=str))
                    status = 0
                else:
                    from src.nextgen.job_diagnostics import diagnose
                    data = diagnose(db, args.job_id)
                    if data and include_matching:
                        matching_doc = prowlarr_matching_json(db, args.job_id, attempt=attempt)
                        data['matching'] = {
                            include_matching: matching_doc,
                        }
                    out(json.dumps(data, indent=2, default=str) if data else json.dumps({'error': 'job not found'}))
                    status = 0 if data else 1
            else:
                if is_short:
                    status = report_prowlarr_matching(db, args.job_id, attempt=attempt, out=out, is_short=True)
                else:
                    status = report(db, args.job_id, out=out)
                    if status == 0 and include_matching == 'prowlarr':
                        out('')
                        report_prowlarr_matching(db, args.job_id, attempt=attempt, out=out, is_short=False)
        if not printing:
            saved = save('\n'.join(lines) + '\n', f'{args.what}-report-{args.job_id}',
                         dest=args.dest, compress=args.compress)
            print(f'Report saved to {saved}')
    finally:
        db.close()
    return status
