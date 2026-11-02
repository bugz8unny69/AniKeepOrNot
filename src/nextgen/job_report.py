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


def main(argv):
    import argparse
    parser = argparse.ArgumentParser(prog='adm report', description='Read-only reports')
    parser.add_argument('--compress', action='store_true',
                        help='Save as a .tar.gz (default /tmp/<kind>-report-<id>.tar.gz)')
    parser.add_argument('--dest', help='File or directory to save the report to')
    parser.add_argument('--config-dir', default='/etc/anime-discovery')
    kinds = parser.add_subparsers(dest='what', required=True, metavar='{job}')
    job = kinds.add_parser('job', help='Everything about one job: history, failed check, paths')
    job.add_argument('--id', dest='job_id', type=int, required=True, help='Job number, e.g. 2593')
    job.add_argument('--json', action='store_true', help='Print the diagnostics as JSON instead of text')
    args = parser.parse_args(argv)
    try:
        db = connect(args.config_dir)
    except Exception as exc:  # Explain plainly; the usual cause is permissions.
        parser.exit(2, f'Cannot open the database read-only ({exc.__class__.__name__}: {exc}). '
                       'Run with sudo, or as the anime-discovery service account.\n')
    lines = []
    printing = not (args.compress or args.dest)
    out = print if printing else lines.append
    with db:
        if args.json:
            from src.nextgen.job_diagnostics import diagnose
            data = diagnose(db, args.job_id)
            out(json.dumps(data, indent=2, default=str) if data else json.dumps({'error': 'job not found'}))
            status = 0 if data else 1
        else:
            status = report(db, args.job_id, out=out)
    if not printing:
        saved = save('\n'.join(lines) + '\n', f'{args.what}-report-{args.job_id}',
                     dest=args.dest, compress=args.compress)
        print(f'Report saved to {saved}')
    return status
