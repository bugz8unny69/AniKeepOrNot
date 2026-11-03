"""Read-only v54 preflight for AniDB-first episode selection (Step 1).

Answers one question before the v55 schema change: can every TVDB-keyed trial
row be converted to exactly one AniDB episode number using evidence already
stored in this database? It never writes, never calls a provider, and never
infers numbers from season offsets or release names.

Run it on a COPY of the database, never the live file:

    PYTHONPATH=. python -m src.nextgen.storage.anidb_selection_preflight --db /tmp/copy.sqlite3

Exit code 0: conversion is unambiguous. 1: blocked (see report). 2: unusable input.
"""
import json
import sqlite3
import sys

from src.nextgen.storage.migrations import _open

EXPECTED_VERSION = 54
# Tables whose tvdb_episode is the trial window key today.
KEYED_TABLES = ('sonarr_backfill_results', 'sonarr_episode_selections',
                'sonarr_episode_candidates', 'sonarr_magnet_intents')
# Discovery job payload fields that hold a window episode number.
PAYLOAD_EPISODES = ('retry_episode', 'fallback_episode', 'exhausted_episode')
OPEN_JOB_STATES = ('PENDING', 'RUNNING', 'RETRY_WAIT', 'ON_HOLD', 'NEEDS_REVIEW', 'BLOCKED')


def _evidence(db, anidb_id, series, season, episode):
    """Distinct AniDB numbers that stored, verified rows give this TVDB placement."""
    rows = db.execute(
        "SELECT anidb_episode_number FROM recognized_episodes "
        "WHERE anidb_id=? AND tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=? "
        "UNION SELECT anidb_episode_number FROM release_verifications "
        "WHERE anidb_id=? AND tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=? "
        "AND tvdb_order='official'",
        (anidb_id, series, season, episode) * 2).fetchall()
    return sorted({row[0] for row in rows})


def preflight(db):
    """Report dict; report['blocking'] lists every reason v55 must not run."""
    version = db.execute('PRAGMA user_version').fetchone()[0]
    report = {'schema_version': version, 'plans': 0, 'duplicate_plans': [],
              'anchors': [], 'rows': {}, 'unmapped': [], 'conflicts': [],
              'open_jobs': [], 'blocking': []}
    if version != EXPECTED_VERSION:
        report['blocking'].append('schema version %s, expected %s' % (version, EXPECTED_VERSION))
        return report
    plans = db.execute(
        "SELECT p.plan_id,p.anidb_id,p.tvdb_series_id,p.tvdb_season,p.anchor_episode,"
        "v.anidb_episode_number,v.anidb_id,v.tvdb_series_id,v.tvdb_season,v.tvdb_episode "
        "FROM trial_plans p LEFT JOIN release_verifications v "
        "ON v.verification_id=p.anchor_verification_id ORDER BY p.plan_id").fetchall()
    report['plans'] = len(plans)
    by_anime = {}
    for plan_id, aid, *_ in plans:
        by_anime.setdefault(aid, []).append(plan_id)
    for aid, ids in sorted(by_anime.items()):
        if len(ids) > 1:
            report['duplicate_plans'].append({'anidb_id': aid, 'plan_ids': ids})
    scope = {}
    for plan_id, aid, tid, season, anchor, number, v_aid, v_tid, v_season, v_episode in plans:
        scope[plan_id] = (aid, tid, season)
        if number is None or (v_aid, v_tid, v_season, v_episode) != (aid, tid, season, anchor):
            report['unmapped'].append({'plan_id': plan_id, 'table': 'trial_plans',
                                       'placement': [season, anchor],
                                       'reason': 'anchor verification missing or different'})
        else:
            report['anchors'].append({'plan_id': plan_id, 'tvdb': [season, anchor],
                                      'anidb_episode_number': number})
    mapping = {}  # (plan_id, tvdb_episode) -> anidb number

    def convert(table, plan_id, episode, detail=None):
        key = (plan_id, episode)
        if key in mapping: return mapping[key]
        if plan_id not in scope:
            report['unmapped'].append({'plan_id': plan_id, 'table': table,
                                       'placement': [None, episode], 'reason': 'unknown plan'})
            mapping[key] = None
            return None
        aid, tid, season = scope[plan_id]
        found = _evidence(db, aid, tid, season, episode)
        if len(found) != 1:
            report['unmapped'].append({
                'plan_id': plan_id, 'table': table, 'placement': [season, episode],
                'reason': 'no stored AniDB evidence' if not found else 'ambiguous: %s' % found,
                **({'detail': detail} if detail else {})})
            mapping[key] = None
            return None
        mapping[key] = found[0]
        return found[0]

    for table in KEYED_TABLES:
        extra = {'sonarr_backfill_results': 'status', 'sonarr_episode_candidates': 'state',
                 'sonarr_magnet_intents': 'state', 'sonarr_episode_selections': 'info_hash'}[table]
        rows = db.execute(f"SELECT plan_id,tvdb_episode,{extra} FROM {table}").fetchall()
        report['rows'][table] = len(rows)
        for plan_id, episode, value in rows:
            convert(table, plan_id, episode, '%s=%s' % (extra, value))
    # Two old keys may not land on one new key inside a plan.
    seen = {}
    for (plan_id, episode), number in mapping.items():
        if number is None: continue
        other = seen.setdefault((plan_id, number), episode)
        if other != episode:
            report['conflicts'].append({'plan_id': plan_id, 'anidb_episode_number': number,
                                        'tvdb_episodes': sorted({other, episode})})
    marks = ','.join('?' for _ in OPEN_JOB_STATES)
    for job_id, kind, state, payload in db.execute(
            f"SELECT job_id,kind,state,payload_json FROM jobs WHERE state IN ({marks}) "
            "AND json_extract(payload_json,'$.plan_id') IS NOT NULL ORDER BY job_id",
            OPEN_JOB_STATES).fetchall():
        body = json.loads(payload)
        entry = {'job_id': job_id, 'kind': kind, 'state': state, 'plan_id': body.get('plan_id')}
        for name in PAYLOAD_EPISODES:
            if type(body.get(name)) is int:
                entry[name] = [body[name], convert('jobs.' + name, body['plan_id'], body[name],
                                                   'job %s' % job_id)]
        report['open_jobs'].append(entry)
    if report['duplicate_plans']:
        report['blocking'].append('%d AniDB anime have more than one trial plan'
                                  % len(report['duplicate_plans']))
    if report['unmapped']:
        report['blocking'].append('%d rows have no unique stored AniDB episode number'
                                  % len(report['unmapped']))
    if report['conflicts']:
        report['blocking'].append('%d plans would get duplicate AniDB keys'
                                  % len(report['conflicts']))
    return report


def _text(report):
    lines = ['AniDB-first selection preflight (read-only)',
             'schema version: %s' % report['schema_version'],
             'trial plans: %s' % report['plans']]
    for table, count in report['rows'].items():
        lines.append('%s rows: %s' % (table, count))
    lines.append('open jobs referencing plans: %s' % len(report['open_jobs']))
    for item in report['duplicate_plans']:
        lines.append('DUPLICATE PLANS anidb_id=%(anidb_id)s plan_ids=%(plan_ids)s' % item)
    for item in report['unmapped']:
        lines.append('UNMAPPED plan_id=%s table=%s tvdb=S%sE%s reason=%s%s' % (
            item['plan_id'], item['table'], item['placement'][0], item['placement'][1],
            item['reason'], (' (%s)' % item['detail']) if item.get('detail') else ''))
    for item in report['conflicts']:
        lines.append('CONFLICT plan_id=%(plan_id)s anidb=%(anidb_episode_number)s '
                     'tvdb_episodes=%(tvdb_episodes)s' % item)
    lines.append('RESULT: ' + ('BLOCKED - ' + '; '.join(report['blocking'])
                               if report['blocking'] else 'OK - conversion is unambiguous'))
    return '\n'.join(lines)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--db', required=True, help='Path to a COPY of the NextGen database')
    parser.add_argument('--json', action='store_true', help='Print the report as JSON')
    args = parser.parse_args(argv)
    try:
        db = _open(args.db, readonly=True, timeout_ms=5000)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print('cannot open database read-only: %s' % exc, file=sys.stderr)
        return 2
    try:
        db.execute('BEGIN')
        report = preflight(db)
    except sqlite3.Error as exc:
        print('database is not a NextGen v54 database: %s' % exc, file=sys.stderr)
        return 2
    finally:
        db.close()
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _text(report))
    return 1 if report['blocking'] else 0


if __name__ == '__main__':
    sys.exit(main())
