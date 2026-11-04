"""Atomic shared-episode batch reservations; never authorizes torrent mutation.

The reservation manager is an explicit opt-in worker primitive until batch
acquisition and safe cleanup are installed. A serial SQLite worker transaction
arbitrates against BOTH existing single-episode and batch owners.
"""
from dataclasses import dataclass
import sqlite3
import time

from src.nextgen.episode_reservations import ReservationConflict, EpisodeReserved
from src.nextgen.history import integer, append_job_history
from src.nextgen.storage.batch_trials import _source as trial_source
from src.nextgen.storage.job_queue import QueueUnavailable
from src.nextgen.storage.migrations import LATEST_VERSION, MigrationError, _open
from src.nextgen.trial_policy import TrialConflict


@dataclass(frozen=True)
class BatchReservationFact:
    file_index: int
    anidb_episode_id: int
    tvdb_episode: int
    tvdb_episode_id: int

    def __post_init__(self):
        integer(self.file_index)
        for item in (self.anidb_episode_id,self.tvdb_episode,self.tvdb_episode_id):
            integer(item,positive=True)


@dataclass(frozen=True)
class BatchReservationInput:
    evaluation_job_id: int
    event_id: int
    plan_id: int
    batch_id: int
    anidb_id: int
    tvdb_series_id: int
    season: int
    episodes: tuple[BatchReservationFact,...]

    def __post_init__(self):
        for value in (self.evaluation_job_id,self.event_id,self.plan_id,self.batch_id,
                      self.anidb_id,self.tvdb_series_id,self.season):
            integer(value,positive=True)
        if (type(self.episodes) is not tuple or not self.episodes
                or any(type(f) is not BatchReservationFact for f in self.episodes)):
            raise ReservationConflict()


def _facts(db,evaluation_job_id,event_id):
    row=db.execute(
        "SELECT p.plan_id,p.batch_id,b.anidb_id,b.tvdb_series_id,b.tvdb_season,"
        "b.job_id,p.anchor_episode,p.backfill_count,p.following_count,"
        "p.planned_size_bytes "
        "FROM batch_trial_plans p JOIN batch_verifications b USING(batch_id) "
        "JOIN jobs j ON j.job_id=p.evaluation_job_id "
        "WHERE p.evaluation_job_id=? AND j.event_id=? AND j.state='DONE' "
        "AND j.kind='EVALUATE_BATCH'",(evaluation_job_id,event_id)).fetchone()
    if row is None:raise ReservationConflict()
    plan_id,batch_id,aid,tid,season,verification_job,anchor,backfill,following,planned_size=row
    try:source=trial_source(db,verification_job,event_id)
    except TrialConflict:raise ReservationConflict() from None
    if (source.batch_id,source.anidb_id,source.tvdb_series_id,source.season)!=(batch_id,aid,tid,season):
        raise ReservationConflict()
    from src.nextgen.trial_policy import Window
    try:allowed=set(Window(backfill,following).episodes(anchor))
    except (ValueError,TrialConflict):raise ReservationConflict() from None
    selected=db.execute(
        "SELECT f.file_index,f.anidb_episode_id,f.tvdb_episode,f.tvdb_episode_id,"
        "f.file_size_bytes,t.selected_size_bytes "
        "FROM batch_trial_files t JOIN batch_verified_files f "
        "ON f.batch_id=? AND f.file_index=t.file_index "
        "WHERE t.plan_id=? ORDER BY f.tvdb_episode,f.file_index",
        (batch_id,plan_id)).fetchall()
    if not selected or len(selected)>10000 or any(
            tvdb_episode not in allowed or size!=stored_size
            for idx,aid_id,tvdb_episode,tvdb_id,size,stored_size in selected):
        raise ReservationConflict()
    source_selected={f.tvdb_episode:f for f in source.episodes if f.tvdb_episode in allowed}
    if (len(selected)!=len(source_selected)
            or planned_size!=sum(row[4] for row in selected)
            or any((idx,aid_id,tvdb_episode,tvdb_id,size)!=(
                source_selected[tvdb_episode].file_index,
                source_selected[tvdb_episode].anidb_episode_id,
                source_selected[tvdb_episode].tvdb_episode,
                source_selected[tvdb_episode].tvdb_episode_id,
                source_selected[tvdb_episode].size_bytes)
                for idx,aid_id,tvdb_episode,tvdb_id,size,_ in selected)):
        raise ReservationConflict()
    facts=tuple(BatchReservationFact(idx,anidb_episode,tvdb_episode,tvdb_id)
                for idx,anidb_episode,tvdb_episode,tvdb_id,*_ in selected)
    return BatchReservationInput(evaluation_job_id,event_id,plan_id,batch_id,
                                 aid,tid,season,facts)


def load_batch_reservation(database,evaluation_job_id,event_id):
    integer(evaluation_job_id,positive=True)
    integer(event_id,positive=True)
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:
            raise QueueUnavailable()
        return _facts(db,evaluation_job_id,event_id)
    except (OSError,sqlite3.Error,MigrationError):raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()


def persist_batch_reservations(db,claim,value):
    if not db.in_transaction or type(value) is not BatchReservationInput:
        raise ValueError('Worker transaction and batch reservation proof required')
    if (claim.kind!='RESERVE_BATCH' or claim.event_id!=value.event_id
            or _facts(db,value.evaluation_job_id,claim.event_id)!=value):
        raise ReservationConflict()
    now=integer(time.time_ns()//1000000)
    job=db.execute('SELECT state,attempt_count,lease_token,lease_expires_at_ms '
                   'FROM jobs WHERE job_id=?',(claim.job_id,)).fetchone()
    if (job is None or job[0]!='RUNNING' or job[1]!=claim.attempt
            or job[2]!=claim.token or job[3] is None or job[3]<=now):
        raise ReservationConflict()
    if db.execute('SELECT 1 FROM batch_episode_reservations WHERE plan_id=? OR owner_job_id=?',
                  (value.plan_id,claim.job_id)).fetchone():
        raise ReservationConflict()
    for fact in value.episodes:
        key=(value.tvdb_series_id,value.season,fact.tvdb_episode)
        # Deliberately fail the entire pack if any selected episode has an
        # active owner, including all existing single-episode jobs.
        single_owner = db.execute(
            "SELECT owner_job_id FROM episode_reservations WHERE state='ACTIVE' AND "
            "(anidb_episode_id=? OR tvdb_episode_id=? OR "
            "(tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=? AND tvdb_order='official')) LIMIT 1",
            (fact.anidb_episode_id,fact.tvdb_episode_id,*key)).fetchone()
        if single_owner:
            owner_id = single_owner[0]
            op = db.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?", (owner_id,)).fetchone()
            raise EpisodeReserved(owner_job_id=owner_id, operation_id=op[0] if op else None)
        batch_owner = db.execute(
            "SELECT owner_job_id FROM batch_episode_reservations WHERE state='ACTIVE' AND "
            "(anidb_episode_id=? OR tvdb_episode_id=? OR "
            "(tvdb_series_id=? AND tvdb_season=? AND tvdb_episode=?)) LIMIT 1",
            (fact.anidb_episode_id,fact.tvdb_episode_id,*key)).fetchone()
        if batch_owner:
            owner_id = batch_owner[0]
            op = db.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?", (owner_id,)).fetchone()
            raise EpisodeReserved(owner_job_id=owner_id, operation_id=op[0] if op else None)
    # The caller owns a BEGIN IMMEDIATE transaction. Either all selected
    # reservations become visible or none do; there is no partial pack claim.
    for fact in value.episodes:
        db.execute(
            'INSERT INTO batch_episode_reservations(plan_id,owner_job_id,file_index,'
            'anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,'
            'tvdb_episode_id,reserved_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (value.plan_id,claim.job_id,fact.file_index,value.anidb_id,
             fact.anidb_episode_id,value.tvdb_series_id,value.season,
             fact.tvdb_episode,fact.tvdb_episode_id,now))
    return append_job_history(db,job_id=claim.job_id,
                              event_code='EPISODE_RESERVATION_CREATED',
                              reason_code='EPISODE_RESERVATION_GRANTED',
                              component='worker',occurred_at_ms=now,
                              details={'attempt':claim.attempt})
