"""Lease-fenced lifecycle on existing v8 jobs/history; caller holds admission.

Each operation owns a dedicated thread-local connection and short transaction.
No implicit database creation/migration, external calls or hidden job dispatch.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import inspect
import json
import sqlite3
import time
import uuid

from src.nextgen.cancellation import CancellationRequested
from src.nextgen.history import HistoryRecord, append_job_history, integer
from src.nextgen.job_contracts import Outcome, Policy, Successor, parse_payload
from src.nextgen.matching import from_json as matching_from_json
from src.nextgen.messages import validate_job_activity
from src.nextgen.storage.migrations import _open, LATEST_VERSION
from src.nextgen.storage.sqlite import enqueue_job_with_receipt, transaction


class LeaseLost(RuntimeError):
    reason_code = "JOB_LEASE_EXPIRED"
    def __init__(self):
        super().__init__("Job ownership is no longer valid.")


class QueueUnavailable(RuntimeError):
    reason_code = "STORAGE_UNAVAILABLE"
    def __init__(self):
        super().__init__("Job storage is unavailable.")


class StorageStatementInvalid(QueueUnavailable):
    """A SQL statement was rejected as invalid: a programming defect, not an outage.

    Raised for sqlite3.ProgrammingError/InterfaceError, such as a wrong
    number of bound parameters or an unsupported parameter type. Subclassing
    QueueUnavailable keeps every existing recovery path unchanged; only the
    reported reason and message differ. failed_check names the statement site.
    """
    reason_code = "STORAGE_STATEMENT_INVALID"
    def __init__(self, failed_check=None):
        RuntimeError.__init__(self, "A database statement is invalid (programming error).")
        self.failed_check = failed_check


def _statement_site(exc):
    """failed_check for the innermost NextGen line that ran the statement.

    Uses the traceback's own line (tb_lineno). The caller's frame is still
    executing its `with self.connection()` exit here, so its f_lineno would
    name the `with` line rather than the failing execute().
    """
    from src.nextgen.reasons import _CHECK
    site, tb = None, exc.__traceback__
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if "src/nextgen/" in filename:
            site = (filename, tb.tb_lineno, tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    if site is None:
        return None
    check = ("src/nextgen/" + site[0].split("src/nextgen/")[-1] + f":{site[1]} {site[2]} "
             + type(exc).__name__[:32].upper())
    return check if _CHECK.fullmatch(check) else None


@dataclass(frozen=True)
class Claim:
    job_id: int
    event_id: int | None
    kind: str
    payload: str = field(repr=False)
    attempt: int
    token: str = field(repr=False)

    def data(self):
        return parse_payload(self.payload)


class JobQueue:
    def __init__(self, database, *, policy=Policy(), clock=None):
        self.database, self.policy = database, policy
        self.clock = clock or (lambda: time.time_ns() // 1000000)

    @contextmanager
    def connection(self):
        db = None
        try:
            db = _open(self.database, readonly=False,timeout_ms=5000)
            db.execute("PRAGMA busy_timeout=5000")
            if db.execute("PRAGMA user_version").fetchone()[0] != LATEST_VERSION:
                raise QueueUnavailable()
            yield db
        except (sqlite3.ProgrammingError, sqlite3.InterfaceError) as exc:
            # Deterministic statement defects. The original error stays in
            # __context__, so exact_error() still logs sqlite's own message.
            raise StorageStatementInvalid(_statement_site(exc)) from None
        except (sqlite3.Error, OSError):
            raise QueueUnavailable() from None
        finally:
            if db is not None:
                db.close()

    def now(self):
        return integer(self.clock())

    def _owns(self, db, claim, now):
        row = db.execute("SELECT state,lease_token,lease_expires_at_ms,attempt_count FROM jobs WHERE job_id=?",
                         (claim.job_id,)).fetchone()
        if row is None or row[0] != "RUNNING" or row[1] != claim.token or row[2] is None or row[2] <= now or row[3] != claim.attempt:
            raise LeaseLost()

        row=db.execute('SELECT o.state FROM job_operations o JOIN job_operation_members m USING(operation_id) WHERE m.job_id=?',(claim.job_id,)).fetchone()
        if row is not None and row[0] in ('CANCEL_REQUESTED','CANCELED'):raise CancellationRequested()

    def operation_id(self,claim):
        with self.connection() as db,transaction(db):
            row=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',(claim.job_id,)).fetchone()
            if row is None:raise QueueUnavailable()
            return row[0]

    def claim(self, halted=None, exclude_kinds=None):
        with self.connection() as db, transaction(db):
            now = self.now()
            if halted is not None and halted():
                return None, ()
            sql = ("SELECT job_id,event_id,kind,CASE WHEN length(CAST(payload_json AS BLOB))<=65536 "
                "THEN payload_json ELSE '' END,attempt_count FROM jobs "
                "WHERE state IN ('PENDING','RETRY_WAIT') AND available_at_ms<=? AND NOT EXISTS (SELECT 1 FROM job_operation_members m JOIN job_operations o USING(operation_id) WHERE m.job_id=jobs.job_id AND o.state<>'ACTIVE') ")
            params = [now]
            if exclude_kinds:
                sql += f"AND kind NOT IN ({','.join('?' for _ in exclude_kinds)}) "
                params.extend(exclude_kinds)
            sql += "ORDER BY available_at_ms,job_id LIMIT 1"
            row = db.execute(sql, params).fetchone()
            if row is None:
                return None, ()
            if db.execute('SELECT 1 FROM job_operation_members WHERE job_id=?',(row[0],)).fetchone() is None:
                op=db.execute('INSERT INTO job_operations(created_at_ms,updated_at_ms) VALUES(?,?)',(now,now)).lastrowid
                db.execute('INSERT INTO job_operation_members VALUES(?,?)',(row[0],op))
            token = uuid.uuid4().hex
            claim = Claim(row[0], row[1], row[2], row[3], row[4]+1, token)
            db.execute("UPDATE jobs SET state='RUNNING',attempt_count=?,lease_token=?,lease_expires_at_ms=?,"
                       "activity_code=NULL,updated_at_ms=? WHERE job_id=?",
                       (claim.attempt,token,now+self.policy.lease_ms,now,claim.job_id))
            record = append_job_history(db,job_id=claim.job_id,event_code="CLAIMED",component="worker",
                                        occurred_at_ms=now,details={"attempt":claim.attempt})
            return claim, (record,)

    def renew(self, claim):
        with self.connection() as db, transaction(db):
            now = self.now()
            self._owns(db, claim, now)
            db.execute("UPDATE jobs SET lease_expires_at_ms=?,updated_at_ms=? WHERE job_id=?",
                       (now+self.policy.lease_ms,now,claim.job_id))

    def start(self, claim, spec, invocation, registry):
        if registry.entries.get(claim.kind) is not spec:
            raise ValueError("handler does not match claimed kind")
        with self.connection() as db, transaction(db):
            now = self.now()
            self._owns(db,claim,now)
            return append_job_history(db,job_id=claim.job_id,event_code="HANDLER_STARTED",component="worker",
                occurred_at_ms=now,details={"attempt":claim.attempt},handler_code=spec.definition.code,
                invocation_id=invocation,handlers=registry.labels)

    def _transition(self, db, claim, outcome, now):
        details = {"attempt":claim.attempt,**(outcome.facts or {})}
        if outcome.metadata_evidence is not None:
            details["metadata_evidence"] = outcome.metadata_evidence
        if outcome.matching is not None:
            if claim.kind != "IDENTIFY_ANIME":
                raise ValueError("matching evidence only belongs to an identity attempt")
            details["matching"] = matching_from_json(outcome.matching)
        activity = outcome.activity_code
        reason = outcome.reason_code
        if outcome.kind in ("complete", "skip"):
            state, code, available = "DONE", "DONE", now
        elif outcome.kind in ("retry", "defer"):
            # A manual retry begins a new failure-budget window. Historical
            # attempts and RETRY_SCHEDULED records remain immutable.
            failures = db.execute(
                "SELECT count(*) FROM job_history WHERE job_id=? AND event_code='RETRY_SCHEDULED' "
                "AND history_id>COALESCE((SELECT max(history_id) FROM job_history "
                "WHERE job_id=? AND event_code='RETRY_REQUESTED'),0)",
                (claim.job_id, claim.job_id)).fetchone()[0]
            if outcome.kind == "retry" and failures >= self.policy.max_retries:
                state, code, available, activity = "NEEDS_REVIEW", "NEEDS_REVIEW", now, None
                details["cause_reason_code"], reason = reason, "JOB_RETRY_EXHAUSTED"
                if "failed_check" not in details:
                    prior_check = db.execute(
                        "SELECT json_extract(details_json, '$.failed_check') FROM job_history "
                        "WHERE job_id=? AND json_extract(details_json, '$.failed_check') IS NOT NULL "
                        "ORDER BY history_id DESC LIMIT 1",
                        (claim.job_id,),
                    ).fetchone()
                    if prior_check and prior_check[0]:
                        details["failed_check"] = prior_check[0]
                if "failed_path" not in details:
                    prior_path = db.execute(
                        "SELECT json_extract(details_json, '$.failed_path') FROM job_history "
                        "WHERE job_id=? AND json_extract(details_json, '$.failed_path') IS NOT NULL "
                        "ORDER BY history_id DESC LIMIT 1",
                        (claim.job_id,),
                    ).fetchone()
                    if prior_path and prior_path[0]:
                        details["failed_path"] = prior_path[0]
            else:
                delay = outcome.delay_ms
                if outcome.kind == "retry":
                    delay = max(delay or 0, min(self.policy.max_backoff_ms, self.policy.backoff_ms * 2**min(failures,30)))
                state, code, available = "RETRY_WAIT", "RETRY_SCHEDULED" if outcome.kind == "retry" else "DEFERRED", now+delay
        else:
            state = {"hold":"ON_HOLD","review":"NEEDS_REVIEW","blocked":"BLOCKED","cancel":"CANCELED","fail":"FAILED"}[outcome.kind]
            code, available = state, now
        validate_job_activity(state,activity)
        db.execute("UPDATE jobs SET state=?,available_at_ms=?,lease_token=NULL,lease_expires_at_ms=NULL,"
                   "last_reason_code=?,activity_code=?,updated_at_ms=? WHERE job_id=?",
                   (state,available,reason,activity,now,claim.job_id))
        return append_job_history(db,job_id=claim.job_id,event_code=code,reason_code=reason,
                                  component="worker",occurred_at_ms=now,details=details)

    def finish(self, claim, outcome, *, spec, invocation, registry):
        if type(outcome) is not Outcome:
            raise ValueError("handler must return a typed outcome")
        if spec is not None and registry.entries.get(claim.kind) is not spec:
            raise ValueError("handler does not match claimed kind")
        with self.connection() as db, transaction(db):
            now = self.now()
            self._owns(db,claim,now)
            records = []
            if spec is not None:
                failed = outcome.kind in ("retry", "blocked")
                records.append(append_job_history(db,job_id=claim.job_id,
                    event_code="HANDLER_FAILED" if failed else "HANDLER_COMPLETED",component="worker",
                    reason_code=outcome.reason_code if failed else None,occurred_at_ms=now,
                    details={"attempt":claim.attempt},handler_code=spec.definition.code,
                    invocation_id=invocation,handlers=registry.labels))
            elif outcome.kind != "review" or outcome.reason_code not in {"UNSUPPORTED_JOB_KIND", "JOB_PAYLOAD_INVALID"}:
                raise ValueError("missing registered handler")
            if outcome.kind in ("complete", "cancel") and spec is not None:
                if spec.persist is not None and (outcome.kind == "complete" or outcome.value is not None):
                    # A feature adapter cannot commit/rollback caller-owned work.
                    db.set_authorizer(lambda action, arg1, arg2, database, source:
                        sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT) else sqlite3.SQLITE_OK)
                    try:
                        result = spec.persist(db,claim,outcome.value)
                    finally:
                        db.set_authorizer(None)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result): result.close()
                        raise ValueError("completion persistence cannot await")
                    if result is not None:
                        if type(result) is not tuple or any(type(item) is not HistoryRecord for item in result):
                            raise ValueError("completion persistence must return audit receipts or None")
                        records.extend(result)
                if outcome.kind == "complete":
                    successors = () if spec.route is None else spec.route(outcome.value)
                    if type(successors) is not tuple or len(successors) > 32:
                        if inspect.iscoroutine(successors): successors.close()
                        raise ValueError("invalid successor plan")
                    for successor in successors:
                        if type(successor) is not Successor or successor.kind not in spec.successor_kinds:
                            raise ValueError("successor not allowed by registration")
                        # enqueue_job owns no transaction/commit; all inserts roll back together.
                        _, created = enqueue_job_with_receipt(db,component="worker",kind=successor.kind,
                            payload=json.loads(successor.payload), event_id=claim.event_id,
                            created_by_job_id=claim.job_id,available_at_ms=now,
                            operation_id=db.execute("SELECT operation_id FROM job_operation_members WHERE job_id=?",(claim.job_id,)).fetchone()[0])
                        records.append(created)
            now = self.now()
            self._owns(db,claim,now)
            if getattr(outcome, 'search_snapshots', None):
                from src.nextgen.storage.prowlarr_snapshots import save_snapshots
                save_snapshots(db, claim.job_id, claim.attempt, outcome.search_snapshots, now)
            if getattr(outcome, 'candidate_evidence', None):
                from src.nextgen.storage.candidate_evidence import save_candidate_evidence
                save_candidate_evidence(db, claim.job_id, claim.attempt, outcome.candidate_evidence, now)
            records.append(self._transition(db,claim,outcome,now))
            # Failed Sonarr candidates use the existing cancellation worker,
            # never the user-facing Cancel button. Non-Sonarr jobs are untouched.
            if db.execute('SELECT state FROM jobs WHERE job_id=?',
                          (claim.job_id,)).fetchone() in (('FAILED',), ('NEEDS_REVIEW',)):
                from src.nextgen.storage.sonarr_candidates import (
                    auto_cleanup_failed_candidate, auto_cleanup_oversized)
                from src.nextgen.storage.batch_candidates import auto_cleanup_failed_batch_candidate
                records.extend(auto_cleanup_failed_candidate(db,claim))
                records.extend(auto_cleanup_oversized(db,claim))
                records.extend(auto_cleanup_failed_batch_candidate(db,claim))
            return tuple(records)

    def recover_expired(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid lease recovery bound")
        with self.connection() as db, transaction(db):
            now = self.now()
            rows = db.execute("SELECT job_id,event_id,kind,'',attempt_count,lease_token FROM jobs "
                "WHERE state='RUNNING' AND lease_expires_at_ms<=? ORDER BY lease_expires_at_ms,job_id LIMIT ?",
                (now,limit)).fetchall()
            records = []
            for row in rows:
                claim = Claim(row[0],row[1],row[2],row[3],row[4],row[5])
                canceled=db.execute("SELECT 1 FROM job_operation_members m JOIN job_operations o USING(operation_id) "
                    "WHERE m.job_id=? AND o.state IN ('CANCEL_REQUESTED','CANCELED')",(claim.job_id,)).fetchone()
                if canceled:
                    from src.nextgen.storage.cancellation import cancel_job
                    records.extend(cancel_job(db,claim.job_id,now))
                    continue
                if claim.attempt == 0:
                    # Older storage permitted RUNNING without a claim count.
                    # Quarantine without inventing processing history.
                    records.append(self._transition(db,claim,Outcome("review","JOB_STATE_INVALID"),now))
                    continue
                records.append(append_job_history(db,job_id=claim.job_id,event_code="LEASE_EXPIRED",
                    reason_code="JOB_LEASE_EXPIRED",component="worker",occurred_at_ms=now,details={"attempt":claim.attempt}))
                records.append(self._transition(db,claim,Outcome("retry","JOB_LEASE_EXPIRED"),now))
            return tuple(records)
