# NextGen SQLite schema and change policy

## Mandatory rule: application changes and SQL schema changes go together

**Any PR that adds, removes, renames, or changes a SQLite table, column, index, foreign key, constraint, trigger, or persisted data shape MUST update the NextGen schema in that same PR.** Do not introduce a code-only SQL change that works solely because somebody manually patched a database. Likewise, do not ship a migration that leaves the Python persistence layer or its schema snapshot inconsistent.

For every change, contributors must:

1. Add the next numbered SQL file under `src/anime/storage/sql/` for existing database upgrades, with a documented expected starting `PRAGMA user_version`, atomic upgrade semantics, and the new version. **Never edit an already published numbered migration** to change its meaning.
2. Update the latest fresh-database `SCHEMA` in `src/anime/storage/sqlite.py`, including dependent foreign keys, constraints, indexes and references, and update the fresh-database `user_version`.
3. Update every matching SQL query, insert/update helper, and model-to-storage mapping used by the component. Do not put business logic or operational/job fields into the `AnimeSeries` domain model merely to avoid a DB change.
4. Add or update offline tests in `src/anime/tests/` for both a **fresh database** and the **sequential upgrade path**, including representative existing rows, referential integrity, and rollback/failure behavior. Commit these tests even when test execution is deferred.
5. Update **this document** when table meaning, schema version, relationships, or migration instructions change. Document any new columns and whether they can be NULL, their defaults, and their validation rules.
6. In the PR description, list the schema version transition, files changed, compatibility considerations, and whether tests have been run. Never describe unrun tests as passing.

This applies to **all** future components, including intake, worker, supervisor, identity, dashboard and `m` admin commands. Any program needing a new table or new stored field must accompany that requirement with a versioned schema change; no silent ad-hoc `CREATE TABLE` or `ALTER TABLE` inside runtime components.

## Current NextGen SQLite state (latest foundation)

Latest fresh SQLite version: **17**. The version stored in `PRAGMA user_version` is the **database schema version**, separate from the `AnimeSeries` Python domain `schema_version` (currently 2 in the checked-in SQLite identity-results constraint).

| Version | SQL file | Purpose |
| --- | --- | --- |
| 1 | `001_foundation.sql` | Historical foundation: accepted events (`webhook_events` at that version), event history, identified anime and episodes, identity results and episode links |
| 2 | `002_independent_jobs.sql` | Independent `jobs` and `job_history`; `created_by_job_id` is **optional provenance**, not a parent/child dependency relationship |
| 3 | `003_incoming_events.sql` | Rename `webhook_events` to `incoming_events`, preserving rows and updating SQLite foreign-key references |
| 4 | `004_diagnostic_components.sql` | Add nullable emitting component to both history tables, retaining historical rows |
| 5 | `005_job_statuses.sql` | Expand lifecycle states and add typed current/historical activity |
| 6 | `006_api_diagnostics.sql` | Permit the separate api emitter in both history tables |
| 7 | `007_worker_component.sql` | Add canonical worker attribution; preserve historical coordinator rows |
| 8 | `008_handler_history.sql` | Record individual handler invocations within existing jobs |
| 9 | `009_provider_cache.sql` | Shared provider pacing and bounded public AniDB/mapping cache |
| 10 | `010_release_verification.sql` | Bound declared single-episode verification to its job and identity snapshot |
| 11 | `011_trial_windows.sql` | Immutable season-scoped trial windows and per-job selection decisions |
| 12 | `012_episode_reservations.sql` | Durable exact-episode reservations bound to selected verification evidence |
| 13 | `013_acquisition_plans.sql` | Immutable hash-bound source inventory and submission marker |
| 14 | `014_acquisition_budgets.sql` | Frozen per-video/plan budgets and selected-byte evidence |
| 15 | `015_torrent_submissions.sql` | One-shot stopped-submission intent and reconciliation progress |
| 16 | `016_torrent_transfers.sql` | Fixed file selection, one-shot start and selected-file progress |
| 17 | `017_cancellation.sql` | Explicit cancellation scopes, cleanup intents and staging temporary receipts |

**Current tables:** `incoming_events`, `event_history`, `identified_anime`, `identified_episodes`, `identity_results`, `result_episode_links`, `jobs`, `job_history`, `provider_limits`, `provider_public_cache`, `release_verifications`, `user_evaluation_plans`, `user_evaluation_checks`, `episode_reservations`, `acquisition_plans`, `acquisition_budgets`, `torrent_submissions`, `torrent_transfers`, `job_operations`, `job_operation_members`, `cancellation_cleanup`, `staging_cleanup_artifacts`.

- `incoming_events`: durable inbound delivery record. An incoming event is not a notification and need not originate from a webhook. Do not deduplicate accepted deliveries by GUID, release title or payload.
- `event_history`: event-specific timeline and reasons.
- `identified_anime` / `identified_episodes`: verified identities and exact AniDB episode ownership; multiple AniDB anime may share a TVDB **series** ID.
- `identity_results` / `result_episode_links`: validated, versioned identity snapshots and event-specific episode associations. The domain model's optional episode coverage is distinct from operational state.
- `jobs`: independent queued work with optional original `event_id` and `created_by_job_id` provenance.
- `job_history`: durable operational job timeline, independent of journald verbosity.

Current fresh schema definition: `src/anime/storage/sqlite.py` (`SCHEMA`), alongside these incrementally versioned historical SQL scripts. They **must remain logically equivalent after the full upgrade sequence**, even though their historical names can differ.

## Migration and deployment boundaries

- Never apply these SQL scripts against the legacy production database. NextGen uses separate database state.
- `initialize(db)` currently atomically initializes an empty NextGen database to v17 or accepts an existing v17 database; it **does not implement a migration runner**. Versioned upgrades for existing v1/v2/v3/v4/v5/v6/v7/v8/v9/v10/v11/v12/v13/v14/v15/v16 files now use the explicit library described in [MIGRATIONS.md](MIGRATIONS.md); initialize() still never upgrades.
- The runner must inspect `PRAGMA user_version`, apply exactly the missing migrations in order, reject unknown/downgrade paths, and back up or otherwise protect state before a live upgrade. It must validate foreign keys and schema state after upgrade.
- A script's comment or `IF NOT EXISTS` is **not** a substitute for checking starting version. A migration must fail safely if applied to an unexpected version.
- SQLite changes that rebuild tables must preserve validated data, indexes, foreign keys, and history; account for transaction safety and SQLite rename/alter semantics.
- Keep SQL transactions short and preserve durable event/job writes and history. Do not infer that a schema change implements worker runtime behavior.

## Test preparation policy for the current development phase

**Code and offline tests are committed, but CI and offline tests are intentionally not executed right now**, per the project development decision. Test execution and any discovered failures are deferred and must be reported honestly. Keep test fixtures and explicit fresh-schema + migration-path regression cases ready for later local execution. No production deployment is implied by merging.

## PR checklist for any SQLite-affecting change

- [ ] New numbered, immutable-history-preserving SQL migration committed (unless no persistence shape changes)
- [ ] `src/anime/storage/sqlite.py` fresh schema and `user_version` updated
- [ ] Runtime SQL/queries and all dependent table/column references updated
- [ ] Fresh-install and sequential-migration tests added/updated
- [ ] Schema reference and migration notes here updated
- [ ] Test execution status accurately documented (currently **not run**)
- [ ] No legacy DB, production service, automatic CI or deployment touched

## Reference PRs

- [#93](https://github.com/bugz8unny69/anime-discovery/pull/93) — Python `AnimeSeries` v2 domain schema; **not** a database migration.
- [#94](https://github.com/bugz8unny69/anime-discovery/pull/94) — SQLite schema foundation v1.
- [#95](https://github.com/bugz8unny69/anime-discovery/pull/95) — SQLite jobs foundation v2.
- [#98](https://github.com/bugz8unny69/anime-discovery/pull/98) — SQLite inbound event naming migration v3.


## v4 diagnostic history fields and writer contract

Both event_history and job_history add component TEXT, nullable with implicit
NULL default, historically constrained to supervisor/intake/coordinator/identity/acquisition.
NULL denotes unknown origin in historical rows. New history writers require a
valid non-null component; migration does not invent attribution for old rows.
All IDs, raw payloads, identities, provenance, histories and indexes are retained.
No job/event state or AnimeSeries v2 field changes. details_json retains the
bounded shape documented in REASON_CODES.md; registered writers validate it.
Direct historical data is not tightened into a new event/reason SQL enum.

004 checks starting user_version=3 inside BEGIN IMMEDIATE, applies both ALTERs,
sets user_version=4 and commits. On any script error the caller must ROLLBACK;
no production upgrade runner is implemented. A wrong starting version triggers
ROLLBACK at the guard. 001/002/003 remain byte-for-byte unchanged.

The shared history writers require an explicit active transaction, validate
known code/scope/reason combinations, component, timestamps and detail bounds,
and never commit. enqueue_job now requires an active transaction and explicit
component, writes CREATED with attempt=0 and leaves commit to its caller.
The high-level delivery/identity helpers own explicit transactions and log
only after commit; a broken log sink does not invalidate persisted history.

Offline fresh/upgraded schema, existing-data preservation, wrong-version,
foreign-key, rollback and component constraint cases are included but unrun.

## v5 job state and activity support

jobs.state now permits PENDING, RUNNING, RETRY_WAIT, ON_HOLD, NEEDS_REVIEW,
DONE, BLOCKED and CANCELED. The default remains PENDING. ON_HOLD denotes a
condition-based wait, BLOCKED a policy/prerequisite stop, CANCELED an explicit
terminal cancellation. These states must not be claimed automatically. The
future worker defines validated transitions and wake-up behavior.

jobs.activity_code TEXT and job_history.activity_code TEXT are nullable with
implicit NULL default, constrained to DOWNLOADING, WAITING_FOR_DOWNLOAD,
WAITING_FOR_SEARCH and SKIPPED_DUPLICATE. SQL constrains current activity pairs:
DOWNLOADING with RUNNING/RETRY_WAIT, either WAITING_* with RETRY_WAIT, and
SKIPPED_DUPLICATE with DONE. NULL is allowed for any state and all old rows.
History activities are snapshots; there is no current-state constraint on an
old history row. The writer captures the job's activity without parsing text.

005 rebuilds jobs at v4→v5, preserving IDs, payloads, attempts, timestamps,
leases, event/provenance FKs and reasons. It recreates the three jobs indexes,
adds the history activity column and checks all FKs before committing v5.
Use only during maintenance with no active transaction and no other DB users.
Because jobs is referenced by itself and job_history, the script disables FK
enforcement before BEGIN. Its caller MUST ROLLBACK on an exception and always
restore PRAGMA foreign_keys=ON in finally, including on failed version/FK guards.
The script restores enforcement on success. Do not use a connection left with
FK enforcement disabled. No production runner is supplied by this change.

Fresh SCHEMA and initialize() now use v5. Published 001/002/003/004 remain
unchanged. No state is inferred from historical reason strings: existing
NextGen jobs keep their exact state and receive NULL activity. This is not a
legacy database import. See JOB_STATUSES.md for the display/legacy mapping.

## v6 separate API diagnostic attribution

The management API is a separate process from the worker. Its stable
component identifier is api in logging settings, runtime diagnostics and
both history tables. Using worker or supervisor as the API emitter would
misattribute actions and is not a substitute for its own component identity.

006 upgrades v5→v6 by rebuilding event_history and job_history with the expanded
component CHECK vocabulary. It preserves every row, history ID, unknown code,
nullable historical component, typed activity and both history indexes. The
foreign-key constraints remain enabled throughout. An in-transaction version
guard and FK check precede the version bump/commit. On any script error the
caller must ROLLBACK. Published 001-005 remain unchanged.

At that PR boundary the fresh schema and initialize() used v6. This adds attribution only;
no API server, authentication, configuration writer or worker is created.
Offline API logger/audit and v6 preservation/rollback/version cases are included
but unexecuted. The latest fresh-versus-sequential metadata case includes 006.


## Explicit migration runner and maintenance admission (no schema change)

`migrations.py` implements preview/apply from existing v1–v8, a validated SQLite
backup, per-version transactions and precommit schema/integrity/FK validation.
`maintenance.py` supplies a durable cooperative process admission barrier;
`admitted_connection()` is available to future runtime operations. No table,
column or persisted row shape changes were introduced by that runner PR.
The naming follow-up expands CHECK constraints to worker in v7; 001–006 remain
unchanged. Older paragraphs saying no runner describe the historical
PR boundaries; this section and MIGRATIONS.md supersede that implementation status.

All runtime users must be admitted before accessing SQLite, including an entire
job attempt and its heartbeats/final commit. Apply requires an active exclusive
lease after drainage; it leaves admission closed for compatibility/readiness
checks. Partial chains retain their last committed version; failures/crashes keep
the pending marker, and recovery is explicit. No API/supervisor/worker wiring
or production upgrade is supplied or executed here. Offline tests are unrun.


## v7 canonical worker component

The async jobs process is named worker (formerly coordinator). Fresh history
component constraints include worker and retain coordinator for historical rows
only. New settings/loggers/audit writers accept worker and reject coordinator.
No job lifecycle state, field or provenance semantics change.

007 upgrades only v6→v7 with FK enforcement enabled, rebuilding both history
tables and preserving every row, ID, unknown code, nullable component, activity,
foreign key and index. The old component text is not rewritten: historical
attribution remains true. The version guard, rebuild, FK validation and bump
are in one transaction; failures require rollback. The runner manifest includes
007; fresh SCHEMA and initialize() use v7. Configuration conversion to schema
v2 is independent and explicitly renames the settings keys. Offline preservation,
rollback, guard and fresh/sequential cases are included but unexecuted.


## v8 handler-invocation history (jobs unchanged)

008 upgrades only v7→v8, rebuilding job_history with three new nullable TEXT
columns (implicit NULL defaults): handler_code (uppercase ASCII code, 1–64
characters), handler_label (nonblank static label, 1–128 characters) and
invocation_id (32 lowercase hexadecimal characters). An all-or-none constraint
allows only three NULLs or a complete valid metadata set. Application writers
further validate static label safety, registration, event scope and attempt.
Old/unknown history gets NULL metadata without guessing a handler.

idx_job_history_invocation indexes (invocation_id, history_id). Partial unique
idx_handler_invocation_start permits at most one HANDLER_STARTED per non-null
invocation, and idx_handler_invocation_end at most one HANDLER_COMPLETED or
HANDLER_FAILED. NULL legacy invocations remain allowed, including historical
unknown/event-code collisions. The existing idx_job_history_job is recreated.
SQL does not claim to enforce cross-row start/ending identity: the registered
writer validates that inside the caller transaction. All previous IDs, columns,
rows, reasons, components, activities and FKs are preserved. event_history and
the jobs table are unchanged. No work_items table, handler-dispatch code or new
job lifecycle state is introduced.

The v7 guard, rebuild, FK check and v8 version bump share one transaction with
FK enforcement enabled. A direct caller must rollback on failure; the explicit
runner validates before COMMIT. Fresh SCHEMA/initialize/admitted connections
and the runner manifest use v8. Published 001–007 remain unchanged. The separate
configuration schema remains v2. Preservation, constraints, rollback, wrong
version and all-version sequential cases are included but unexecuted.


## Provider operational state (v9)

provider_limits has one row per allowlisted provider: anidb, anidb_titles,
anime_lists or tvdb. next_allowed_at_ms is a nonnegative UTC-millisecond
reservation/cooldown boundary; failures defaults to zero and is bounded 0–16.
reservation_token is a required opaque 32-character publication-generation token,
not an authentication credential. No row is created until a request is reserved.

provider_public_cache has a composite (provider,cache_key) primary key, required
fetched_at_ms/expires_at_ms (expiry after fetch), a required public XML BLOB up to
64 MiB, and an expiry index. Only AniDB HTTP, AniDB title dump and Anime-Lists
mapping payloads are allowed. Helpers bound the complete cache to 130 rows and
128 MiB, evicting oldest fetches; expired/future-dated entries are not reused.
No TVDB responses, credentials or bearer tokens can be stored through this API.
TVDB data/token caching is process-local and authorization-scoped.

Runtime helpers never create tables or migrate. Provider requests run outside
short reservation/cache transactions while the job retains maintenance admission.
Old numbered scripts 001–008 remain immutable. See ../providers/README.md.


## Declared release verification (v10)

release_verifications binds one immutable verification row to a unique job_id,
identity_result_id and AniDB anime/episode ownership. verification_id is the
primary key; idx_release_identity indexes the identity snapshot reference.
Composite foreign keys enforce that the identity result and AniDB episode belong
to the recorded anime. The helper also verifies the job's incoming event,
unchanged source bytes/hash, canonical TVDB identity and source snapshot before
inserting within the fenced worker transaction.

All columns are nonnullable. The row stores release_sha256 (64 lowercase hex),
positive AniDB episode number/ID and TVDB series/episode record IDs, a nonnegative
TVDB season, a positive TVDB episode number and nonnegative verified_at_ms.
tvdb_order defaults to and is constrained to official. verification_scope is
constrained to DECLARED_SINGLE_EPISODE. This is declared-release identity evidence,
not a verified file manifest, trial eligibility or acquisition permission.
The existing AnimeSeries snapshot is preserved and no job/state redesign occurs.


## Trial selection intents (v11)

user_evaluation_plans has one immutable plan per AniDB anime, TVDB series, verified official
season/order. Required fields bind its anchor verification/episode, bounded
backfill/following counts (0–1000 with at least one nonzero), configuration revision
and creation time. Anchors/count arithmetic is checked against signed-integer
bounds. Helpers cross-check the anchor and current verification source identities.
The unique scope makes first committed plan creation safe under concurrency.

user_evaluation_checks records one decision per job, with source verification, selected
boolean, readable allowlisted reason and timestamp. plan_id is nullable only for
USER_EVALUATION_DISABLED with selected=0; other decisions reference a plan and have matching
selected/reason semantics. idx_user_evaluation_checks_plan supports plan history.
Neither table grants an acquisition lease or proves file/provider availability.
Backfill/following numbers are requested season positions, not invented episode IDs.

## SQLite v12: exact episode reservation

012_episode_reservations.sql adds episode_reservations. The table binds its
unique owner job to evaluation, verification, plan and provider identities.
Unique official TVDB season/episode, AniDB episode ID and TVDB episode ID prevent
competing placement/ownership from silently creating a second slot. No row is
external content ownership or proof of staging. There is no automatic TTL or
release operation. See ../jobs/RESERVE_EPISODE.md. Historical 001–011 is immutable.

## SQLite v13: acquisition preparation

acquisition_plans stores bounded validated source-file inventory, exact info
hashes, unique job/reservation/marker and configuration revision. PREPARED is
the only supported state and confers no external ownership. Its state machine
requires a future versioned migration before submission/reconciliation is added.
See ../jobs/PREPARE_ACQUISITION.md. Historical SQL 001–012 remains immutable.

## SQLite v14: prepared-byte budgets

acquisition_budgets freezes per-video/plan byte caps and configuration revision.
acquisition_plans.selected_size_bytes is positive for new admissions and nullable
only for preserved historical rows; helpers block further admission when prior
size is unknown. Budget creation, aggregate check and preparation commit are
atomic. No automatic release or live disk accounting is implied. See
../DOWNLOAD_CONFIGURATION.md. Historical 001–013 remains unchanged.

## SQLite v15: stopped submission journal

torrent_submissions binds one immutable pinned request to a prepared plan and
first job/attempt, with constrained UNCERTAIN/EXTERNAL/REVIEW/OWNED_STOPPED
reason pairs. Absence/retry/expired lease never deletes or rearms the row. Short
fenced journal progress commits are distinct from final queue completion.
See ../jobs/SUBMIT_TORRENT.md. Historical SQL 001–014 remains immutable.

## SQLite v16: selected-video transfer journal

`torrent_transfers` records one fixed selection and one possible start per
submission plan, with creating job/attempt, immutable fingerprint, piece geometry,
bounded progress and readable phase/reason. Historical stopped submission facts
remain receipts after handoff. Migration 016 and the fresh schema agree; published
SQL 001–015 is unchanged. See [DOWNLOAD_TORRENT.md](../jobs/DOWNLOAD_TORRENT.md).

## SQLite v17: cancellation and cleanup

Explicit scope membership follows trusted successor handoff; provenance stays
historical. Requests fence claims/completion, while per-operation OS locks prove
active work drained before destructive I/O. Cleanup journals retain uncertain
stop/remove and exact file fingerprints; staging receipts protect publication.
Migration 017 preserves existing jobs/history and gives old jobs separate scopes,
without guessing dependencies. Historical SQL 001–016 is unchanged.
See [CANCELLATION.md](../CANCELLATION.md) for capabilities and recovery limits.

## SQLite v18: durable management actions

018 adds management_actions with a unique control job and a composite
(operation_id, action) key for KEEP/CLEANUP. Foreign keys retain the target
scope/job and control job; a self-target check rejects the identical job ID.
Runtime helpers enforce distinct control/target scopes, matching kind and current
eligibility. Cancellation plus conditional cleanup enqueue is atomic. No prior
rows are rewritten; fresh schema and explicit runner use v18. Published SQL
001–017 remains unchanged. See [MANAGEMENT_ACTIONS.md](../MANAGEMENT_ACTIONS.md).
