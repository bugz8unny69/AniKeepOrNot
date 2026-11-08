# Async worker core

Current metadata composition: the identity worker supplies the built-in
hash-addressed HTTP(S) reader and installs acquisition through download by
default. Source configuration is checked at use, not during unrelated identity
startup. Missing source settings enter review. A trusted read_metainfo override
remains supported. This supersedes older optional-reader/default-reservation-only
notes below. See [metadata retrieval](acquisition/METAINFO_SOURCE.md).

Implemented core only: worker.py, job_contracts.py and storage/job_queue.py.
An opt-in identity handler/persistence adapter is now in jobs/identify_anime_job.py;
its concrete verified-identity resolver is still required at composition time.
Concrete provider libraries now support opt-in identity composition (see
providers/README.md); no download, trial or Keep handlers are bundled.
The separate management API/supervisor remain future consumers; the worker has
no HTTP server and does not launch subprocesses. The default empty handler
registry fails readiness before claiming work. No worker has been run or deployed.

## Trusted registration and operation

Install explicit Registration entries in Registry. Each binds a stable kind to
a HandlerDefinition (code/static label), callable, async/thread mode, allowed
successor kinds and optional pure route/synchronous persistence adapters. All
allowed successor kinds must be installed. No directory scanning, payload module
names, hooks or arbitrary dynamic imports. Handler modules use `_job.py` names.

The service/application entry point is `await run_worker(config_directory,
registrations)`, which reads current validated startup configuration and handles
SIGTERM/SIGINT by stopping claims and draining admitted work. Startup concurrency
and database location stay pinned; each attempt reads a fresh immutable operation
snapshot. The combined view is revalidated after startup fields are pinned;
a newly saved media root cannot overlap the database still active before restart.
An invalid effective view fails before claim, leaving pending jobs untouched.
Trial/Keep destinations are available through that snapshot but a
publishing handler must validate configuration, real paths/access and recorded
artifact bindings before acting. The worker itself never moves or deletes media.

A handler receives Context with immutable claim/config references, safe logger,
thread-safe cancellation event and data() returning detached payload data. It
returns an Outcome: complete, retry, defer, hold, review or blocked. Completed
values are feature-specific typed facts for its trusted persistence adapter,
not raw exception strings or generic fields inserted into diagnostic history.

Outcome reasons must be registered. retry/defer delays are bounded; worker policy
sets lease/poll intervals and retry/backoff limits. These policies are validated
constructor parameters, not silently invented settings-schema keys. Registration
requires async functions for async mode, synchronous callables for thread mode.
Do not spawn untracked background threads/tasks from a handler.

## Claiming, invocation and atomic handoff

All attempts (including direct embedded calls) share startup worker.concurrency
slots and tracking. The async loop checks
expired leases, and claims only due PENDING/RETRY_WAIT rows ordered by readiness
and ID. Each claim uses BEGIN IMMEDIATE, increments attempt_count, sets a fresh
lease token/expiry and writes CLAIMED in one transaction. There is no blanket
per-anime lock or deduplication of incoming deliveries.

Maintenance admission covers configuration read, claim, handler, heartbeat and
final persistence, including offloaded work. Pending maintenance blocks new
admissions while existing work drains. Idle polling does not retain admission.
Provider/resource gates still belong in feature libraries.

Enqueue rejects payloads exceeding 64 KiB UTF-8. Claims do not transfer oversized
historical payloads into Python; their original stored contents remain untouched.
Before invoking a known handler, the worker validates bounded object payloads
and commits HANDLER_STARTED. Unknown kinds/invalid payloads go to NEEDS_REVIEW
with readable reason codes without pretending the handler was called. The
heartbeat renews only an unexpired matching RUNNING token/attempt. Failure to
confirm ownership cancels the handler and never assumes it may commit success.

Completion uses a dedicated connection and worker-owned transaction: verify
lease/state/attempt; append handler ending; run the trusted persistence and
routing adapters; insert permitted successors with existing event/provenance;
recheck lease; update job state/release lease and append its audit; commit. All
facts/successors/ending records roll back together on failure. Adapter commit,
rollback and savepoint transaction escapes are denied by SQLite authorization;
adapters must still be short, synchronous database operations with no external
calls or DDL. Route adapters are pure and return at most 32 Successor records.
The worker returns/logs successor CREATED receipts only after the same commit.

Persisted completion is not reconstructed by repeatedly scanning all DONE jobs.
A stale or repeated completion fails ownership checks, preventing a second
successor handoff. No work_items table, parent/child workflow engine, new jobs
columns or SQLite migration is needed for these operations; the queue/history shapes remain unchanged (provider tables bring schema to v9).

## Waiting, retries and failures

A normal defer (for example a prerequisite/download wait) writes DEFERRED,
RETRY_WAIT and a future availability, with optional permitted activity label.
It does not spend the failure retry budget. attempt_count counts actual claims;
the budget separately counts committed RETRY_SCHEDULED records for this job.

Failure retries use bounded exponential backoff and a registered reason. A
requested retry delay can extend the backoff up to the outcome's one-day bound.
After max_retries committed retry schedules, another failure enters NEEDS_REVIEW
with JOB_RETRY_EXHAUSTED and the cause code. retryable hints never decide this
policy. Permanent blocked/review/hold outcomes are not automatically reclaimed.
Unexpected handler exceptions become JOB_HANDLER_FAILED retries, without storing
exception text. Normal deferred/hold/review outcomes complete the handler call
but do not mark the queued job DONE.

Expired RUNNING leases record LEASE_EXPIRED and either schedule a bounded failure
retry or exhaust to review. No attempt count is added until a new claim. A stale
invocation can retain its unmatched start; recovery does not invent a successful
ending. Existing external side effects require feature-specific intent/receipt
reconciliation before replay. SQLite fencing is not external-service exactly-once.
Expired historical RUNNING rows with zero attempts are quarantined in NEEDS_REVIEW
with JOB_STATE_INVALID, without inventing a claim or lease-expiry observation.
They do not prevent recovery of other valid rows. Other malformed rows fail closed.

Storage/configuration/completion-adapter failures stop the polling loop and drain
other tasks; they do not blindly replay uncertain commits. Existing leases can
then be reconciled/recovered on a later start. A lease lost to expiry is a per-job
ownership loss. Every failure emitted by this core uses a registered safe reason;
no payloads, lease tokens, configuration values or raw exception strings are logged.

## Pause, drain and cancellation

pause/resume/stop/drain are local event-loop controls for a later transport to
invoke. They are not an implemented management API/IPC or supervisor readiness
protocol. The durable MaintenanceGate remains the cross-process admission barrier.
A stop/pause flag is checked again inside the claim transaction. A claim already
authorized before stopping is admitted work and may drain normally.

Graceful stop does not cancel running jobs. Forced task cancellation waits for
owned executor work to finish before releasing maintenance admission; repeated
cancellation cannot abandon a blocking thread in the background. Child cleanup
is shielded and drained. A handler that independently raises CancelledError is
recorded as a handler failure; cancellation of the owning attempt leaves recovery
to its lease. close() stops claims and drains embedded attempts, and concurrent
close callers wait for the same executor shutdown. An attempt cannot close or
drain itself. A noncooperative blocking handler can therefore delay
shutdown/maintenance; process isolation/forced termination would require a
separate explicit design. Interrupted work keeps its recorded lease/start for
fenced recovery; the worker does not silently reset it to PENDING.

## Remaining feature work and verification

The agreed architecture documents still guide actual business handlers. Provider
pacing, exact-resource acquisition gates, ownership receipts, artifact/decision/
import manifests and their SQL helpers are feature work, not secretly supplied
by the worker core. The authenticated management API and process supervision
remain separate components.

Offline cases cover exclusive claims, renew/expiry/stale owners, retry-versus-
defer budgeting, atomic facts/successors, adapter transaction escapes, current
configuration snapshots, same-anime concurrency, handler failures, maintenance,
shutdown and blocking cancellation. These tests are authored but **unexecuted**.
No CI, application/worker, SQL, production or media operations have been executed.


## Feature persistence results and expected rejection

A synchronous persistence adapter may return None or a tuple of HistoryRecord
receipts for histories it wrote inside the worker transaction. These are emitted
only after the whole completion commits; arbitrary text/results are rejected.
Adapters must not log tentative history themselves. The identity adapter uses
this for the existing incoming-event SERIES_IDENTIFIED audit.

An adapter can raise CompletionRejected with a review/hold/blocked Outcome for
an expected domain conflict. The first completion transaction rolls back facts,
handler ending and successors. The worker then rechecks the same lease and records
that outcome in a new transaction without invoking persistence/routing again.
It never converts storage or unexpected adapter exceptions into successful
completion. See jobs/README.md for the first feature adapter and its limitations.


## Identity-provider readiness composition

providers.configuration.build_identity_worker/run_identity_worker compose the
required AniDB-first resolver and protected SecretStore. They validate startup
configuration locally, and install a synchronous readiness callback run in the
control executor after each fresh operation snapshot but before claiming work.
Readiness failure releases admission without changing queued jobs. Configuration
or credential-storage errors discovered later in a handler remain infrastructure
errors; the worker stops/drains and preserves the lease for recovery instead of
inventing a provider retry. The generic Worker remains usable for other trusted
job registries; no HTTP management transport or supervisor is added here.


The concrete composition also registers SELECT_USER_EVALUATION after VERIFY_RELEASE.
Feature-owned completion adapters atomically freeze trial windows and decisions;
the generic worker remains concerned only with execution, fencing and auditing.
TRIAL_EVALUATED receipts are logged only after commit. No acquisition handler or
resource lease is supplied by numeric selection policy. See USER_EVALUATION_POLICY.md.

The default provider composition now includes RESERVE_EPISODE after evaluation.
Its feature adapter reserves one exact verified episode inside the ordinary
fenced completion transaction. Busy slots become held jobs; inconsistent
evidence becomes review. It adds no whole-anime scheduling lock, external
client call or acquisition successor. See jobs/RESERVE_EPISODE.md.

Optional trusted read_metainfo composition installs PREPARE_ACQUISITION after
selected reservations. It performs metadata reads outside write transactions
and publishes an immutable PREPARED inventory/marker through the worker's
ordinary fenced completion adapter. No dynamic hooks or external torrent
submission/reconciliation transport are added. See jobs/PREPARE_ACQUISITION.md.

Download settings are validated at point of use, not imposed on unrelated
identity work. Acquisition preparation now atomically admits the selected video
against its selection plan's frozen per-video/total size caps. A rejected or
unknown-size admission goes to readable review. No external client transport or
submission state is introduced. See DOWNLOAD_CONFIGURATION.md.

SUBMIT_TORRENT now journals a one-shot stopped add and read-only reconciliation.
Its short journal transactions use the existing attempt-wide maintenance
admission and current queue lease; no HTTP spans a write transaction. Durable
external progress can precede final queue completion and survives crashes.
No replacement send right is granted after expiration/absence. See
jobs/SUBMIT_TORRENT.md for the external-fencing limitation and next transfer stage.

## Downloaded-media verification

The default identity-worker composition routes completed DOWNLOAD_TORRENT into
VERIFY_MEDIA and installs its handler. Generic/custom registries stay explicit.
See [the verification contract](jobs/VERIFY_MEDIA.md). The worker requires local
shared download paths and `/usr/bin/ffprobe`. SQLite v19 requires explicit upgrade.
No staging successor is installed yet; verification success does not mean staged.

## Verified-video staging

The default composition now also installs STAGE_USER_EVALUATION_MEDIA and routes successful
VERIFY_MEDIA completion into it atomically. See
[jobs/STAGE_USER_EVALUATION_MEDIA.md](jobs/STAGE_USER_EVALUATION_MEDIA.md). Use a non-root worker and an
accessible existing staging folder. SQLite v20 upgrades explicitly; Keep and
viewer refresh remain separate, unimplemented integrations.

## Current Shoko and service setup contract

The latest draft adds optional Shoko AniDB metadata after Keep (configuration v10,
backward-readable secrets v6; SQLite stays v23). It also provides explicit local
setup/maintenance commands and composed worker/systemd examples. See
[Shoko metadata](SHOKO_METADATA.md) and [Service setup](SERVICE_SETUP.md).
These supersede earlier remaining-Shoko/supervision/version notes above.
Tests, migrations and services have not been executed.
