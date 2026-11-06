"""Identify one incoming delivery through a trusted async identity library.

Provider verification, shared rate limits and secret access belong to that
library. Neither a webhook nor a matching title is proof of verified identity.
No bundled live resolver, provider HTTP requests or acquisition are installed.
"""
from dataclasses import dataclass, field
import inspect
import sqlite3

from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration, Successor, parse_payload, payload_json
from src.nextgen.schema.anime_series import AnimeSeries
from src.nextgen.providers.errors import (ProviderUnavailable, ProviderDeferred,
    ProviderInvalidResponse, ProviderAuthenticationFailed)
from src.nextgen.storage.job_queue import QueueUnavailable
from src.nextgen.storage.migrations import LATEST_VERSION, MigrationError, _open
from src.nextgen.storage.sqlite import IdentityConflict, IdentityNotReady, persist_identity
from src.nextgen.worker import blocking
from src.nextgen.matching import MatchingTrace

KIND = "IDENTIFY_ANIME"
DEFINITION = HandlerDefinition(KIND, "Identify anime series")


@dataclass(frozen=True)
class IdentityRequest:
    event_id: int
    source: str
    raw_body: bytes = field(repr=False)



@dataclass(frozen=True)
class IdentifiedSeries:
    event_id: int
    series_json: str = field(repr=False)

    def __post_init__(self):
        integer(self.event_id,positive=True)
        candidate = parse_payload(self.series_json)
        AnimeSeries.from_dict(candidate)

    def data(self):
        return parse_payload(self.series_json)


def _request(database, event_id):
    integer(event_id,positive=True)
    db = None
    try:
        db = _open(database,readonly=True,timeout_ms=5000)
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != LATEST_VERSION:
            raise QueueUnavailable()
        row = db.execute("SELECT source,CASE WHEN length(raw_body)<=65536 THEN raw_body ELSE NULL END,"
                         "state FROM incoming_events WHERE event_id=?",(event_id,)).fetchone()
        if (row is None or type(row[1]) is not bytes
                or row[2] not in ("PENDING", "RETRY_WAIT", "IDENTIFIED")):
            raise IdentityNotReady()
        return IdentityRequest(event_id,row[0],row[1])
    except (sqlite3.Error, OSError, MigrationError):
        raise QueueUnavailable() from None
    finally:
        if db is not None:
            db.close()


def _persist(db, claim, result):
    if type(result) is not IdentifiedSeries or result.event_id != claim.event_id:
        raise ValueError("identity result does not match its delivery")
    try:
        _, audit = persist_identity(db,claim.event_id,result.data())
    except (IdentityConflict, IdentityNotReady) as exc:
        raise CompletionRejected(Outcome("review",exc.reason_code)) from None
    return () if audit is None else (audit,)


def registration(resolve, *, release_kind=None):
    """Compose with an async verified-identity service, optionally a release job.

    resolve(request, operation_configuration, cancellation_event) returns a
    provider-verified AnimeSeries dictionary, or raises a documented typed error.
    It must verify AniDB/TVDB identity independently of untrusted delivery hints.
    It owns shared provider pacing and bounded I/O; it must not mutate the queue.
    An optional release kind must actually be installed in the same Registry.
    """
    if not inspect.iscoroutinefunction(resolve):
        raise ValueError("an async identity resolver is required")
    supports_matching = "matching" in inspect.signature(resolve).parameters
    if release_kind is not None and (type(release_kind) is not str or not release_kind.strip()
                                     or len(release_kind) > 64 or release_kind == KIND):
        raise ValueError("invalid release successor kind")

    async def execute(context):
        # The delivery is the durable source. Never accept a candidate schema,
        # verified flag, executable name or alternate event ID from payload.
        if context.data() != {} or context.claim.event_id is None:
            return Outcome("review", "JOB_PAYLOAD_INVALID")
        trace = MatchingTrace()
        def evidence():
            return trace.dump()
        try:
            request = await blocking(_request, context.configuration.value("storage.database_path"),
                                     context.claim.event_id)
            if supports_matching:
                candidate = await resolve(request, context.configuration, context.cancelled, matching=trace)
            else:
                candidate = await resolve(request, context.configuration, context.cancelled)
        except ProviderDeferred as exc:
            return Outcome("defer", "JOB_WAITING", delay_ms=exc.delay_ms, matching=evidence(), candidate_evidence=trace.candidate_evidence)
        except (ProviderUnavailable, ProviderInvalidResponse) as exc:
            return Outcome("retry", exc.reason_code, matching=evidence(), candidate_evidence=trace.candidate_evidence)
        except ProviderAuthenticationFailed:
            trace.check("tvdb_series", "failed", "provider_auth_failed")
            return Outcome("review", "SERVICE_SETUP_INVALID", facts={"service_status": "AUTH_FAILED"}, matching=evidence(), candidate_evidence=trace.candidate_evidence)
        except (IdentityNotReady, IdentityConflict) as exc:
            return Outcome("review", exc.reason_code, matching=evidence(), candidate_evidence=trace.candidate_evidence)
        try:
            validated = AnimeSeries.from_dict(candidate)
            result = IdentifiedSeries(request.event_id, payload_json(validated.to_dict()))
        except (ValueError, TypeError, RecursionError, OverflowError):
            return Outcome("review", "IDENTITY_INVALID", matching=evidence(), candidate_evidence=trace.candidate_evidence)
        return Outcome(value=result, matching=evidence(), candidate_evidence=trace.candidate_evidence)

    def route(result):
        if type(result) is not IdentifiedSeries:
            raise ValueError("invalid identity completion")
        if release_kind is None:
            return ()
        # Series identity never authorizes acquisition. The next job must prove
        # exact episode coverage; resolve the immutable snapshot by event/anime.
        return (Successor.create(release_kind,{"anidb_id":result.data()["anime"]["anidb_id"]}),)

    return Registration(KIND,DEFINITION,execute,successor_kinds=() if release_kind is None else (release_kind,),
                        route=route,persist=_persist)
