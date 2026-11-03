"""Atomic trial-plan anchoring and evaluation. No external resource claims."""
import json
import sqlite3
import time

from src.nextgen.history import integer, append_job_history
from src.nextgen.storage.job_queue import QueueUnavailable
from src.nextgen.storage.migrations import LATEST_VERSION, MigrationError, _open
from src.nextgen.trial_policy import TrialSource, TrialProposal, TrialConflict, Window


def _source(db,verification_job_id,event_id):
    row=db.execute("SELECT r.verification_id,r.job_id,j.event_id,r.anidb_id,r.tvdb_series_id,r.tvdb_season,r.tvdb_episode,"
                   "r.anidb_episode_number "
                   "FROM release_verifications r JOIN jobs j ON j.job_id=r.job_id "
                   "JOIN identified_anime a ON a.anidb_id=r.anidb_id JOIN incoming_events e ON e.event_id=j.event_id "
                   "WHERE r.job_id=? AND j.event_id=? AND j.state='DONE' AND j.kind='VERIFY_RELEASE' AND e.state='IDENTIFIED' "
                   "AND r.verification_scope='DECLARED_SINGLE_EPISODE' AND r.tvdb_order='official' "
                   "AND a.tvdb_series_id=r.tvdb_series_id",(verification_job_id,event_id)).fetchone()
    if row is None:raise TrialConflict()
    return TrialSource(*row)


def _delivery(db,event_id):
    """(source, release title) of the delivery; title is untrusted, a hint only."""
    row=db.execute("SELECT source,raw_body FROM incoming_events WHERE event_id=?",(event_id,)).fetchone()
    if row is None:raise TrialConflict()
    try:
        body=json.loads(row[1])
        title=body.get('release_name') if type(body) is dict else None
    except (ValueError,TypeError,UnicodeError,RecursionError):title=None
    return row[0],title if type(title) is str else ''


def load_trial_source(database,verification_job_id,event_id,*,with_delivery=False):
    integer(verification_job_id,positive=True)
    integer(event_id,positive=True)
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute("BEGIN")
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:raise QueueUnavailable()
        source=_source(db,verification_job_id,event_id)
        return (source,_delivery(db,event_id)) if with_delivery else source
    except (OSError,sqlite3.Error,MigrationError):raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()


def persist_trial(db,claim,proposal):
    """Caller owns fenced transaction. First committed plan fixes the anchor."""
    if not db.in_transaction or type(proposal) is not TrialProposal:raise ValueError('trial transaction and facts required')
    source=proposal.source
    if source.event_id!=claim.event_id or _source(db,source.verification_job_id,claim.event_id)!=source:
        raise TrialConflict()
    # One frozen plan per AniDB anime; later deliveries reuse it in any TVDB season.
    row=db.execute("SELECT plan_id,anchor_episode,backfill_count,following_count,anchor_verification_id FROM trial_plans "
                   "WHERE anidb_id=? AND tvdb_order='official'",(source.anidb_id,)).fetchone()
    if row is not None:
        anchor=db.execute("SELECT anidb_id,tvdb_series_id,anidb_episode_number,tvdb_order FROM release_verifications WHERE verification_id=?",(row[4],)).fetchone()
        if anchor!=(source.anidb_id,source.tvdb_series_id,row[1],'official'):raise TrialConflict()
        row=row[:4]
    now=integer(time.time_ns()//1000000)
    if row is None and (proposal.window.backfill_count or proposal.window.following_count):
        proposal.window.anidb_episodes(source.anidb_episode_number)
        plan_season = source.tvdb_season if source.tvdb_season is not None else 1
        plan=db.execute("INSERT INTO trial_plans(anidb_id,tvdb_series_id,tvdb_season,anchor_verification_id,anchor_episode,"
                        "backfill_count,following_count,configuration_revision,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                        (source.anidb_id,source.tvdb_series_id,plan_season,source.verification_id,source.anidb_episode_number,
                         proposal.window.backfill_count,proposal.window.following_count,proposal.configuration_revision,now)).lastrowid
        row=(plan,source.anidb_episode_number,proposal.window.backfill_count,proposal.window.following_count)
    if row is None:
        raise TrialConflict()
    else:
        plan,anchor,backfill,following=row
        selected=source.anidb_episode_number in Window(backfill,following).anidb_episodes(anchor)
        reason='TRIAL_SELECTED' if selected else 'TRIAL_OUTSIDE_WINDOW'
    db.execute("INSERT INTO trial_evaluations(job_id,verification_id,plan_id,selected,reason_code,evaluated_at_ms) VALUES(?,?,?,?,?,?)",
               (claim.job_id,source.verification_id,plan,int(selected),reason,now))
    return append_job_history(db,job_id=claim.job_id,event_code='TRIAL_EVALUATED',reason_code=reason,
                              component='worker',occurred_at_ms=now,details={'attempt':claim.attempt})
