# NextGen shared diagnostic infrastructure contract

Status: implemented on the PR #100 branch; source-reviewed and syntax-checked,
but **no offline tests or CI have been executed**. This is a shared library,
not a running operational service. The worker remains the next component.

Implementation reuses NextGen settings, stdlib logging/levels, reason messages,
redaction patterns and storage helpers. Legacy informed the shared-message and
exception-handling design; no legacy runtime is imported or modified.

## Outcome and scope

Every essential delivery/job transition must have bounded, interpretable audit
history committed with its state change. Runtime logs add troubleshooting
detail; they never replace history or determine retry policy.

This implementation belongs in diagnostic-infrastructure PR #100 before
worker execution. Keep the domain AnimeSeries v2 untouched. No provider,
acquisition, dashboard, management API, or process supervision implementation
is required to prove this foundation.

## Mandatory human-readable messages

Follow [HUMAN_READABLE_MESSAGES.md](HUMAN_READABLE_MESSAGES.md): **every emitted
marker, reason/error, checkpoint and displayed state requires central English
copy and a stable namespaced translation key**. Journal entries include both
codes and resolved sentences; dashboard and CLI use the same catalog.
Registration must cover normal waiting/progress/success as well as errors.
Do not synthesize prose by replacing underscores or interpreting exceptions.

The existing reasons.py English messages/actions are the reason source of
truth. messages.py implements event/state catalogs and the common renderer alongside
history.py durable writers. MessageCatalog accepts validated trusted locale
maps as immutable snapshots; missing locale entries fall back to English.
Translation file loading and user locale configuration remain later work.
The renderer uses complete static sentences with variable facts kept separate.

## One contract, two destinations

| Fact | Durable SQLite history | Runtime structured log |
| --- | --- | --- |
| What happened | Registered event_code | Same event_code as the event name plus resolved English event message |
| Why, when relevant | Registered reason_code, nullable | Same reason_code and resolved reason message |
| Delivery correlation | event_id FK, directly or through jobs | event_id and derived trace_id when present |
| Independent work | job_id FK | job_id |
| Provenance | jobs.created_by_job_id, nullable | Optional bounded created_by_job_id |
| Attempt and recovery facts | Validated safe_details fields | Validated attempt and contextual decision |
| Emitting component | component (nullable only for historical rows) | component |
| Timing | occurred_at_ms; history_id orders ties | log time, monotonic elapsed_ms when available |
| Troubleshooting | No raw exception or payload | Catch-boundary exception type/traceback under a defined safe-output policy |

Write the durable record in the transaction first. Emit the corresponding
best-effort lifecycle log after commit; rolled-back transitions must not be
logged as completed. A crash between commit and log is acceptable because the
durable history is authoritative. Do not add an outbox solely to guarantee logs.
A logging sink failure after commit must not undo or relabel committed work.
An exception during the transaction rolls back both state and history.
Storage failure itself may only reach runtime diagnostics until recovery.

## Stable lifecycle and runtime catalog

Scopes and reason requirements are validated by the shared writer.

| event_code | Scope | Reason | Default English message |
| --- | --- | --- | --- |
| ACCEPTED | incoming event | Absent | Release received. |
| SERIES_IDENTIFIED | incoming event | Absent | Anime identified. |
| CREATED | job | Absent | Job created. |
| CLAIMED | job | Absent | Job started. |
| LEASE_EXPIRED | job | JOB_LEASE_EXPIRED | The job was interrupted before it finished. |
| RETRY_SCHEDULED | job | Required | Another attempt is scheduled. |
| NEEDS_REVIEW | job | Required | Stopped; needs review. |
| DONE | job | Absent | Job finished. |
| ON_HOLD | job | Required | Job on hold. |
| BLOCKED | job | Required | Job blocked. |
| CANCELED | job | Required | Job canceled. |
| OPERATION_FAILED | runtime log only | Required | An operation failed. |

OPERATION_FAILED is a catch-boundary observation, not a durable job transition
or a new FAILED state. Event message keys are event.<CODE>.message. SERIES_IDENTIFIED does not imply
episode coverage or acquisition. DONE means only this independent job completed;
successor creation, when required, must still be in the same transaction.
LEASE_EXPIRED records the observed expiry during ownership-fenced recovery.

Display states require state.<entity>.<CODE>.label:

| Entity | Code | Default English label |
| --- | --- | --- |
| job | PENDING | Queued |
| incoming_event | PENDING | Received |
| job | RUNNING | Running |
| incoming_event | RUNNING | Identifying |
| job, incoming_event | RETRY_WAIT | Waiting for another attempt |
| job, incoming_event | NEEDS_REVIEW | Needs review |
| job | DONE | Completed |
| job | ON_HOLD | On hold |
| job | BLOCKED | Blocked |
| job | CANCELED | Canceled |
| incoming_event | IDENTIFIED | Series identified |

The two entities use separate keys even where English text matches. These
labels cover all states currently permitted by SQLite v8; they add no state.
All reason codes use reasons.py copy and reason.<CODE>.message/action keys.

These are new-emission rules, not a rejection policy for historical reads.
Unknown historical event/reason codes must remain readable using generic copy.
Reject unregistered new event/reason codes at write time.
LEASE_EXPIRED and the resulting RETRY_SCHEDULED or NEEDS_REVIEW can be separate
history rows in one transaction; they do not introduce a new job state.
The worker later validates transitions and determines retry eligibility.
The diagnostic writer validates audit shape, not scheduler business policy.
CANCELED, BLOCKED and ON_HOLD are now explicit stored states. No FAILED state
is introduced. Typed activity and its history snapshots are documented in
[JOB_STATUSES.md](JOB_STATUSES.md); they must not mask a terminal/held/review state.

## Correlation and attempts

- Derive delivery trace_id as AD-<event_id> from the database identity, never
  from provider text, release title, GUID, or user-supplied trace values.
- Jobs without an event use JOB-<job_id>. These labels are stable within one
  NextGen database, not globally unique across separate installations.
- A job associated with a delivery logs both event_id/AD trace and job_id.
  Retries retain job_id and event_id; attempt_count increments only on claim.
- created_by_job_id remains optional provenance. Follow it to explain which
  job requested another job; never infer dependencies or lifecycle propagation.
  A successor's event association must be explicit and validated by its creator.
- CLAIMED and subsequent attempt outcomes record attempt in safe_details.
  CREATED may use attempt=0. For RETRY_SCHEDULED, attempt describes the
  attempt just concluded, not a pre-incremented future claim.
- Retry-exhaustion details retain max_attempts and cause_reason_code; the
  top-level reason is JOB_RETRY_EXHAUSTED.
- Do not store lease tokens in diagnostic history or logs. The worker must still use them in
  conditional state mutations; history insertion does not confer ownership.

## Durable writer boundary

Shared APIs in history.py: append_event_history and append_job_history. Each requires a caller-owned active SQLite
transaction, validates typed and bounded fields before INSERT, and never
commits, opens a connection, changes job state, retries SQL, or logs success.
Reject use outside an active transaction so a history write cannot silently
autocommit separately from its transition.

Required facts: positive signed-64-bit target ID; nonnegative signed-64-bit
occurred_at_ms; known event_code and reason rules; known emitting component;
details rebuilt through safe_details, not json.dumps of arbitrary input.
Unknown detail keys, malformed values and excessive sizes fail with static
validation messages without echoing input. Serialize canonical JSON with
allow_nan=False. Enforce a small encoded-size cap (4 KiB) in addition to
per-field bounds. History writers are internal interfaces, not authorization.

accept_event, accept_event_and_job, enqueue_job and save_identity use the
common writers. The transaction(db) context manager starts BEGIN IMMEDIATE
and owns commit/rollback, including on autocommit connections; it rejects
nested ownership. enqueue_job requires an already active transaction and an
explicit component argument. It inserts CREATED in the same transaction as
its job row; accept_event_and_job adds no duplicate. save_identity idempotent
returns do not append duplicate successful transitions.
An intake-created job's emitting component is intake; a job created by a
handler records the actual creator component, not an inferred parent role.
Caller APIs must make component attribution explicit where it is ambiguous.

## SQLite v4/v5/v6 and compatibility

Durable component attribution cannot be reconstructed reliably from the
existing v3 rows. 004_diagnostic_components.sql therefore provides the v3→v4 migration
rather than hiding an unversioned field in JSON.

The migration adds nullable component TEXT to event_history and job_history, constrained to
the same current component vocabulary as settings.py. NULL means historical
origin was not captured; new shared writers require an explicit valid value.
Preserve all existing rows and their unknown reason/event text.
Record attempts using the bounded details fields already introduced by #97.
No new trace column, provenance relationship, or domain field is necessary.

PR #100 includes 004_diagnostic_components.sql, matching fresh SCHEMA and
user_version=6, storage helper changes, schema-policy documentation and offline
fresh-install/sequential-upgrade tests. Published 001/002/003/004 are untouched
by the v5 follow-up. The new
004 checks its starting version inside its transaction. 005_job_statuses.sql
then upgrades v4→v5 for job states/activities by rebuilding jobs and preserving
its references. Its caller must roll back on error and always restore foreign
keys in finally. initialize() now creates a fresh v8 database atomically, accepts v8, and
refuses to stamp an older or unversioned existing schema. The explicit version-aware
runner is documented in storage/MIGRATIONS.md; no live upgrades have been executed.

## Runtime logging and reload

The event helper supports created_by_job_id and the registered event names
above. Correlation trace IDs are derived from validated numeric IDs; supplied
arbitrary trace strings and other unsafe extra fields are dropped.
Do not copy catalog retryable into a retry decision: null means undetermined,
and boolean recovery decisions must reflect actual policy/evidence.
The audit source must not depend on logger.isEnabledFor.

configure_logging currently reapplies levels within a process. Preserve that
capability and validate settings before replacing active configuration.
Supervisor transport, reload signals, generation distribution and acknowledgments
are later work; no reload service or file watcher is claimed here.

Exception diagnostics now use bounded safe traceback summaries: a known
exception type and up to 16 frame line numbers. Raw exception text, paths,
source lines, custom class names, locals and chained exception content are
omitted. exception() never passes raw exc_info to handlers, so its output is
safe even with an unconfigured formatter. SafeFormatter also replaces raw or
cached exception formatting and stack_info for direct logging calls. Regex
redaction remains a defense for known credential patterns, not a guarantee
that arbitrary direct logger messages are safe. Components must use event()
and exception() with registered codes, not log raw provider values.

log_history uses a registered reason's severity (INFO for unreasoned lifecycle
entries), and resolves an underlying cause_reason_code to readable text too.
Runtime output is best-effort. Broken logger sinks return False rather than
turn committed work into a failure. Journald ingestion and supervisor reload
distribution remain deployment/future-component work, not implemented features.

## Offline acceptance cases authored, not executed

1. Catalog validation for event scopes, missing/disallowed reasons, unknown
   new codes, valid unknown historical reads, and component validation.
2. Detail type/size boundaries, secret-bearing rejected inputs, static errors,
   deterministic serialization, and absence of raw values in history.
3. Refusal outside a transaction; commit and rollback coupling for delivery,
   initial job+CREATED, identity, and representative job transition writes.
4. Successor insertion plus predecessor DONE and both histories roll back
   together on injected failure. This is a storage contract test, not a worker.
5. Multiple jobs per delivery, jobs without a delivery, retry attempt identity,
   nullable historical component, and optional created_by_job_id preservation.
6. History persists at CRITICAL logging; disabled/failed log sinks do not
   suppress committed history. Rolled-back work has no success lifecycle log.
7. Fresh v6 and sequential upgrades preserve IDs, payloads, foreign keys,
   indexes, histories and versions; failed upgrades do not claim a new version.
8. Safe exception diagnostics at every supported level, bounded output and
   no secret leakage across known redaction edge cases.
9. Reapplying validated local logging settings changes verbosity without
   duplicate handlers; invalid settings leave the active logger unchanged.
10. Every event/reason/state has English copy and stable translation keys;
    journal includes codes plus sentences, shared presentation resolves the
    same catalog, unknown history has readable generic text, and missing
    translations fall back to English. See HUMAN_READABLE_MESSAGES.md for
    the complete coverage, safe-rendering and locale acceptance requirements.

Tests must be committed with implementation but remain **unexecuted** under
the current project policy. Do not trigger CI. The executable cases in test_messages.py, test_diagnostic_history.py,
test_diagnostic_logging.py and test_diagnostic_migration.py remain unexecuted.
Existing storage/settings tests were updated. Only source review and AST
syntax parsing were performed; neither establishes runtime correctness.

## Remaining sequence

Review this implementation, v4/v5/v6 persistence changes and unexecuted offline
cases. The next component is worker ownership, routing, retries and
atomic completion against this contract. Dashboard, supervisor distribution, management
API, intake service and provider business logic remain later milestones.


## Using the implemented interfaces

- messages.DEFAULT_CATALOG.event(code), .reason(code), and .state(entity, code)
  return resolved copy and stable translation keys. An explicit MessageCatalog
  can hold trusted translated maps; no external file loader is included.
- diagnostics.configure_logging(settings, component) installs the safe
  formatter and updates process-local logger/handler thresholds without
  duplicate handlers. Invalid configuration is rejected before logger changes.
- diagnostics.event(logger, level, code, reason_code=..., event_id=..., job_id=...)
  validates registered emissions and adds static English messages. Reasons
  requiring context do not automatically determine a retryable log decision.
- diagnostics.exception(logger, "OPERATION_FAILED", exc,
  reason_code="JOB_HANDLER_FAILED", job_id=...) adds the safe traceback summary.
- history.append_event_history / append_job_history return frozen insert
  receipts. Pass safe_details-compatible dictionaries. Job lifecycle audits
  require an attempt matching the current job counter. CREATED is zero;
  processing events require positive attempts. Hold/review/block/cancel/activity
  events may record zero when work has not yet been claimed. The writer also
  captures current activity_code as a typed historical snapshot.
- diagnostics.log_history(logger, receipt) is called only after the outer
  transaction commits. Discard receipts on rollback. This is an explicit
  caller obligation, not a transaction hook; no arbitrary SQL history scanner
  or historical-detail renderer is claimed.
- storage accept_event / accept_event_and_job / save_identity own transactions
  and emit their lifecycle logs after commit. enqueue_job requires component
  and an active caller transaction; it creates its durable audit but performs
  no logging/commit. Its caller may log CREATED with validated correlation
  facts after commit. A Python connection context alone does not begin a
  transaction: use transaction(db) or explicit BEGIN/commit/rollback.

These writers validate audit shape and registered meaning; they do not enforce
job transition legality, ownership, expiry or safe external replay. Those are
worker requirements. Never catch a mutation/audit error and then commit
its enclosing transaction; propagate it so the unit rolls back.

## Job status follow-up

The v5 schema and messages cover legacy operational display distinctions.
MessageCatalog.job_status(state, activity_code) selects a readable/translated
label without masking terminal, held or review states. log_history emits both
activity_code and its static readable label. New ON_HOLD/BLOCKED/CANCELED
events require registered reasons. Activity is recorded on the event that carries
it, for example DEFERRED with TORRENT_DOWNLOAD_WAITING (ACTIVITY_CHANGED was
never emitted and was removed in migration 064). Specific handler reasons are registered when their
semantics are implemented. No cancellation actions, download ownership logic,
condition signaling or worker loop is implemented by the schema change.

## Separate process boundary and final diagnostic review

The future management API has its own process and api component identity.
It owns request authentication/authorization and invokes shared management
libraries. The worker owns job claiming, dispatch, leases, scheduling and
outcomes. It hosts no public management endpoints, settings persistence or SQL
migration API. Its small internal async control interface will support pause,
drain, resume, shutdown and readiness acknowledgment through the later shared
maintenance protocol. Supervisor owns process launch/restart when implemented.

006_api_diagnostics.sql permits api emitter attribution in both history tables,
with matching fresh v6 schema and logging overrides. Published 001-005 remain
unchanged. No HTTP API or cross-process control transport is implemented here.
The final diagnostic review includes API attribution and v6 migration regression
cases; all offline tests remain unexecuted. Source review, AST syntax parsing
and diff checks are the only validation performed under current policy.


## Worker naming follow-up

The canonical async job process is worker, formerly coordinator. New loggers,
logging overrides and audit writers use component=worker. SQLite v7 migration
007 adds worker to history component constraints, preserving old coordinator
attribution unchanged. Published SQL 001–006 remain immutable. Fresh SCHEMA,
initialization, migration runner and offline cases use the latest v7 contract.
Configuration schema v2 explicitly converts coordinator keys to worker; neither
reading a file nor starting a job silently migrates persistent settings.
The management API remains a separate process. Tests are included but unexecuted.


## Handler invocation history

The existing jobs model is retained. SQLite v8 adds registered handler_code,
static handler_label snapshot and invocation_id on job_history; ordinary/old
history entries use NULL. HANDLER_STARTED/HANDLER_COMPLETED/HANDLER_FAILED
have shared readable messages, with a registered reason required for failure.
Each invocation stays tied to one job/attempt and can be rendered separately
from job lifecycle state. See JOB_HANDLERS.md for registration, translation,
transaction ordering and the requirement for future worker lease fencing.
No worker runtime, management API or work_items redesign is introduced.
