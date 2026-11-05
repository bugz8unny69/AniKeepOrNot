# SQLite upgrades and maintenance library

Latest schema: **v18**, adding durable management action submissions.

Implemented library only: `storage/migrations.py`, `maintenance.py` and the
`storage.sqlite.admitted_connection()` runtime helper. The public management
API, ADM command, worker, supervisor and recovery UI are future consumers.
No upgrade or SQL has been executed during development; offline tests are unrun.

## Separate service responsibilities

The management API authenticates/authorizes an ADM request and calls libraries
under the service account. It never submits a database upgrade as a normal job.
The worker remains a separate async jobs process. These blocking filesystem,
SQLite and flock operations belong in a worker thread, as a whole operation;
do not hold an exclusive lease in one thread and release it in another coroutine
before that thread stops. Cancellation must await thread completion before
exiting the lease. Maintenance lock attempts themselves are nonblocking.

All cooperating local processes must use the same `MaintenanceGate(database)`
canonical path and local filesystem. Its parent directory must already exist,
be service-owned and inaccessible to untrusted writers. Do not unlink/replace
stable lock files, use aliases/hard links for a database, fork with admissions
held, or treat advisory locks as protection against arbitrary external programs.
The configured database location is restart-required; do not switch gate scopes
while old work is running. NFS/distributed workers are outside this contract.

`gate.operation()` holds shared admission for the **entire job attempt or API
operation**, including provider waits, short-lived DB connections, final commit,
lease heartbeats and cleanup. Take admission **before claiming** a job. Do not
release it between claim and dispatch. Different jobs can hold shared admissions
concurrently, including jobs for the same anime. This is a maintenance barrier,
not business-key serialization. Provider throttles and acquisition gates still
belong in their libraries. Nested admissions are prohibited: an outer handler
passes its admitted context to helpers rather than asking them to admit again.

`admitted_connection(gate)` is a convenience for a complete standalone database
operation: admit, open an existing current-v8 database with FK enforcement, and
close it before releasing admission. It never initializes or upgrades. Legacy
low-level `connect()` remains for explicit bootstrap/offline fixtures; existing
storage helpers are connection-based and do not secretly take admissions.
**No existing daemon is claimed to be gated**: the future runtime must wire every
DB user and job attempt to this contract. In-flight admitted work may continue
to open connections and heartbeat after a request; all connections must close
before its admission ends. Unadmitted maintenance-status/recovery endpoints use
the gate files only; endpoints that need ordinary database access are paused.

## Admission and maintenance sequence

1. The API previews via `migrations.preview(gate.database)` within normal
   admission, or previews under an existing exclusive maintenance lease.
2. `ticket = gate.request()` durably creates a bounded pending marker and blocks
   new admissions. Repeated requests return the existing ticket.
3. Already admitted work drains naturally. Pending/retry/held jobs remain exactly
   as stored; maintenance neither cancels nor claims them nor resets attempts.
   `gate.exclusive(ticket)` raises `MaintenanceBusy` until all admissions drain.
   The future API schedules bounded polling and displays status. No forced
   timeout, cancellation or lease reassignment is hidden in this library.
4. Inside exclusive maintenance, call `migrations.apply(database, plan,
   lease=lease)`. It checks lease scope, revalidates the version/schema and
   packaged script hashes against the preview, and backs up before changing DDL.
5. A successful apply **keeps admission closed**. The service must establish that
   processes are compatible with the new schema and ready to use it before
   `lease.complete()`. Actual restarts/readiness acknowledgments belong to later
   process supervision. Reopening happens only on clean exclusive-context exit.

No deadline clears the marker. A failed upgrade, controller crash, or exit
without completion leaves maintenance requested, even though OS locks release.
A restarted trusted API can read `gate.status()` and reenter exclusive maintenance
using that ticket. Tickets are service-side request identifiers, not an auth
mechanism. Corrupt/missing/unreadable state or conflicting controllers fail closed.
A missing marker permits ordinary admissions but cannot grant an exclusive lease.

Do not blindly resume after failure. Inspect the actual current schema, validate
health and process compatibility, and explicitly continue or complete recovery.
Re-preview after any partial upgrade. A timed-out drain can be abandoned by
explicitly reacquiring exclusive access and checking readiness before completing;
it cannot automatically resume while old work still holds admissions.
Lease deadlines continue to use wall-clock time during maintenance. After resume,
the future worker recovers genuinely expired leases by its normal fenced
policy; this library never extends/reassigns leases or rewrites job states. This first
version favors safe draining over immediate cancellation. Configuration updates
that require only per-operation snapshots need no global drain. Future destructive
configuration/database-location changes use an explicit maintenance/restart flow.

The pending marker and stable admission/work lock files live alongside the DB;
only the marker contains a 32-character identifier. It stores no payload, settings
values, credentials or exception strings. Writes synchronize file and directory.
A resume directory-sync failure attempts to recreate the marker before releasing
the admission mutex and reports failure. Severe filesystem failure during that
repair cannot guarantee durable state; keep the service stopped until storage and
maintenance state are inspected. Never infer readiness from a successful HTTP
request alone. Stable file lock artifacts remain after successful completion.

## Version-aware database runner

Only existing, regular, versioned NextGen SQLite databases at v1–v8 are supported.
Version 0, unknown/newer versions, schema drift, orphan foreign keys and failed
integrity checks are refused. An empty new DB uses explicit `initialize()`;
legacy databases are never detected/imported by guessing table names.

The immutable bundled manifest names 001–008. Preview reports starting/target
versions, a schema fingerprint and the missing-script SHA-256 values. Preview
uses a read transaction and read-only database connection; it makes no schema
changes, backups or implicit database. SQLite may access existing WAL/SHM files;
this is not a promise of an OS filesystem with no sidecar activity.
Version/schema/script equality protects against stale plans, **not changing job
row content**. Normal work may finish between preview and drain. Backup captures
the latest drained state, which is what the upgrade must preserve.

Before modifying DDL, SQLite's backup API captures all committed data, including
WAL, into a unique `DATABASE.before-vN-ID.sqlite` in the same directory. The backup
is validated for version, full schema, foreign keys and integrity, then its file
and directory are synchronized. Its mode is 0600. Retention/deletion and export
are explicit future administration operations. Failure may leave an incomplete
backup artifact for inspection; it never permits the upgrade to continue.

The runner owns a new dedicated connection, outside any caller transaction.
It applies exactly the missing scripts in order and checks the starting version
before **every** step, including historical 002/003 that lack SQL guards. It
executes complete SQL statements without `executescript`'s implicit pre-commit.
Before each script's COMMIT, it checks resulting version, table/index/constraint
structure, all foreign keys and database integrity. It always rolls back a failed
step and restores FK enforcement, particularly for the v5 referenced-table rebuild.
The final v8 contract is compared with fresh `SCHEMA`.

**Atomicity is per migration version, not the whole upgrade chain.** If v4 commits
and v5 fails, the database remains at v4; backup retains the pre-upgrade version,
and maintenance stays active. After a crash SQLite recovers the current step by
its journal/WAL. Re-preview the actual recovered version before continuing.
No automatic restore, downgrade or deletion of database/WAL/SHM files is allowed.
A restore requires a separate stopped-service procedure and validated backup.

SQLite **v7** adds worker to the two history component CHECK vocabularies,
retaining coordinator solely for historical attribution. Migration 007 preserves
all rows and indexes; published 001–006 remain unchanged. No job state or payload
shape changes. The latest fresh schema includes the same constraints;
configuration schema versions and publication revisions remain independent.
Operational events/reasons have registered static English messages and translation
keys; errors expose no raw SQL, filesystem paths or SQLite exception strings.
The API catch boundary logs failures using OPERATION_FAILED plus the typed reason;
successful upgrade/request/resume emits registered runtime markers. The durable
maintenance marker and schema version carry recovery state regardless of log level.
A management-request audit table/API is future work, not supplied by these markers.

## Offline verification prepared

Cases cover every supported starting version, fresh-schema equivalence, preserved
identity/history/provenance and WAL backup, unknown/version-zero/drift/FK failures,
stale plans/script changes, scoped/expired leases, backup failure, precommit rollback
and partial-chain recovery, FK restoration, symlinks, cross-process drain, controller
process death and explicit resume. Tests are authored but **not executed**. No CI,
SQL execution, deployment, legacy runtime changes or live database access occurred.


The handler-history follow-up extends the latest schema to v8 through immutable
008, rebuilding only job_history. The jobs table and independent queue model
remain unchanged; handler invocation fields/indexes are described in
SCHEMA_POLICY.md and ../JOB_HANDLERS.md. Published 001–007 remain unchanged.


009_provider_cache.sql upgrades v8 to v9 atomically under the same explicit
maintenance protocol. It adds provider_limits and provider_public_cache with an
expiry index, preserving existing job/event/identity/history rows. Fresh schema
and runtime version guards now require v9. It runs no provider HTTP requests,
imports no legacy metadata and persists no authentication tokens. A provider
library never initializes or upgrades a database implicitly.


010_release_verification.sql upgrades v9→v10 under explicit maintenance, adding
release_verifications and its identity-reference index. It preserves provider,
job, event, identity and history rows. The runner supports all starting versions
v1–v10; runtime initialization/version guards use v10. Historical scripts 001–009
remain immutable. No migration, SQL or database upgrade was executed in development.


011_trial_windows.sql upgrades v10→v11 atomically under explicit maintenance.
It adds trial_plans/trial_evaluations with constrained counts/decisions and a plan
history index. Fresh schema, runtime guards and v1–v11 sequential cases accompany
the migration. Earlier scripts 001–010 remain unchanged. No SQL was executed.

SQLite v12 follow-up. 012_episode_reservations.sql upgrades v11→v12
with exact-episode gates. Use the existing explicit maintenance migration path;
no runtime helper creates the table. Fresh DDL and versioned SQL are updated
together. Published migrations 001–011 remain unchanged.

013_acquisition_plans.sql upgrades v12→v13 under explicit maintenance, binding
one preparation job/reservation/content hash to inventory and a unique marker.
Only PREPARED state is supported. Fresh schema and sequential upgrade coverage
extend through v13. Historical scripts 001–012 remain unchanged.

014_acquisition_budgets.sql upgrades v13→v14 with a frozen per-selection-plan
budget and nullable selected_size_bytes on prepared plans. Old rows remain NULL
and require explicit size review before further admission. Fresh DDL and runner
coverage extend through v14; historical scripts 001–013 remain immutable.

015_torrent_submissions.sql upgrades v14→v15 with one-shot stopped-submission
intent/progress and constrained reconciliation states. No automatic rearm or
external client request occurs during migration. Fresh schema and sequential
coverage extend through v15. Historical scripts 001–014 remain unchanged.

## Selected-video transfer journal

016_torrent_transfers.sql upgrades v15→v16. The new constrained transfer journal
records separate selection/start authorization and bounded client progress without
changing prior submission receipts. Fresh DDL and sequential runner coverage extend
through v16; published SQL 001–015 stays unchanged. Tests remain unexecuted.

## Cancellation scopes and cleanup

017_cancellation.sql upgrades v16→v17, preserving job/history facts and
backfilling each historical job as an independent cancellation scope. Trusted
future successor handoffs share explicit scope membership. Fresh schema, runner
and offline equivalence/preservation cases extend through v17; published SQL
001–016 remains unchanged. See ../CANCELLATION.md. Tests remain unexecuted.


## v18 durable management actions

018_management_actions.sql upgrades v17→v18 with the management_actions table,
foreign keys, action allowlist and unique target-operation/action and control-job
constraints. Existing jobs, histories, cancellation scopes and journals are
unchanged; no historical action associations are guessed. Fresh SCHEMA and the
runner manifest match v18. Scripts 001–017 remain immutable. Offline preservation,
fresh equivalence, guard, uniqueness and rollback cases are written but unrun.
See [MANAGEMENT_ACTIONS.md](../MANAGEMENT_ACTIONS.md).
Current Keep addition: schema v22 adds 022_keep_imports.sql, the durable Sonarr
series/import intent and permanent-file result. Published 001–021 remain unchanged.
Use the existing maintenance preview/backup/apply contract; startup never upgrades
an existing v21 database implicitly. Configuration v8 and secrets v4 are independent.


## v57 → v58: job-level FAILED terminal state

058_failed_job_state.sql rebuilds the jobs table with a CHECK constraint
permitting FAILED alongside existing states (PENDING, RUNNING, RETRY_WAIT,
ON_HOLD, NEEDS_REVIEW, DONE, BLOCKED, CANCELED). Existing jobs, foreign keys,
and indexes are preserved. Fresh SCHEMA and the migration runner match v58.

